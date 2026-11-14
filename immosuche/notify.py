"""Erstellt und versendet die tägliche Sammelmail."""
from __future__ import annotations

import html
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .models import Listing


def _fmt_price(p: float | None) -> str:
    return f"CHF {int(round(p)):,}".replace(",", "’") if p else "Preis auf Anfrage"


def _facts(l: Listing) -> str:
    parts = []
    if l.rooms:
        parts.append(f"{l.rooms:g} Zimmer")
    if l.area:
        parts.append(f"{int(l.area)} m²")
    parts.append(_fmt_price(l.price))
    if l.available:
        a = l.available
        if len(a) == 10 and a[4] == "-":  # 2026-12-01 -> 01.12.2026
            a = f"{a[8:10]}.{a[5:7]}.{a[:4]}"
        parts.append(f"ab {a}")
    return " · ".join(parts)


def _place(l: Listing) -> str:
    return ", ".join(x for x in (l.street, f"{l.zipcode} {l.city}".strip()) if x)


def render(listings: list[Listing], also_on: dict[str, list[Listing]]) -> tuple[str, str]:
    groups: dict[str, list[Listing]] = defaultdict(list)
    for l in listings:
        groups[l.source].append(l)

    h = ['<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;color:#1d1d1f">',
         f'<h2 style="margin:0 0 4px">{len(listings)} neue Treffer</h2>',
         f'<p style="margin:0 0 20px;color:#666">Quellen: {", ".join(sorted(groups))}</p>']
    t = [f"{len(listings)} neue Treffer\n"]
    for src in sorted(groups):
        h.append(f'<h3 style="border-bottom:1px solid #ddd;padding-bottom:4px;margin:24px 0 8px">{html.escape(src)}</h3>')
        t.append(f"\n== {src} ==")
        for l in sorted(groups[src], key=lambda x: x.price or 1e12):
            dup = also_on.get(l.uid) or []
            dup_html = "".join(f' · <a href="{html.escape(d.url)}" style="color:#666">auch auf {html.escape(d.source)}</a>' for d in dup)
            img = (f'<td width="120" valign="top" style="padding-right:12px"><a href="{html.escape(l.url)}">'
                   f'<img src="{html.escape(l.image)}" width="120" height="90" style="border-radius:6px;display:block;object-fit:cover" alt=""></a></td>') if l.image else ""
            h.append(
                f'<table cellpadding="0" cellspacing="0" style="margin:0 0 14px;width:100%"><tr>{img}<td valign="top">'
                f'<a href="{html.escape(l.url)}" style="font-weight:600;color:#0a58ca;text-decoration:none">{html.escape(l.title or "Inserat")}</a><br>'
                f'<span style="color:#333">{html.escape(_facts(l))}</span><br>'
                f'<span style="color:#666;font-size:13px">{html.escape(_place(l))}{dup_html}</span></td></tr></table>')
            t.append(f"- {l.title}\n  {_facts(l)} | {_place(l)}\n  {l.url}")
            t.extend(f"  auch auf {d.source}: {d.url}" for d in dup)
    h.append('<p style="color:#999;font-size:12px;margin-top:28px">Automatisch erstellt durch immosuche (GitHub Actions).</p></div>')
    return "\n".join(t), "".join(h)


def send(subject: str, text: str, html_body: str, recipients: list[str], host: str, port: int,
         user: str, password: str, sender: str | None = None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender or user
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="immosuche.local")
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
