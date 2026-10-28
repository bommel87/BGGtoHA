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

## Mitspieler
Für jeden Mitspieler gibt es einen Sensor `sensor.<gerät>_mitspieler_<name>`. Sein Zustand ist die Zahl der
gemeinsamen Partien. Als Attribute kommen `bgg_username`, `bgg_profile`, `wins`, `win_rate` (in %), `last_played`
und `top_games` dazu.

- **Aus BGG:** Die Integration lädt deine auf BGG geloggten Partien und liest die Mitspieler aus. Nach dem Start
  gleicht sie alle Partien im Hintergrund ab, bei jedem Sync danach nur die neuen. Ein vollständiger Abgleich geht
  jederzeit mit `sync_players`. Spieler mit BGG-Account werden über den Account zusammengeführt, auch wenn ihr Name
  unterschiedlich geschrieben ist. Du selbst tauchst nicht auf.
- **Selbst anlegen und verknüpfen:** `set_player` mit `name` und optional `bgg_username` (wird bei BGG geprüft).
  Damit legst du neue Mitspieler an oder verknüpfst einen reinen Namen aus deinen BGG-Partien, zum Beispiel „Ben“,
  mit dessen BGG-Account. `bgg_username` leer lassen löst die Verknüpfung. `remove_player` entfernt einen selbst
  angelegten Eintrag. `get_players` liefert alle Mitspieler.
- **Beim Spielen:** `end_play` und `log_play` haben das Feld `player_names` (Namen oder BGG-Benutzernamen, ohne dich).
  Bekannte Mitspieler werden beim Upload mit ihrem BGG-Account geloggt, die Partie erscheint dann auch bei ihnen.
  Du wirst automatisch als Spieler ergänzt. `winner` kann einer der Mitspieler oder dein BGG-Name sein.
  `players` wird aus der Liste berechnet, wenn es nicht angegeben ist. Auch `start_play` nimmt `player_names` an.
  `end_play` übernimmt diese Mitspieler, wenn du beim Beenden keine anderen angibst.

### Partie-Kachel
```yaml
type: custom:bgg-play-card
```
Die Kachel zeigt die laufende Partie mit Laufzeit. Mitspieler wählst du per Antippen aus, neue Namen kommen über
„+ Name“ dazu. Außerdem gibt es eine Auswahl für den Gewinner, ein Notizfeld und die Knöpfe **Beenden** und
**Verwerfen**. Die Mitspieler aus dem Start sind vorausgewählt. Im Fenster eines Spiels in der Regalkarte wählst du
die Mitspieler schon vor **Partie starten** aus. Soll die Kachel nur bei laufender Partie erscheinen, packst du sie
in eine `conditional`-Karte mit `state: running` auf dem Sitzungs-Sensor.

**Punkte:** In der Kachel trägst du Punkte für dich und jeden Mitspieler ein. Bleibt der Gewinner auf
„Automatisch“, gewinnt die höchste Punktzahl, bei Gleichstand gewinnen mehrere. Die Punkte gehen mit zu BGG. Per
Dienst geht das über `scores` bei `end_play` und `log_play`, zum Beispiel `{"Anna": 42, "bommel87": 38}`. Dein
BGG-Name steht dabei für dich.

### Partie in BG Stats übernehmen (QR-Code)
Nach **Beenden** zeigt die Kachel einen QR-Code und den Knopf **In BG Stats öffnen**. Scannst du den Code mit dem
Handy oder tippst den Link auf dem Handy an, öffnet BG Stats den Import-Dialog mit Spiel, Datum, Dauer, Spielern,
Punkten, Gewinner und Notiz. Dafür nutzt die Integration das offizielle Link-Format von BG Stats (`createPlay`).
Für ältere Partien gibt es den Knopf **Letzte Partie an BG Stats** oder den Dienst `bgstats_link` mit `play_id`.
Der Dienst liefert `url` und `qr_svg`. Mit `bgstats: false` in der Kachel blendest du das aus.

Im Fenster eines Spiels in der Regalkarte zeigt **QR-Code: neue Partie in BG Stats** einen Code, der in BG Stats eine
neue Partie mit diesem Spiel öffnet. Als Dienst heißt das `bgstats_game_qr`.

### Zuordnungen im Spiel-Fenster
Im Fenster eines Spiels in der Regalkarte siehst und änderst du neben dem Standort auch **NFC-Tag**,
**Spotify-Playlist** und **Lichtprofil** (Szene oder Skript). **Gescannten übernehmen** ordnet den Tag zu, der in den
letzten 10 Minuten gescannt wurde. **Speichern** übernimmt alle Felder, ein leeres Feld löscht die Zuordnung.

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
