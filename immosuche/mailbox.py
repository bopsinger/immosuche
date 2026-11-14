"""Liest Suchabo-Mails der Portale aus einem IMAP-Postfach und extrahiert Inserate."""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .models import Listing

log = logging.getLogger(__name__)

PORTAL_NAMES = {
    "homegate.ch": "Homegate", "immoscout24.ch": "ImmoScout24", "newhome.ch": "Newhome",
    "comparis.ch": "Comparis", "immostreet.ch": "ImmoStreet", "realadvisor.ch": "RealAdvisor",
    "tutti.ch": "Tutti", "anibis.ch": "Anibis", "immobilier.ch": "Immobilier.ch",
    "home.ch": "home.ch", "flatfox.ch": "Flatfox",
}
NON_LISTING = re.compile(
    r"abo|alert|unsubscribe|abmeld|desinscri|search|suche|recherche|setting|einstellung|profil|login|"
    r"account|konto|newsletter|privacy|datenschutz|agb|impressum|app-store|download|static|image|img|media",
    re.I)
ID_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")
_NUM = r"(\d{1,3}(?:[’' ,]\d{3})+|\d{3,7})(?:\.\d{2})?"
PRICE_RE = re.compile(r"(?:CHF|SFr\.?|Fr\.)\s*" + _NUM + r"|" + _NUM + r"\s*(?:\.[-–]|CHF|Fr\.)", re.I)
ROOMS_RE = re.compile(r"(\d{1,2}(?:[.,]5)?)\s*(?:-\s*)?(?:Zimmer|Zi\.?|Zr\.?|pièces|pces|rooms|locali)\b", re.I)
AREA_RE = re.compile(r"(\d{2,4})\s*(?:m²|m2|qm)", re.I)
ZIP_RE = re.compile(r"\b([1-9]\d{3})\s+([A-ZÄÖÜÉÈ][^\W\d_]+(?:[ \-](?:[A-ZÄÖÜÉÈ]|am|an|bei|im|sur)[^\W\d_]*)*)")
GENERIC_TEXT = re.compile(r"^(ansehen|details?|mehr|zum inserat|anzeigen|jetzt ansehen|voir|view|>|»)$", re.I)


def portal_of(host: str) -> str | None:
    host = host.lower().split(":")[0]
    for dom in PORTAL_NAMES:
        if host == dom or host.endswith("." + dom):
            return dom
    return None


def listing_id(url: str) -> tuple[str, str] | None:
    """(portal_domain, id) für eine direkte Inseratsadresse, sonst None."""
    u = urlparse(url)
    dom = portal_of(u.netloc)
    if not dom or NON_LISTING.search(u.path):
        return None
    ids = ID_RE.findall(u.path)
    return (dom, ids[-1]) if ids else None


def clean_url(url: str) -> str:
    """Entfernt Verfolgungsparameter (utm_*, Mail-Kennungen) aus der Adresse."""
    u = urlparse(url)
    q = "&".join(p for p in u.query.split("&") if p and not re.match(r"(utm_|mc_|cid=|ec_|tc=|trk)", p, re.I))
    return u._replace(query=q, fragment="").geturl()


class RedirectResolver:
    """Löst Klick-Verfolgungsadressen der Newsletter auf (nur Weiterleitungs-Kopfzeilen, kein Seitenabruf)."""

    def __init__(self, limit: int = 400):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (immosuche; Weiterleitung aufloesen)"
        self.cache: dict[str, str | None] = {}
        self.left = limit

    def resolve(self, url: str) -> str | None:
        if url in self.cache:
            return self.cache[url]
        result, cur = None, url
        for _ in range(6):
            if self.left <= 0:
                break
            self.left -= 1
            try:
                r = self.s.head(cur, allow_redirects=False, timeout=15)
                if r.status_code in (405, 403, 400):
                    r = self.s.get(cur, allow_redirects=False, timeout=15, stream=True)
                    r.close()
            except requests.RequestException:
                break
            loc = r.headers.get("Location")
            if not loc:
                break
            cur = requests.compat.urljoin(cur, loc)
            if listing_id(cur):
                result = cur
                break
        self.cache[url] = result
        return result


def _num(s: str) -> float | None:
    s = re.sub(r"[’'\s,]", "", s).rstrip(".-–")
    try:
        return float(s)
    except ValueError:
        return None


def _context(a) -> tuple[str, str]:
    """Text und erstes Bild des kleinsten umgebenden Blocks, der wie eine Inseratkarte aussieht."""
    node = a
    for _ in range(8):
        node = node.parent
        if node is None:
            break
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) > 40 and (PRICE_RE.search(text) or ROOMS_RE.search(text)):
            if len(text) > 900:
                break
            img = node.find("img")
            return text, (img.get("src") if img else "") or ""
    return " ".join(a.get_text(" ", strip=True).split()), ""


def parse_html(html: str, resolver: RedirectResolver | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[tuple[str, str], Listing] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        lid = listing_id(href)
        if not lid and resolver:
            target = resolver.resolve(href)
            if target:
                href, lid = target, listing_id(target)
        if not lid:
            continue
        dom, key = lid
        text, img = _context(a)
        anchor = " ".join(a.get_text(" ", strip=True).split())
        l = found.get(lid) or Listing(source=PORTAL_NAMES[dom], key=key, url=clean_url(href))
        if anchor and len(anchor) > len(l.title) and not GENERIC_TEXT.match(anchor) and len(anchor) < 160:
            l.title = anchor
        if img and not l.image and not img.lower().endswith(".gif"):
            l.image = img
        if l.price is None and (m := PRICE_RE.search(text)):
            l.price = _num(m.group(1) or m.group(2))
        if l.rooms is None and (m := ROOMS_RE.search(text)):
            l.rooms = _num(m.group(1))
        if l.area is None and (m := AREA_RE.search(text)):
            l.area = _num(m.group(1))
        if not l.zipcode and (m := ZIP_RE.search(text)):
            l.zipcode, l.city = m.group(1), m.group(2).strip()
        if not l.title:
            l.title = text[:120]
        found[lid] = l
    return list(found.values())


def _html_of(msg) -> str:
    html, plain = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/html", "text/plain") or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ct == "text/html":
            html += text
        else:
            plain += text
    if html:
        return html
    # Nur-Text-Mail: Adressen zu Links machen
    return re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', plain.replace("\n", "<br>\n"))


def fetch(cfg: dict, host: str, user: str, password: str, processed: set[str]) -> tuple[list[Listing], set[str]]:
    """Liefert (Inserate, neu verarbeitete Message-IDs)."""
    since = (date.today() - timedelta(days=int(cfg.get("tage_zurueck", 3)))).strftime("%d-%b-%Y")
    resolver = RedirectResolver()
    listings: list[Listing] = []
    done: set[str] = set()
    with imaplib.IMAP4_SSL(host) as M:
        M.login(user, password)
        M.select(f'"{cfg.get("ordner", "INBOX")}"', readonly=True)
        nums: set[bytes] = set()
        for dom in cfg.get("absender") or []:
            typ, data = M.search(None, "SINCE", since, "FROM", f'"{dom}"')
            if typ == "OK" and data and data[0]:
                nums.update(data[0].split())
        for n in sorted(nums, key=int):
            typ, data = M.fetch(n, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            mid = (msg.get("Message-ID") or f"num-{n.decode()}").strip()
            if mid in processed:
                continue
            subj = str(make_header(decode_header(msg.get("Subject", ""))))
            items = parse_html(_html_of(msg), resolver)
            log.info("Mail '%s': %d Inserate", subj[:70], len(items))
            listings.extend(items)
            done.add(mid)
    return listings, done
