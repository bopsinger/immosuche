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


def test_schluessel_roundtrip(tmp_path):
    from immosuche import schluessel
    priv, pem = schluessel.laden_oder_erzeugen(tmp_path, "geheim")
    ct = schluessel.verschluesseln(pem, "a@b.ch")
    priv2, pem2 = schluessel.laden_oder_erzeugen(tmp_path, "geheim")   # erneut laden statt neu erzeugen
    assert pem2 == pem and schluessel.entschluesseln(priv2, ct) == "a@b.ch"
    priv3, pem3 = schluessel.laden_oder_erzeugen(tmp_path, "anderes")  # Passwort geändert -> neuer Schlüssel
    assert pem3 != pem and schluessel.entschluesseln(priv3, ct) is None


def test_alte_konfiguration_und_zustand(tmp_path):
    import json
    from immosuche.__main__ import load_config, load_state
    (tmp_path / "config.yaml").write_text("suche:\n  orte: [{name: 'Ort Thalwil (ZH)', umkreis_km: 5}]\n")
    cfg, suchen, quelle = load_config(tmp_path, None)
    assert suchen[0]["id"] == "standard" and suchen[0]["name"] == "Thalwil" and suchen[0]["aktiv"]
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"gemeldet": {"flatfox:1": 5}, "verworfen": ["flatfox:2"], "kriterien": "h"}))
    st = load_state(p, "standard")
    assert st["gemeldet"] == {"standard|flatfox:1": 5}
    assert st["verworfen"] == {"standard": {"hash": "h", "ids": ["flatfox:2"]}}


def test_empfaenger(tmp_path):
    from immosuche import schluessel
    from immosuche.__main__ import empfaenger_von
    priv, pem = schluessel.laden_oder_erzeugen(tmp_path, "x")
    s = {"id": "a", "empfaenger": [{"maske": "a•••@b•••.ch", "geheim": schluessel.verschluesseln(pem, "anna@beispiel.ch")}]}
    assert empfaenger_von(s, priv, ["std@x.ch"]) == (["anna@beispiel.ch"], ["a•••@b•••.ch"])
    assert empfaenger_von({"id": "b", "empfaenger": []}, priv, ["std@x.ch"]) == (["std@x.ch"], ["s•••@x•••.ch"])
