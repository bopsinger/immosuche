"""Tageslauf: Quellen abfragen, Duplikate entfernen, neue Treffer melden, Kartendaten schreiben.

Aufruf:  python -m immosuche [--dry-run] [--html vorschau.html]
Kriterien: neueste Datei in suche/*.yaml (von der Karte erzeugt), sonst config.yaml.
Umgebungsvariablen (GitHub-Secrets):
  MAIL_USER, MAIL_PASSWORD   Konto für Versand (SMTP) und Sammelpostfach (IMAP)
  MAIL_TO                    Empfänger, kommagetrennt
  SMTP_HOST (smtp.gmail.com), SMTP_PORT (465), IMAP_HOST (imap.gmail.com), MAIL_FROM (optional)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import flatfox, mailbox, notify
from .models import Listing

log = logging.getLogger("immosuche")
KEEP_DAYS = 180
POSTFACH_TAGE = 21   # Inserate aus Portal-Mails so lange auf der Karte zeigen


def load_config(root: Path, explicit: str | None) -> tuple[dict, str]:
    """Grundeinstellung aus config.yaml; der Abschnitt "suche" kommt (falls vorhanden) aus
    suche.yaml (von der Karte gespeichert), sonst aus der neuesten Datei in suche/."""
    if explicit:
        return yaml.safe_load(Path(explicit).read_text()), explicit
    cfg = yaml.safe_load((root / "config.yaml").read_text())
    kandidaten = [root / "suche.yaml"] + sorted((root / "suche").glob("*.yaml"), reverse=True)
    for f in kandidaten:
        if f.is_file():
            neu = yaml.safe_load(f.read_text()) or {}
            cfg["suche"] = neu.get("suche", neu)   # Datei darf nur den Abschnitt "suche" enthalten
            return cfg, str(f.relative_to(root))
    return cfg, "config.yaml"


def maskieren(adresse: str) -> str:
    """s.mueller@beispiel.ch -> s•••@b•••.ch (Empfänger sind öffentlich sichtbar, daher nur angedeutet)."""
    try:
        name, dom = adresse.strip().split("@", 1)
        teile = dom.rsplit(".", 1)
        return f"{name[:1]}•••@{teile[0][:1]}•••.{teile[1]}" if len(teile) == 2 else f"{name[:1]}•••@•••"
    except ValueError:
        return "•••"


def load_state(path: Path) -> dict:
    st = json.loads(path.read_text()) if path.exists() else {}
    for k, v in {"gemeldet": {}, "verworfen": [], "kriterien": "", "mails": {},
                 "fingerprints": {}, "postfach": {}}.items():
        st.setdefault(k, v)
    return st


def save_state(path: Path, st: dict) -> None:
    now = time.time()
    for k in ("gemeldet", "mails"):
        st[k] = {i: t for i, t in st[k].items() if t >= now - KEEP_DAYS * 86400}
    # Portalübergreifende Duplikate nur 30 Tage abgleichen (sonst Fehltreffer bei gleichem Preis)
    st["fingerprints"] = {i: t for i, t in st["fingerprints"].items() if t >= now - 30 * 86400}
    st["postfach"] = {i: d for i, d in st["postfach"].items() if d.get("_seit", 0) >= now - POSTFACH_TAGE * 86400}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=0, sort_keys=True))


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


def dedupe(items: list[Listing], st: dict) -> tuple[list[Listing], dict[str, list[Listing]]]:
    """Nur neue Inserate (noch nie gemeldet), portalübergreifend zusammengefasst."""
    fresh = [l for l in items if l.uid not in st["gemeldet"]
             and not (l.fingerprint() and l.fingerprint() in st["fingerprints"])]
    return group(fresh)


def kartendaten(listings: list[Listing], also: dict[str, list[Listing]], st: dict, crit: dict, quelle: str,
                versand: dict | None = None) -> dict:
    def eintrag(l: Listing) -> dict:
        d = {"id": l.uid, "q": l.source, "t": l.title, "u": l.url, "p": l.price, "z": l.rooms, "f": l.area,
             "plz": l.zipcode, "o": l.city, "s": l.street, "b": l.image, "v": l.available,
             "lat": round(l.lat, 6) if l.lat else None, "lon": round(l.lon, 6) if l.lon else None,
             "ca": 1 if l.approx else None,
             "e": datetime.fromtimestamp(st["gemeldet"].get(l.uid, time.time()), timezone.utc).date().isoformat(),
             "a": [{"q": d.source, "u": d.url} for d in also.get(l.uid, [])] or None}
        return {k: v for k, v in d.items() if v not in (None, "")}
    return {"stand": datetime.now(timezone.utc).isoformat(timespec="minutes"), "quelle": quelle,
            "kriterien": crit, "versand": versand or {}, "inserate": sorted((eintrag(l) for l in listings), key=lambda e: e["e"], reverse=True)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", help="bestimmte Konfigurationsdatei statt suche/ bzw. config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="keine Mail senden, nichts speichern")
    ap.add_argument("--html", help="Mailvorschau in diese Datei schreiben")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    root = Path(args.root)
    cfg, quelle = load_config(root, args.config)
    crit, notif, src = cfg["suche"], cfg.get("benachrichtigung", {}), cfg.get("quellen", {})
    log.info("Kriterien aus %s", quelle)
    state_path = root / "state" / "state.json"
    st = load_state(state_path)

    crit_hash = hashlib.sha1(json.dumps(crit, sort_keys=True).encode()).hexdigest()
    if st["kriterien"] != crit_hash:
        log.info("Suchkriterien geändert: verworfene Inserate werden neu geprüft")
        st["verworfen"], st["kriterien"] = [], crit_hash

    user, pw = os.environ.get("MAIL_USER", ""), os.environ.get("MAIL_PASSWORD", "")
    aktuell: list[Listing] = []   # alles, was derzeit passt (für die Karte)
    errors: list[str] = []

    if src.get("flatfox", True):
        try:
            hits, rejected = flatfox.search(crit, set(st["verworfen"]))
            aktuell += hits
            st["verworfen"] = sorted(set(st["verworfen"]) | rejected)
        except Exception as e:  # eine fehlerhafte Quelle soll den Lauf nicht abbrechen
            log.exception("Flatfox fehlgeschlagen")
            errors.append(f"Flatfox: {e}")

    pcfg = src.get("postfach") or {}
    new_mails: set[str] = set()
    if pcfg.get("aktiv") and user and pw:
        try:
            items, new_mails = mailbox.fetch(pcfg, os.environ.get("IMAP_HOST", "imap.gmail.com"),
                                             user, pw, set(st["mails"]))
            now = time.time()
            for l in items:
                if l.lat is None and l.zipcode and (m := plz_mitte(l.zipcode)):
                    l.lat, l.lon, l.approx = m[0], m[1], True
                st["postfach"].setdefault(l.uid, {**l.to_dict(), "_seit": now})
        except Exception as e:
            log.exception("Postfach fehlgeschlagen")
            errors.append(f"Postfach: {e}")
    elif pcfg.get("aktiv"):
        log.warning("Postfach aktiv, aber MAIL_USER/MAIL_PASSWORD fehlen; übersprungen")
    aktuell += [Listing.from_dict(d) for d in st["postfach"].values()]

    new, also_new = dedupe(aktuell, st)
    log.info("%d aktuell passend, davon %d neu", len(aktuell), len(new))

    shown = new[: int(notif.get("max_treffer_pro_mail", 60))]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    karte = os.environ.get("KARTE_URL") or (f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else "")
    text, body = notify.render(shown, also_new, karte)
    if len(new) > len(shown):
        note = f"… und {len(new) - len(shown)} weitere, alle auf der Karte."
        text += "\n" + note
        body = body.replace("<!--ende-->", f"<p>{note}</p><!--ende-->", 1)
    if errors:
        text += "\n\nHinweise: " + "; ".join(errors)
        body = body.replace("<!--ende-->", f'<p style="color:#b00">Hinweise: {"; ".join(errors)}</p><!--ende-->', 1)
    if args.html:
        Path(args.html).write_text(body)

    if args.dry_run:
        print(text)
        return 0

    should_send = bool(new) or not notif.get("nur_bei_treffern", True) or bool(errors)
    zugestellt = not should_send
    if should_send:
        to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
        if not (to and user and pw):
            log.warning("MAIL_TO, MAIL_USER oder MAIL_PASSWORD fehlt; keine Mail versendet")
        else:
            subject = notif.get("betreff", "Immosuche: {anzahl} neue Treffer").format(anzahl=len(new))
            if errors and not new:
                subject = "Immosuche: Fehler beim Tageslauf"
            notify.send(subject, text, body, to, os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                        int(os.environ.get("SMTP_PORT", "465")), user, pw, os.environ.get("MAIL_FROM"))
            log.info("Mail an %d Empfänger versendet", len(to))
            zugestellt = True

    now = time.time()
    for l in (new if zugestellt else []):  # ohne Versand beim nächsten Lauf erneut melden
        st["gemeldet"][l.uid] = now
        if fp := l.fingerprint():
            st["fingerprints"][fp] = now
    for dups in (also_new.values() if zugestellt else []):
        for d in dups:
            st["gemeldet"].setdefault(d.uid, now)
    for m in new_mails:
        st["mails"][m] = now

    alle, also_alle = group(aktuell)
    versand = {"an": [maskieren(a) for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()],
                "zeit": os.environ.get("VERSAND_ZEIT", "06:17"), "nur_bei_treffern": notif.get("nur_bei_treffern", True),
                "letzter_lauf": {"neu": len(new), "mail": bool(new) and zugestellt and should_send}}
    (root / "daten.json").write_text(json.dumps(kartendaten(alle, also_alle, st, crit, quelle, versand),
                                                ensure_ascii=False, separators=(",", ":")))
    save_state(state_path, st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
