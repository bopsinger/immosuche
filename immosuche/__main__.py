"""Tageslauf: Quellen abfragen, Duplikate entfernen, neue Treffer melden.

Aufruf:  python -m immosuche [--dry-run] [--html vorschau.html] [--config config.yaml]
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
from pathlib import Path

import yaml

from . import flatfox, mailbox, notify
from .models import Listing

log = logging.getLogger("immosuche")
KEEP_DAYS = 180


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"gemeldet": {}, "verworfen": [], "kriterien": "", "mails": {}, "fingerprints": {}}


def save_state(path: Path, st: dict) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for k in ("gemeldet", "mails"):
        st[k] = {i: t for i, t in st[k].items() if t >= cutoff}
    # Portalübergreifende Duplikate nur 30 Tage abgleichen (sonst Fehltreffer bei gleichem Preis)
    st["fingerprints"] = {i: t for i, t in st["fingerprints"].items() if t >= time.time() - 30 * 86400}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=0, sort_keys=True))


def dedupe(items: list[Listing], st: dict) -> tuple[list[Listing], dict[str, list[Listing]]]:
    """Entfernt bereits gemeldete Inserate und fasst dasselbe Objekt über Portale zusammen."""
    out: list[Listing] = []
    also: dict[str, list[Listing]] = {}
    by_fp: dict[str, Listing] = {}
    by_extra: dict[str, Listing] = {}
    for l in items:
        if l.uid in st["gemeldet"]:
            continue
        fp = l.fingerprint()
        if fp and fp in st["fingerprints"]:          # bereits früher über ein anderes Portal gemeldet
            st["gemeldet"][l.uid] = time.time()
            continue
        first = (fp and by_fp.get(fp)) or next((by_extra[e] for e in l.extra_ids if e in by_extra), None)
        if first is not None and first.source != l.source:  # gleiche Quelle: eigenständige Inserate
            if l.uid not in {d.uid for d in also.get(first.uid, [])}:
                also.setdefault(first.uid, []).append(l)
            continue
        if any(o.uid == l.uid for o in out):
            continue
        out.append(l)
        if fp and fp not in by_fp:
            by_fp[fp] = l
        for e in l.extra_ids:
            by_extra[e] = l
    return out, also


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--dry-run", action="store_true", help="keine Mail senden, Zustand nicht speichern")
    ap.add_argument("--html", help="Mailvorschau in diese Datei schreiben")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text())
    crit, notif, src = cfg["suche"], cfg.get("benachrichtigung", {}), cfg.get("quellen", {})
    state_path = Path(args.state)
    st = load_state(state_path)

    crit_hash = hashlib.sha1(json.dumps(crit, sort_keys=True).encode()).hexdigest()
    if st.get("kriterien") != crit_hash:
        log.info("Suchkriterien geändert: verworfene Inserate werden neu geprüft")
        st["verworfen"], st["kriterien"] = [], crit_hash

    user, pw = os.environ.get("MAIL_USER", ""), os.environ.get("MAIL_PASSWORD", "")
    found: list[Listing] = []
    errors: list[str] = []

    if src.get("flatfox", True):
        try:
            known = set(st["gemeldet"]) | set(st["verworfen"])
            hits, rejected = flatfox.search(crit, known)
            found += hits
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
            found += items
        except Exception as e:
            log.exception("Postfach fehlgeschlagen")
            errors.append(f"Postfach: {e}")
    elif pcfg.get("aktiv"):
        log.warning("Postfach aktiv, aber MAIL_USER/MAIL_PASSWORD fehlen; übersprungen")

    new, also = dedupe(found, st)
    log.info("Insgesamt %d neue Treffer (%d Duplikate zusammengefasst)", len(new), sum(map(len, also.values())))

    shown = new[: int(notif.get("max_treffer_pro_mail", 60))]
    text, body = notify.render(shown, also)
    if len(new) > len(shown):
        note = f"\n… und {len(new) - len(shown)} weitere (werden beim nächsten Lauf nicht erneut gemeldet)."
        text += note
        body = body.replace("</div>", f"<p>{note.strip()}</p></div>", 1)
    if errors:
        text += "\n\nHinweise: " + "; ".join(errors)
        body = body.replace("</div>", f'<p style="color:#b00">Hinweise: {"; ".join(errors)}</p></div>', 1)
    if args.html:
        Path(args.html).write_text(body)

    should_send = bool(new) or not notif.get("nur_bei_treffern", True) or (errors and not new)
    if args.dry_run:
        print(text)
        return 0

    if should_send:
        to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
        if not (to and user and pw):
            log.error("MAIL_TO, MAIL_USER oder MAIL_PASSWORD fehlt; keine Mail versendet")
            return 1
        subject = notif.get("betreff", "Immosuche: {anzahl} neue Treffer").format(anzahl=len(new))
        if errors and not new:
            subject = "Immosuche: Fehler beim Tageslauf"
        notify.send(subject, text, body, to, os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                    int(os.environ.get("SMTP_PORT", "465")), user, pw, os.environ.get("MAIL_FROM"))
        log.info("Mail an %d Empfänger versendet", len(to))

    now = time.time()
    for l in new:
        st["gemeldet"][l.uid] = now
        if fp := l.fingerprint():
            st["fingerprints"][fp] = now
    for dups in also.values():
        for d in dups:
            st["gemeldet"][d.uid] = now
    for m in new_mails:
        st["mails"][m] = now
    save_state(state_path, st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
