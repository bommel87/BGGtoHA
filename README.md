# BGG Collection Sync (Home Assistant / HACS)

## Test mit Claude geschrieben###

Synchronisiert deine BoardGameGeek-Sammlung **und Erweiterungen** nach Home Assistant (SQLite-Datenbank), mit
Sprache der gewählten Version, Verknüpfung Erweiterung ⇄ Basisspiel, **Standort / NFC-Tag / Spotify-Playlist /
Lichtprofil je Spiel**, **Partien-Logging** und einem **Spielvorschlag** (z. B. für ein ESP32-Touch-Display).

## Installation
1. Repo auf GitHub (Struktur: `custom_components/bgg_collection_sync/`, `hacs.json`, optional `esphome/`).
2. HACS → Benutzerdefinierte Repositories → URL, Kategorie *Integration* (privates Repo: Ordner manuell nach
   `/config/custom_components/` kopieren). Neustart.
3. *Geräte & Dienste → Integration hinzufügen → BGG Collection Sync*. BGG verlangt für die XML-API einen Token.
4. Optionen: Sync-Intervall, nur Besitz, Erweiterungen, **Wiedergabegerät** (Spotify-Player für Playlists),
   **Szene/Skript bei Spielende** (z. B. normales Licht).

> Update von 0.2.x: Datenbank wird migriert. Der erste Sync danach lädt für alle Spiele einmalig die Spielinfos
> (Spielerzahl, Dauer, Typ, Modus) nach und dauert länger.

## Zuordnungen je Spiel
```yaml
action: bgg_collection_sync.set_game_meta
data:
  name: Catan
  location: "Regal 2, Fach 3"
  spotify_uri: "https://open.spotify.com/playlist/..."   # oder spotify:playlist:...
  light_scene: scene.spieleabend_gemuetlich               # scene.* oder script.*
```
Felder weglassen = unverändert, leerer String = löschen. Die Zuordnungen bleiben bei jedem Sync erhalten.

**NFC:** Tag an der Schachtel scannen (HA-App / Tag-Leser), dann
`bgg_collection_sync.assign_scanned_tag` mit dem Spiel aufrufen (gilt für den Tag der letzten 10 Minuten) – oder
die Tag-ID aus *Einstellungen → Tags* als `nfc_tag` setzen. Danach gilt: **Tag scannen = Partie starten**,
nochmal scannen = Partie beenden.

## Partien
- Start (manuell): `start_play` mit Spiel + optional `players`. Aktiviert Lichtprofil und Playlist.
- Ende: `end_play` (optional `winner`, `note`, `players`); Dauer wird automatisch berechnet, Playlist pausiert,
  Szene/Skript bei Spielende läuft.
- `log_play` trägt eine Partie nachträglich ein, `cancel_play` verwirft die laufende, `get_plays` liest zurück.
- Events: `bgg_collection_sync_play_started` / `bgg_collection_sync_play_ended`.
- Tabellen: `plays` (läuft eine Partie, ist `ended_at` leer – übersteht Neustarts).
- Partien werden immer lokal gespeichert.

### Partien auf BGG loggen
In den Optionen der Integration das **BGG-Passwort** eintragen. Ist **automatisch auf BGG loggen** aktiv, landen
beendete (`end_play`, auch per NFC) und nachgetragene (`log_play`) Partien zusätzlich auf BGG. Pro Aufruf lässt sich
das mit `upload_to_bgg: true/false` übersteuern. Übertragen werden Spiel, Datum, Dauer, Notiz und der Gewinner als
Spieler.

Die offizielle XML-API kann nur lesen. Die Integration meldet sich deshalb wie die BGG-Webseite an und nutzt deren
Schnittstelle `geekplay.php`. Diese Schnittstelle ist inoffiziell und kann sich ändern. Schlägt der Upload fehl, z. B.
wegen Cloudflare oder eines falschen Passworts, bleibt die Partie lokal erhalten, und es erscheint eine
Benachrichtigung. Erneut versuchen mit `bgg_collection_sync.upload_play` und `play_id` (Feld `id` aus `get_plays`).
Bereits hochgeladene Partien haben eine `bgg_play_id` und werden nicht doppelt geloggt.

