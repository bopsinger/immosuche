from immosuche.mailbox import listing_id, parse_html, clean_url
from immosuche.__main__ import dedupe

HOMEGATE = """<html><body><table>
<tr><td><a href="https://www.homegate.ch/mieten/4002345678?utm_source=alert&utm_medium=email"><img src="https://media2.homegate.ch/x.jpg"></a></td>
<td><a href="https://www.homegate.ch/mieten/4002345678?utm_source=alert">Helle 3.5-Zimmer-Wohnung mit Balkon</a><br>
CHF 2’450.– · 3.5 Zimmer · 82 m²<br>Seefeldstrasse 12, 8008 Zürich</td></tr>
<tr><td><a href="https://www.homegate.ch/mieten/4002345679">Ansehen</a></td><td>4 Zimmer, 95 m2, CHF 2'890, 8004 Zürich</td></tr>
<tr><td><a href="https://www.homegate.ch/de/suchabo/abmelden/123456789">Abmelden</a></td></tr>
</table></body></html>"""

IS24 = """<div><div class="card"><a href="https://www.immoscout24.ch/rent/4002345678">3.5 Zimmer Wohnung in Zürich</a>
<p>8008 Zürich</p><p>3.5 Zimmer | 82 m² | CHF 2,450.-</p></div>
<a href="https://www.immoscout24.ch/de/immobilien/mieten/ort-zuerich?searchId=99999999">Alle Ergebnisse</a></div>"""


def test_listing_id():
    assert listing_id("https://www.homegate.ch/mieten/4002345678?x=1") == ("homegate.ch", "4002345678")
    assert listing_id("https://www.comparis.ch/immobilien/marktplatz/details/show/32145678") == ("comparis.ch", "32145678")
    assert listing_id("https://www.homegate.ch/de/suchabo/abmelden/123456789") is None
    assert listing_id("https://example.com/12345678") is None


def test_parse_homegate():
    ls = {l.key: l for l in parse_html(HOMEGATE)}
    assert set(ls) == {"4002345678", "4002345679"}
    a = ls["4002345678"]
    assert a.source == "Homegate" and a.title.startswith("Helle 3.5")
    assert (a.price, a.rooms, a.area, a.zipcode) == (2450, 3.5, 82, "8008")
    assert a.image.endswith("x.jpg") and "utm_" not in a.url
    b = ls["4002345679"]
    assert (b.price, b.rooms, b.zipcode) == (2890, 4, "8004")


def test_cross_portal_dedupe():
    items = parse_html(HOMEGATE) + parse_html(IS24)
    st = {"gemeldet": {}, "fingerprints": {}}
    new, also = dedupe(items, st)
    assert len(new) == 2
    assert [d.source for d in also["homegate:4002345678"]] == ["ImmoScout24"]


def test_clean_url():
    assert clean_url("https://x.ch/a?utm_source=1&id=5#f") == "https://x.ch/a?id=5"
