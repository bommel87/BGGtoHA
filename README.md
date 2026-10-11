# BGG Collection Sync (Home Assistant / HACS)

Synchronisiert deine BoardGameGeek-Sammlung nach Home Assistant und speichert sie in einer SQLite-Datenbank.
Pro Spiel wird die **Sprache der in der Collection ausgewählten Version** mitgespeichert und angezeigt.

## Installation
1. Repository auf GitHub hochladen (Struktur: `custom_components/bgg_collection_sync/` + `hacs.json`).
2. HACS → Drei-Punkte-Menü → *Benutzerdefinierte Repositories* → URL eintragen, Kategorie *Integration*.
3. Installieren, Home Assistant neu starten.
4. *Einstellungen → Geräte & Dienste → Integration hinzufügen → BGG Collection Sync*.

> **API-Token:** BGG verlangt für die XML-API inzwischen einen Token. Registriere dafür eine Anwendung in
> deinem BGG-Konto und trage den Token bei der Einrichtung ein.

## Was du bekommst
- `sensor.boardgamegeek_<name>_sammlung` – Anzahl Spiele. Attribute: `languages` (Anzahl je Sprache) und
  `games` (Name, Jahr, Sprache, Version).
- `sensor.…_letzte_synchronisierung` – Zeitpunkt des letzten Syncs.
- Datenbank: `<config>/bgg_collection_sync.db` (Tabellen `games`, `versions`).
- Services: `bgg_collection_sync.sync_now`, `bgg_collection_sync.get_games` (mit optionalem Sprachfilter).
- Optionen: Sync-Intervall (Standard 12 h), nur Spiele im Besitz.

## Beispiel: Markdown-Karte
```yaml
type: markdown
title: Meine Spiele
content: >
  {% for g in state_attr('sensor.boardgamegeek_DEIN_NAME_sammlung', 'games') %}
  - **{{ g.name }}** ({{ g.year }}) – {{ g.language or 'Sprache unbekannt' }}
  {% endfor %}
```

## Beispiel: nur deutsche Versionen
```yaml
action: bgg_collection_sync.get_games
data:
  language: German
response_variable: spiele
```

## Datenbank direkt abfragen
```sql
SELECT name, year, version_name, version_language FROM games ORDER BY name;
```