## Spielvorschlag
`suggest_game` (`players`, `mode` any/cooperative/competitive, `game_type`
any/family/strategy/party/abstract/thematic/wargame/children, `max_playtime`) mischt passende Spiele aus deinem
Besitz. `reject_suggestion` → nächster Vorschlag, `accept_suggestion` → Standort wird angezeigt,
`start_play` ohne Angabe startet das akzeptierte Spiel, `reset_suggestion` setzt zurück.
Kooperativ = Mechanik „Cooperative Game"; Wettkampf schließt semi-kooperative Spiele ein. Der Typ stammt aus
den BGG-Subdomains bzw. Kategorien. Spiele ohne diese Daten werden bei gesetztem Filter nicht vorgeschlagen.

## Entitäten
`Sammlung`, `Erweiterungen`, `Partien`, `Spielsitzung` (idle/running), `Vorschlag`
(idle/suggested/accepted/none, Attribute: name, location, summary …), `Letzte Synchronisierung`.

## Virtuelles Billy-Regal (Dashboard-Karte)
Ab 0.4.0 bringt die Integration die Karte `bgg-shelf-card` mit. Sie wird automatisch geladen, eine Ressource muss
nicht angelegt werden (nach dem Update einmal den Browser-Cache leeren bzw. die App neu laden).
Dashboard bearbeiten → *Karte hinzufügen* → *BGG Spieleregal*, oder per YAML:
```yaml
type: custom:bgg-shelf-card
title: Spieleregal
compartments: 6          # Fächer pro Billy (Standard 6, oberstes = Fach 1)
stack_size: 5            # Schachteln pro Stapel
shelves: [Regal 1, Regal 2, Wohnzimmer]   # optional: Reihenfolge, auch leere Regale
show_expansions: false   # Erweiterungen mit eigenem Standort mit einsortieren
```
Der Standort wird so gelesen: alles vor „Fach N“ ist der Regalname, N die Fachnummer von oben
(`Regal 2, Fach 3`, `Wohnzimmer Fach 1`). Ohne Fachnummer landet das Spiel unter dem Regal („ohne Fach“), ohne
Standort im Stapel „Ohne Standort“. Suche hebt Treffer hervor; ein Klick auf eine Schachtel zeigt Details,
erlaubt den Standort zu ändern (`set_game_meta`) und eine Partie zu starten.

### Status (Im Besitz / Zu verkaufen / Früher besessen)
Der Status kommt aus deiner BGG-Sammlung: „Zu verkaufen“ (*For Trade*) bleibt im Regal (€-Schild) und erscheint
zusätzlich im Bereich *Zu verkaufen*; „Früher besessen“ (*Previously Owned*) steht nicht mehr im Regal, sondern im
Bereich *Früher besessen*; auf BGG gelöschte Spiele verschwinden.

Ändern: im Fenster eines Spiels **Auf BGG ändern** öffnet das Spiel auf BGG. Dort den Sammlungsstatus anpassen –
nach dem nächsten Sync (oder `bgg_collection_sync.sync_now`) zeigt das Regal den neuen Stand. Die BGG-API kann nur
lesen, und die BGG-Webseite blockt automatische Änderungen, deshalb gibt es kein direktes Zurückschreiben.

## ESP32 Touch AMOLED (ESPHome)
Ordner `esphome/`: `bgg-picker.yaml` (Hauptdatei), `ui.yaml` (Auswahl → Vorschlag → Ja/Nein → Standort + Start →
läuft/Beenden), `hardware_waveshare_amoled.yaml` (**Platzhalter** für Display/Touch deines Modells).
Entity-IDs in den `substitutions` anpassen, in HA beim Gerät „Aktionen ausführen" erlauben.
