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
- **Hinweis:** Partien werden lokal gespeichert, nicht zu BGG hochgeladen.

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

## ESP32 Touch AMOLED (ESPHome)
Ordner `esphome/`: `bgg-picker.yaml` (Hauptdatei), `ui.yaml` (Auswahl → Vorschlag → Ja/Nein → Standort + Start →
läuft/Beenden), `hardware_waveshare_amoled.yaml` (**Platzhalter** für Display/Touch deines Modells).
Entity-IDs in den `substitutions` anpassen, in HA beim Gerät „Aktionen ausführen" erlauben.
