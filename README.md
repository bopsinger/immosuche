# Immosuche: konsolidierte tägliche Immobiliensuche

Durchsucht einmal täglich mehrere Schweizer Immobilienquellen nach den Kriterien in
[`config.yaml`](config.yaml), entfernt Doppelte (dasselbe Objekt auf mehreren Portalen)
und schickt **eine** Sammelmail mit allen neuen Treffern an beliebig viele Adressen.
Bereits gemeldete Inserate werden nie ein zweites Mal verschickt.

**Karte:** https://bopsinger.github.io/immosuche/ (alle aktuell passenden Wohnungen,
Filter, Fotos; im Reiter *Meine Suche* Gebiete auf der Karte festlegen und Kriterien ändern).

## Quellen

| Quelle | Zugang |
|---|---|
| Flatfox | direkt über die öffentliche Flatfox-Schnittstelle |
| Homegate, ImmoScout24, Newhome, Comparis, ImmoStreet, RealAdvisor, Tutti, Anibis, Immobilier.ch | über die **portaleigenen Suchabos**, die an das Sammelpostfach gehen und hier ausgewertet werden |

Die grossen Portale sperren automatisierte Abfragen (Cloudflare/DataDome) und untersagen sie in
ihren Nutzungsbedingungen. Ihre eigenen E-Mail-Suchabos sind der zulässige und stabile Weg.

## Einrichtung (einmalig)

1. **Sammelpostfach**: ein Gmail-Konto (z.B. ein neues, nur dafür).
   In den Google-Kontoeinstellungen die Bestätigung in zwei Schritten aktivieren und unter
   *Sicherheit → App-Passwörter* ein App-Passwort erzeugen.
2. **Suchabos anlegen** auf Homegate, ImmoScout24, Newhome, Comparis (und weiteren Portalen nach Wunsch)
   mit denselben Kriterien wie in `config.yaml`, Benachrichtigung **täglich**, Empfänger = Sammelpostfach.
   Tipp: Comparis bündelt bereits viele kleinere Portale.
3. **GitHub-Secrets** setzen (*Settings → Secrets and variables → Actions*):

   | Secret | Inhalt |
   |---|---|
   | `MAIL_USER` | Adresse des Sammelpostfachs |
   | `MAIL_PASSWORD` | dessen App-Passwort |
   | `MAIL_TO` | Empfänger, kommagetrennt, z.B. `a@example.ch, b@example.ch` |
   | `SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `MAIL_FROM` | nur nötig, wenn nicht Gmail |

4. Unter *Actions → Tägliche Immobiliensuche → Run workflow* einmal manuell starten.
   Der erste Lauf meldet alle aktuell passenden Inserate, danach nur noch neue.

## Suchen verwalten

Alles auf der Karte (bopsinger.github.io/immosuche):

- **Suchen** (Startseite): alle Suchen mit Gebiet, Kriterien, Empfängern und Anzahl Treffer;
  **+ Neue Suche**, Bearbeiten, Pausieren, Löschen. Jede Suche hat eigene E-Mail-Empfänger
  (ohne Eintrag gilt das Secret `MAIL_TO`).
- **Wohnungen**: alle Treffer mit Fotos, filterbar nach Suche; Ortssuche mit «Suche anlegen».
- **Quellen**: Status je Portal (verbunden, letzte Suchabo-Mail) mit Anleitung zum Einrichten.

Beim ersten Speichern fragt die Karte einmalig nach einem GitHub-Zugangsschlüssel (nur dieses Repository,
Contents: Read and write; er bleibt im Browser). Gespeichert wird in `suche.yaml`; das startet sofort
einen Suchlauf, dessen Ergebnis die Karte direkt anzeigt.

**Datenschutz:** Das Repository ist öffentlich. E-Mail-Adressen werden deshalb im Browser mit dem
öffentlichen Schlüssel `schluessel/oeffentlich.pem` verschlüsselt; nur der Suchlauf kann sie lesen
(der private Schlüssel ist mit dem Secret `MAIL_PASSWORD` gesichert). Ändert sich dieses Passwort,
entsteht ein neuer Schlüssel und die Empfänger müssen neu eingetragen werden. Suchkriterien und
Kartendaten sind öffentlich einsehbar; die Seite ist für Suchmaschinen gesperrt.

## Lokal testen

```bash
pip install -r requirements.txt
python -m immosuche --dry-run --html vorschau.html
```

## Zeitplan

`.github/workflows/taeglich.yml`: täglich 05:17 UTC (06:17 Uhr im Winter, 07:17 Uhr im Sommer).
Bereits gemeldete Inserate stehen in `state/state.json`, die Kartendaten in `daten.json`;
beides aktualisiert der Lauf selbst.
