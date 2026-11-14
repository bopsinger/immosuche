"""Tageslauf: alle Suchen ausführen, neue Treffer je Suche melden, Kartendaten schreiben.

Aufruf:  python -m immosuche [--dry-run] [--html vorschau.html]
Suchen: suche.yaml (von der Karte gespeichert; Liste "suchen"), sonst Abschnitt "suche" aus config.yaml.
Umgebungsvariablen (GitHub-Secrets):
  MAIL_USER, MAIL_PASSWORD   Konto für Versand (SMTP) und Sammelpostfach (IMAP)
  MAIL_TO                    Standardempfänger für Suchen ohne eigene Empfänger, kommagetrennt
  SMTP_HOST (smtp.gmail.com), SMTP_PORT (465), IMAP_HOST (imap.gmail.com), MAIL_FROM, VERSAND_ZEIT
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import flatfox, mailbox, notify, schluessel
from .models import Listing

log = logging.getLogger("immosuche")
KEEP_DAYS = 180
POSTFACH_TAGE = 21          # Inserate aus Portal-Mails so lange auf der Karte zeigen
STANDARD_ID = "standard"    # Kennung der Suche aus der alten Einzel-Konfiguration
META = ("id", "name", "aktiv", "empfaenger")   # Felder einer Suche, die keine Suchkriterien sind
PORTALE = {  # Portale, die über Suchabos ins Sammelpostfach eingebunden werden (Comparis genügt)
    "comparis.ch": ("Comparis", "https://www.comparis.ch/immobilien"),
    "homegate.ch": ("Homegate", "https://www.homegate.ch"),
    "immoscout24.ch": ("ImmoScout24", "https://www.immoscout24.ch"),
    "newhome.ch": ("Newhome", "https://www.newhome.ch"),
}
PFLICHT = {"flatfox", "comparis.ch"}


def ts_iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes") if ts else None


# ---------------------------------------------------------------- Konfiguration
def name_aus_orten(crit: dict) -> str:
    orte = [str(o.get("name", "")).replace("Ort ", "").split(" (")[0] for o in crit.get("orte") or []]
    return ", ".join(o for o in orte if o) or "Meine Suche"


def load_config(root: Path, explicit: str | None) -> tuple[dict, list[dict], str]:
    """Liefert (Grundeinstellung, Liste der Suchen, Herkunft)."""
    cfg = yaml.safe_load(Path(explicit or root / "config.yaml").read_text())
    quelle = explicit or "config.yaml"
    datei = root / "suche.yaml"
    if not explicit and datei.is_file():
        inhalt = yaml.safe_load(datei.read_text()) or {}
        quelle = "suche.yaml"
    else:
        inhalt = cfg
    if "suchen" in inhalt:
        suchen = [dict(s) for s in inhalt["suchen"] or []]
    else:  # altes Format mit einer einzigen Suche
        einzel = dict(inhalt.get("suche") or {})
        suchen = [{"id": STANDARD_ID, "name": name_aus_orten(einzel), "aktiv": True, "empfaenger": [], **einzel}]
    for i, s in enumerate(suchen):
        s.setdefault("id", f"suche{i + 1}")
        s["id"] = str(s["id"])
        s.setdefault("name", name_aus_orten(s))
        s.setdefault("aktiv", True)
        s.setdefault("empfaenger", [])
    return cfg, suchen, quelle


def kriterien(suche: dict) -> dict:
    return {k: v for k, v in suche.items() if k not in META}


# ---------------------------------------------------------------- Zustand
def load_state(path: Path, erste_id: str) -> dict:
    st = json.loads(path.read_text()) if path.exists() else {}
    for k, v in {"gemeldet": {}, "verworfen": {}, "mails": {}, "fingerprints": {}, "postfach": {},
                 "portale": {}, "postfach_status": {}, "suchen": {}}.items():
        st.setdefault(k, v)
    # Übernahme aus dem früheren Einzelsuche-Format: bisherige Meldungen der ersten Suche zuordnen
    for k in ("gemeldet", "fingerprints"):
        st[k] = {(i if "|" in i else f"{erste_id}|{i}"): t for i, t in st[k].items()}
    if isinstance(st["verworfen"], list):
        st["verworfen"] = {erste_id: {"hash": st.pop("kriterien", ""), "ids": st["verworfen"]}}
    st.pop("kriterien", None)
    return st


def save_state(path: Path, st: dict, ids: set[str]) -> None:
    now = time.time()
    for k in ("gemeldet", "mails"):
        st[k] = {i: t for i, t in st[k].items() if t >= now - KEEP_DAYS * 86400}
    # Portalübergreifende Duplikate nur 30 Tage abgleichen (sonst Fehltreffer bei gleichem Preis)
    st["fingerprints"] = {i: t for i, t in st["fingerprints"].items() if t >= now - 30 * 86400}
    st["postfach"] = {i: d for i, d in st["postfach"].items() if d.get("_seit", 0) >= now - POSTFACH_TAGE * 86400}
    st["verworfen"] = {i: v for i, v in st["verworfen"].items() if i in ids}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=0, sort_keys=True))


# ---------------------------------------------------------------- Hilfen
_PLZ: dict[str, tuple[float, float] | None] = {}


def plz_mitte(plz: str) -> tuple[float, float] | None:
    """Ungefähre Lage über den Mittelpunkt der Postleitzahl (geo.admin.ch)."""
    if plz not in _PLZ:
        try:
            r = requests.get(flatfox.GEO, params={"searchText": plz, "type": "locations", "origins": "zipcode",
                                                  "sr": 4326, "limit": 1}, timeout=20)
            t = r.json().get("results") or []
            _PLZ[plz] = (t[0]["attrs"]["lat"], t[0]["attrs"]["lon"]) if t else None
        except Exception:
            _PLZ[plz] = None
    return _PLZ[plz]


def maskieren(adresse: str) -> str:
    """s.mueller@beispiel.ch -> s•••@b•••.ch (die Kartendaten sind öffentlich, daher nur angedeutet)."""
    try:
        name, dom = adresse.strip().split("@", 1)
        teile = dom.rsplit(".", 1)
        return f"{name[:1]}•••@{teile[0][:1]}•••.{teile[1]}" if len(teile) == 2 else f"{name[:1]}•••@•••"
    except ValueError:
        return "•••"


def group(items: list[Listing]) -> tuple[list[Listing], dict[str, list[Listing]]]:
    """Fasst dasselbe Objekt über verschiedene Portale zusammen (erstes Vorkommen gewinnt)."""
    out: list[Listing] = []
    also: dict[str, list[Listing]] = {}
    by_fp: dict[str, Listing] = {}
    by_extra: dict[str, Listing] = {}
    uids: set[str] = set()
    for l in items:
        if l.uid in uids:
            continue
        fp = l.fingerprint()
        first = (fp and by_fp.get(fp)) or next((by_extra[e] for e in l.extra_ids if e in by_extra), None)
        if first is not None and first.source != l.source:  # gleiche Quelle: eigenständige Inserate
            if l.uid not in {d.uid for d in also.get(first.uid, [])}:
                also.setdefault(first.uid, []).append(l)
            continue
        uids.add(l.uid)
        out.append(l)
        if fp and fp not in by_fp:
            by_fp[fp] = l
        for e in l.extra_ids:
            by_extra[e] = l
    return out, also


def dedupe(items: list[Listing], st: dict, sid: str = STANDARD_ID) -> tuple[list[Listing], dict[str, list[Listing]]]:
    """Nur für diese Suche noch nie gemeldete Inserate, portalübergreifend zusammengefasst."""
    fresh = [l for l in items if f"{sid}|{l.uid}" not in st["gemeldet"]
             and not (l.fingerprint() and f"{sid}|{l.fingerprint()}" in st["fingerprints"])]
    return group(fresh)


def postfach_passt(l: Listing, crit: dict) -> bool:
    """Prüft ein Inserat aus einer (bewusst breiten) Portal-Abo-Mail gegen die Suche: Lage, Preis, Zimmer,
    Fläche und Ausschlusswörter, soweit die Mail sie angibt. Die Lage ist oft nur der Mittelpunkt der
    Postleitzahl, daher etwas Spielraum."""
    if l.lat is not None:
        spiel = 1.5 if l.approx else 0.3
        if not any(o.get("lat") is not None and
                   flatfox.distance_km(o["lat"], o["lon"], l.lat, l.lon) <= float(o.get("umkreis_km", 3)) + spiel
                   for o in crit.get("orte") or []):
            return False
    titel = (l.title or "").lower()
    if any(re.search(r"(?<!\w)" + re.escape(w.lower()), titel) for w in crit.get("ausschluss_woerter") or []):
        return False
    for wert, lo, hi in ((l.price, "preis_min", "preis_max"), (l.rooms, "zimmer_min", "zimmer_max"),
                         (l.area, "flaeche_min", "flaeche_max")):
        if wert is not None and ((crit.get(lo) is not None and wert < crit[lo]) or
                                 (crit.get(hi) is not None and wert > crit[hi])):
            return False
    return True


def empfaenger_von(suche: dict, priv, standard: list[str]) -> tuple[list[str], list[str]]:
    """(Klartext-Adressen für den Versand, maskierte Adressen für die Karte)."""
    klar, masken = [], []
    for e in suche.get("empfaenger") or []:
        if isinstance(e, str):          # Klartext (z.B. von Hand eingetragen)
            klar.append(e); masken.append(maskieren(e))
        elif priv is not None and (a := schluessel.entschluesseln(priv, e.get("geheim", ""))):
            klar.append(a); masken.append(e.get("maske") or maskieren(a))
        else:
            log.warning("Suche %s: ein Empfänger konnte nicht entschlüsselt werden", suche["id"])
    if not klar:
        return standard, [maskieren(a) for a in standard]
    return klar, masken


def eintrag(l: Listing, also: dict[str, list[Listing]], erst: float, suchen_ids: list[str]) -> dict:
    d = {"id": l.uid, "q": l.source, "t": l.title, "u": l.url, "p": l.price, "z": l.rooms, "f": l.area,
         "plz": l.zipcode, "o": l.city, "s": l.street, "b": l.image, "v": l.available,
         "lat": round(l.lat, 6) if l.lat else None, "lon": round(l.lon, 6) if l.lon else None,
         "ca": 1 if l.approx else None, "e": datetime.fromtimestamp(erst, timezone.utc).date().isoformat(),
         "a": [{"q": d.source, "u": d.url} for d in also.get(l.uid, [])] or None, "su": suchen_ids}
    return {k: v for k, v in d.items() if v not in (None, "")}


# ---------------------------------------------------------------- Ablauf
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", help="bestimmte Konfigurationsdatei statt suche.yaml bzw. config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="keine Mail senden, nichts speichern")
    ap.add_argument("--html", help="Mailvorschau der ersten Suche in diese Datei schreiben")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    root = Path(args.root)
    cfg, suchen, quelle = load_config(root, args.config)
    notif, src = cfg.get("benachrichtigung", {}), cfg.get("quellen", {})
    log.info("%d Suche(n) aus %s", len(suchen), quelle)
    state_path = root / "state" / "state.json"
    st = load_state(state_path, suchen[0]["id"] if suchen else STANDARD_ID)

    user, pw = os.environ.get("MAIL_USER", ""), os.environ.get("MAIL_PASSWORD", "")
    standard = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
    priv, pub = schluessel.laden_oder_erzeugen(root / "schluessel", pw)
    jetzt = time.time()

    # --- Sammelpostfach (einmal für alle Suchen)
    pcfg = src.get("postfach") or {}
    new_mails: set[str] = set()
    if pcfg.get("aktiv") and user and pw:
        try:
            items, new_mails, stats = mailbox.fetch(pcfg, os.environ.get("IMAP_HOST", "imap.gmail.com"),
                                                    user, pw, set(st["mails"]))
            for l in items:
                if l.lat is None and l.zipcode and (m := plz_mitte(l.zipcode)):
                    l.lat, l.lon, l.approx = m[0], m[1], True
                st["postfach"].setdefault(l.uid, {**l.to_dict(), "_seit": jetzt})
            for dom, s in stats.items():
                alt = st["portale"].setdefault(dom, {"mails": 0, "inserate": 0, "letzte_mail": None})
                alt["mails"] += s["mails"]
                alt["inserate"] += s["inserate"]
                if s["letzte_mail"] and (not alt["letzte_mail"] or s["letzte_mail"] > alt["letzte_mail"]):
                    alt["letzte_mail"] = s["letzte_mail"]
            st["postfach_status"] = {"ok": True, "geprueft": jetzt, "fehler": ""}
        except Exception as e:
            log.exception("Postfach fehlgeschlagen")
            st["postfach_status"] = {"ok": False, "geprueft": jetzt, "fehler": str(e)[:200]}
    elif pcfg.get("aktiv"):
        log.warning("Postfach aktiv, aber MAIL_USER/MAIL_PASSWORD fehlen; übersprungen")
        st["postfach_status"] = {"ok": False, "geprueft": jetzt, "fehler": "Zugangsdaten fehlen"}
    postfach = [Listing.from_dict(d) for d in st["postfach"].values()]

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    karte = os.environ.get("KARTE_URL") or (f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else "")
    aktive = [s for s in suchen if s.get("aktiv", True)]
    karten_inserate: dict[str, dict] = {}
    suchen_info = []
    flatfox_anzahl = 0

    for suche in suchen:
        sid, crit = suche["id"], kriterien(suche)
        an, masken = empfaenger_von(suche, priv, standard)
        info = {"id": sid, "name": suche["name"], "aktiv": suche.get("aktiv", True), "empfaenger": masken,
                "kriterien": suche, "anzahl": 0, "neu": 0, **st["suchen"].get(sid, {})}
        if not suche.get("aktiv", True):
            suchen_info.append(info)
            continue
        errors: list[str] = []
        aktuell: list[Listing] = []
        h = hashlib.sha1(json.dumps(crit, sort_keys=True).encode()).hexdigest()
        vw = st["verworfen"].get(sid) or {}
        if vw.get("hash") != h:
            vw = {"hash": h, "ids": []}
        if src.get("flatfox", True):
            try:
                hits, rejected = flatfox.search(crit, set(vw["ids"]))
                aktuell += hits
                flatfox_anzahl += len(hits)
                vw["ids"] = sorted(set(vw["ids"]) | rejected)
            except Exception as e:  # eine fehlerhafte Quelle soll den Lauf nicht abbrechen
                log.exception("Flatfox fehlgeschlagen")
                errors.append(f"Flatfox: {e}")
        st["verworfen"][sid] = vw
        aktuell += [l for l in postfach if postfach_passt(l, crit)]

        new, also_new = dedupe(aktuell, st, sid)
        log.info("Suche «%s»: %d passend, %d neu", suche["name"], len(aktuell), len(new))
        shown = new[: int(notif.get("max_treffer_pro_mail", 60))]
        text, body = notify.render(shown, also_new, (karte + f"?suche={sid}") if karte else "", suche["name"])
        if len(new) > len(shown):
            note = f"… und {len(new) - len(shown)} weitere, alle auf der Karte."
            text += "\n" + note
            body = body.replace("<!--ende-->", f"<p>{note}</p><!--ende-->", 1)
        if errors:
            text += "\n\nHinweise: " + "; ".join(errors)
            body = body.replace("<!--ende-->", f'<p style="color:#b00">Hinweise: {"; ".join(errors)}</p><!--ende-->', 1)
        if args.html and suche is aktive[0]:
            Path(args.html).write_text(body)

        zugestellt = False
        if new and not args.dry_run:
            if an and user and pw:
                betreff = f"Immosuche «{suche['name']}»: {len(new)} neue Wohnung{'' if len(new) == 1 else 'en'}"
                notify.send(betreff, text, body, an, os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                            int(os.environ.get("SMTP_PORT", "465")), user, pw, os.environ.get("MAIL_FROM"))
                log.info("Suche «%s»: Mail an %d Empfänger versendet", suche["name"], len(an))
                zugestellt = True
            else:
                log.warning("Suche «%s»: keine Empfänger oder Zugangsdaten; nichts versendet", suche["name"])
        if args.dry_run:
            print(text)
        if zugestellt:  # ohne Versand beim nächsten Lauf erneut melden
            for l in new:
                st["gemeldet"][f"{sid}|{l.uid}"] = jetzt
                if fp := l.fingerprint():
                    st["fingerprints"][f"{sid}|{fp}"] = jetzt
            for dups in also_new.values():
                for d in dups:
                    st["gemeldet"].setdefault(f"{sid}|{d.uid}", jetzt)
            st["suchen"][sid] = {"letzte_mail": ts_iso(jetzt), "letzte_mail_anzahl": len(new)}

        alle, also_alle = group(aktuell)
        info.update({"anzahl": len(alle), "neu": len(new), **st["suchen"].get(sid, {}), "fehler": errors})
        suchen_info.append(info)
        for l in alle:
            erst = st["gemeldet"].get(f"{sid}|{l.uid}", jetzt)
            if l.uid in karten_inserate:
                k = karten_inserate[l.uid]
                k["su"].append(sid)
                k["e"] = min(k["e"], datetime.fromtimestamp(erst, timezone.utc).date().isoformat())
            else:
                karten_inserate[l.uid] = eintrag(l, also_alle, erst, [sid])

    if args.dry_run:
        return 0

    for m in new_mails:
        st["mails"][m] = jetzt
    ps = st["postfach_status"]
    quellen = [{"id": "flatfox", "name": "Flatfox", "art": "direkt", "link": "https://flatfox.ch", "ok": True,
                "inserate": flatfox_anzahl, "pflicht": True}]
    for dom, (name, link) in PORTALE.items():
        p = st["portale"].get(dom) or {}
        quellen.append({"id": dom, "name": name, "art": "suchabo", "link": link, "ok": bool(p.get("mails")),
                        "letzte_mail": ts_iso(p.get("letzte_mail")), "mails": p.get("mails", 0),
                        "inserate_gesamt": p.get("inserate", 0), "pflicht": dom in PFLICHT})
    daten = {
        "stand": ts_iso(jetzt), "quelle": quelle, "zeit": os.environ.get("VERSAND_ZEIT", "06:17"),
        "standard_empfaenger": [maskieren(a) for a in standard],
        "postfach": {"adresse": maskieren(user) if user else "", "ok": ps.get("ok", False),
                     "geprueft": ts_iso(ps.get("geprueft")), "fehler": ps.get("fehler", "")},
        "quellen": quellen, "schluessel": pub, "suchen": suchen_info,
        "inserate": sorted(karten_inserate.values(), key=lambda e: e["e"], reverse=True),
    }
    (root / "daten.json").write_text(json.dumps(daten, ensure_ascii=False, separators=(",", ":")))
    save_state(state_path, st, {s["id"] for s in suchen})
    return 0


if __name__ == "__main__":
    sys.exit(main())
