"""Partien, Metadaten, NFC-Tags und Spielvorschläge."""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.components import persistent_notification
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import BggAuthError, BggError, BggWebClient
from .const import (
    CONF_END_SCENE,
    CONF_MEDIA_PLAYER,
    CONF_PASSWORD,
    CONF_UPLOAD_PLAYS,
    DEFAULT_UPLOAD_PLAYS,
    DOMAIN,
)
from .logic import filter_games, normalize_spotify, spotify_media_type, summary_line

if TYPE_CHECKING:
    from .coordinator import BggCoordinator

_LOGGER = logging.getLogger(__name__)

TAG_MAX_AGE = timedelta(minutes=10)
_SCENE_RE = re.compile(r"^(scene|script)\.[a-z0-9_]+$")


class BggManager:
    """Hält Vorschlags- und Sitzungslogik; State lebt in Coordinator + DB."""

    def __init__(self, hass: HomeAssistant, coordinator: BggCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.suggestion: dict[str, Any] = {"phase": "idle"}
        self._queue: list[int] = []
        self._filters: dict[str, Any] = {}
        self._last_tag: tuple[str, datetime] | None = None
        self._started_playback = False
        self._unsub = None
        self._web_client: BggWebClient | None = None

    # ---- Lebenszyklus -----------------------------------------------------
    @callback
    def async_start(self) -> None:
        self._unsub = self.hass.bus.async_listen("tag_scanned", self._handle_tag)

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # ---- Helfer -----------------------------------------------------------
    @property
    def _username(self) -> str:
        return self.coordinator.client.username

    @property
    def _options(self) -> dict[str, Any]:
        return self.coordinator.config_entry.options

    def _pool(self) -> list[dict[str, Any]]:
        data = self.coordinator.data
        return data["games"] + data["expansions"] + data.get("prev_owned", [])

    def find_game(self, bgg_id: int | None = None, name: str | None = None) -> dict[str, Any]:
        pool = self._pool()
        if bgg_id is not None:
            for game in pool:
                if game["bgg_id"] == bgg_id:
                    return game
            raise ServiceValidationError(f"BGG-ID {bgg_id} ist nicht in der Sammlung")
        if name:
            needle = name.strip().lower()
            exact = [g for g in pool if g["name"].lower() == needle]
            if len(exact) == 1:
                return exact[0]
            part = [g for g in pool if needle in g["name"].lower()]
            if len(part) == 1:
                return part[0]
            if not part:
                raise ServiceValidationError(f"Kein Spiel mit dem Namen '{name}' gefunden")
            raise ServiceValidationError(
                "Name nicht eindeutig: " + ", ".join(g["name"] for g in part[:5])
            )
        raise ServiceValidationError("bgg_id oder name angeben")

    async def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=False)
        except HomeAssistantError as err:
            _LOGGER.warning("Aufruf %s.%s fehlgeschlagen: %s", domain, service, err)

    # ---- Metadaten --------------------------------------------------------
    async def async_set_meta(self, game: dict[str, Any], fields: dict[str, str]) -> None:
        fields = {k: (v or "").strip() for k, v in fields.items()}
        if "spotify_uri" in fields:
            try:
                fields["spotify_uri"] = normalize_spotify(fields["spotify_uri"])
            except ValueError as err:
                raise ServiceValidationError(f"Ungültiger Spotify-Link/URI: {err}") from err
        scene = fields.get("light_scene")
        if scene and not _SCENE_RE.match(scene):
            raise ServiceValidationError("light_scene muss eine scene.* oder script.* Entität sein")
        tag = fields.get("nfc_tag")
        if tag:
            for other in self._pool():
                if other["bgg_id"] != game["bgg_id"] and other["nfc_tag"].lower() == tag.lower():
                    raise ServiceValidationError(
                        f"Der Tag ist bereits '{other['name']}' zugeordnet"
                    )
        await self.hass.async_add_executor_job(
            self.coordinator.db.set_meta, self._username, game["bgg_id"], fields
        )
        await self.coordinator.async_refresh_local()

    async def async_assign_scanned_tag(self, game: dict[str, Any]) -> str:
        if self._last_tag is None or dt_util.utcnow() - self._last_tag[1] > TAG_MAX_AGE:
            raise ServiceValidationError(
                "Kein Tag in den letzten 10 Minuten gescannt - Tag zuerst scannen"
            )
        tag_id = self._last_tag[0]
        await self.async_set_meta(game, {"nfc_tag": tag_id})
        return tag_id

    # ---- NFC --------------------------------------------------------------
    @callback
    def _handle_tag(self, event: Event) -> None:
        tag_id = event.data.get("tag_id")
        if not tag_id:
            return
        self._last_tag = (str(tag_id), dt_util.utcnow())
        self.hass.async_create_task(self._process_tag(str(tag_id)))

    async def _process_tag(self, tag_id: str) -> None:
        game = next(
            (g for g in self._pool() if g["nfc_tag"] and g["nfc_tag"].lower() == tag_id.lower()),
            None,
        )
        if game is None:
            return
        active = self.coordinator.data["active_play"]
        try:
            if active is None:
                await self.async_start_play(game)
            elif active["bgg_id"] == game["bgg_id"]:
                await self.async_end_play()
            else:
                _LOGGER.warning(
                    "Tag von '%s' gescannt, aber '%s' läuft noch", game["name"], active["game_name"]
                )
        except ServiceValidationError as err:
            _LOGGER.warning("Tag-Aktion fehlgeschlagen: %s", err)

    # ---- Partien ----------------------------------------------------------
    async def async_start_play(
        self,
        game: dict[str, Any] | None = None,
        players: int | None = None,
        player_names: list[str] | None = None,
    ) -> dict[str, Any]:
        if game is None:
            s = self.suggestion
            if s.get("phase") != "accepted":
                raise ServiceValidationError(
                    "Kein Spiel angegeben und kein akzeptierter Vorschlag vorhanden"
                )
            game = self.find_game(bgg_id=s["bgg_id"])
            players = players or s.get("players")
        active = self.coordinator.data["active_play"]
        if active:
            raise ServiceValidationError(f"Es läuft bereits ein Spiel: {active['game_name']}")

        lineup = self.resolve_players(player_names, None)
        players = players or (len(lineup) + 1 if lineup else None)
        now = dt_util.utcnow()
        await self.hass.async_add_executor_job(
            self.coordinator.db.start_play,
            self._username,
            game["bgg_id"],
            game["name"],
            players,
            now.isoformat(),
            lineup,
        )
        await self._apply_start(game)
        self._reset_suggestion_state()
        await self.coordinator.async_refresh_local()
        self.hass.bus.async_fire(
            f"{DOMAIN}_play_started",
            {
                "bgg_id": game["bgg_id"],
                "name": game["name"],
                "location": game["location"],
                "players": players,
                "player_names": [p["name"] for p in lineup],
            },
        )
        return {"game": game["name"], "location": game["location"]}

    async def _apply_start(self, game: dict[str, Any]) -> None:
        scene = game["light_scene"]
        if scene:
            await self._call(scene.split(".")[0], "turn_on", {"entity_id": scene})
        player = self._options.get(CONF_MEDIA_PLAYER)
        uri = game["spotify_uri"]
        if uri and player:
            await self._call(
                "media_player",
                "play_media",
                {
                    "entity_id": player,
                    "media_content_id": uri,
                    "media_content_type": spotify_media_type(uri),
                },
            )
            self._started_playback = True

    async def async_end_play(
        self,
        winner: str | None = None,
        note: str | None = None,
        players: int | None = None,
        upload: bool | None = None,
        player_names: list[str] | None = None,
    ) -> dict[str, Any]:
        active = self.coordinator.data["active_play"]
        if not active:
            raise ServiceValidationError("Es läuft aktuell kein Spiel")
        ended = dt_util.utcnow()
        started = dt_util.parse_datetime(active["started_at"]) or ended
        duration = max(1, round((ended - started).total_seconds() / 60))
        if player_names is None:  # beim Start gewählte Mitspieler übernehmen
            player_names = [p["name"] for p in active["player_names"]]
        lineup = self.resolve_players(player_names, winner)
        players = players or (len(lineup) + 1 if lineup else active["players"])
        await self.hass.async_add_executor_job(
            self.coordinator.db.end_play,
            active["id"],
            ended.isoformat(),
            duration,
            players,
            winner,
            note,
            lineup,
        )
        await self._apply_end()
        await self.coordinator.async_refresh_local()
        self.hass.bus.async_fire(
            f"{DOMAIN}_play_ended",
            {
                "bgg_id": active["bgg_id"],
                "name": active["game_name"],
                "duration_min": duration,
                "players": players,
                "winner": winner,
                "player_names": [p["name"] for p in lineup],
            },
        )
        result = {"game": active["game_name"], "duration_min": duration, "players": players}
        return {**result, **await self._maybe_upload(active["id"], upload)}

    async def _apply_end(self) -> None:
        scene = self._options.get(CONF_END_SCENE)
        if scene:
            await self._call(scene.split(".")[0], "turn_on", {"entity_id": scene})
        player = self._options.get(CONF_MEDIA_PLAYER)
        if player and self._started_playback:
            await self._call("media_player", "media_pause", {"entity_id": player})
        self._started_playback = False

    async def async_cancel_play(self) -> None:
        active = self.coordinator.data["active_play"]
        if not active:
            raise ServiceValidationError("Es läuft aktuell kein Spiel")
        await self.hass.async_add_executor_job(self.coordinator.db.cancel_play, active["id"])
        await self._apply_end()
        await self.coordinator.async_refresh_local()

    async def async_log_play(
        self,
        game: dict[str, Any],
        played_at: datetime | None,
        duration_min: int | None,
        players: int | None,
        winner: str | None,
        note: str | None,
        upload: bool | None = None,
        player_names: list[str] | None = None,
    ) -> dict[str, Any]:
        start = played_at or dt_util.utcnow()
        end = start + timedelta(minutes=duration_min or 0)
        lineup = self.resolve_players(player_names, winner)
        players = players or (len(lineup) + 1 if lineup else None)
        play_id = await self.hass.async_add_executor_job(
            self.coordinator.db.add_play,
            self._username,
            game["bgg_id"],
            game["name"],
            start.isoformat(),
            end.isoformat(),
            duration_min,
            players,
            winner,
            note,
            lineup,
        )
        await self.coordinator.async_refresh_local()
        result = {"game": game["name"], "played_at": start.isoformat()}
        return {**result, **await self._maybe_upload(play_id, upload)}

    # ---- Mitspieler -------------------------------------------------------
    def find_player(self, name: str) -> dict[str, Any] | None:
        """Bekannten Mitspieler per Name oder BGG-Benutzername finden."""
        needle = name.strip().lower()
        for p in self.coordinator.data.get("players", []):
            if p["name"].lower() == needle or (p["bgg_username"] or "").lower() == needle:
                return p
        return None

    def resolve_players(
        self, names: list[str] | None, winner: str | None
    ) -> list[dict[str, Any]]:
        """Namen -> [{name, bgg_username, win}]; unbekannte Namen bleiben reine Namen.

        Steht der Gewinner nicht in der Liste (und bist es nicht du), wird er ergänzt.
        """
        if not names:
            return []
        me = self._username.lower()
        win = (winner or "").strip().lower()
        lineup: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in [*names, *([winner] if winner else [])]:
            name = (raw or "").strip()
            if not name or name.lower() == me:
                continue
            known = self.find_player(name)
            entry = {
                "name": known["name"] if known else name,
                "bgg_username": known["bgg_username"] if known else None,
            }
            ident = (entry["bgg_username"] or entry["name"]).lower()
            if ident in seen:
                continue
            seen.add(ident)
            entry["win"] = win in (name.lower(), entry["name"].lower(),
                                   (entry["bgg_username"] or "").lower())
            lineup.append(entry)
        return lineup

    async def async_set_player(self, name: str, bgg_username: str | None) -> dict[str, Any]:
        """Mitspieler anlegen oder mit einem BGG-Account verknüpfen (leer = Verknüpfung lösen)."""
        name = name.strip()
        bgg_username = (bgg_username or "").strip() or None
        if not name:
            raise ServiceValidationError("Name angeben")
        if bgg_username:
            if bgg_username.lower() == self._username.lower():
                raise ServiceValidationError("Das ist dein eigener BGG-Account")
            try:
                exists = await self.coordinator.client.user_exists(bgg_username)
            except BggError as err:
                raise HomeAssistantError(f"BGG-Benutzer konnte nicht geprüft werden: {err}") from err
            if not exists:
                raise ServiceValidationError(f"BGG-Benutzer '{bgg_username}' gibt es nicht")
        await self.hass.async_add_executor_job(
            self.coordinator.db.set_player, self._username, name, bgg_username
        )
        await self.coordinator.async_refresh_local()
        return {"name": name, "bgg_username": bgg_username}

    async def async_remove_player(self, name: str) -> None:
        removed = await self.hass.async_add_executor_job(
            self.coordinator.db.remove_player, self._username, name.strip()
        )
        if not removed:
            raise ServiceValidationError(
                f"'{name}' ist kein selbst angelegter Mitspieler "
                "(Mitspieler aus BGG-Partien lassen sich nicht entfernen)"
            )
        await self.coordinator.async_refresh_local()

    # ---- Partien auf BGG loggen ------------------------------------------
    def _web(self) -> BggWebClient:
        password = self._options.get(CONF_PASSWORD)
        if not password:
            raise ServiceValidationError(
                "Zum Loggen auf BGG das BGG-Passwort in den Optionen der Integration eintragen"
            )
        if self._web_client is None:
            self._web_client = BggWebClient(
                async_create_clientsession(self.hass), self._username, password
            )
        return self._web_client

    async def async_upload_play(self, play_id: int) -> dict[str, Any]:
        """Lädt eine lokal gespeicherte Partie auf BGG hoch."""
        db = self.coordinator.db
        play = await self.hass.async_add_executor_job(db.get_play, self._username, play_id)
        if play is None:
            raise ServiceValidationError(f"Partie {play_id} nicht gefunden")
        if play["ended_at"] is None:
            raise ServiceValidationError("Die Partie läuft noch")
        if play["bgg_play_id"] is not None:
            raise ServiceValidationError("Die Partie ist bereits auf BGG geloggt")
        started = dt_util.parse_datetime(play["started_at"]) or dt_util.utcnow()
        web = self._web()
        try:
            bgg_play_id = await web.log_play(
                play["bgg_id"],
                dt_util.as_local(started).date().isoformat(),
                play["duration_min"],
                play["note"],
                self._bgg_players(play),
            )
        except BggAuthError as err:
            self._web_client = None
            raise HomeAssistantError(str(err)) from err
        except BggError as err:
            raise HomeAssistantError(f"Loggen auf BGG fehlgeschlagen: {err}") from err
        # 0 = hochgeladen, aber BGG hat keine ID genannt (verhindert Doppel-Upload)
        await self.hass.async_add_executor_job(db.set_bgg_play_id, play_id, bgg_play_id or 0)
        return {"play_id": play_id, "bgg_play_id": bgg_play_id}

    def _bgg_players(self, play: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Spielerliste für BGG: du selbst + Mitspieler (mit BGG-Account, falls bekannt)."""
        winner = (play["winner"] or "").strip()
        lineup = play["player_names"]
        if not lineup:  # nur Gewinner bekannt (wie bisher)
            return [{"name": winner, "win": 1}] if winner else None
        me = self._username
        result = [
            {"name": me, "username": me, "win": int(winner.lower() == me.lower())}
        ]
        for p in lineup:
            entry = {"name": p["name"], "win": int(bool(p.get("win")))}
            if p.get("bgg_username"):
                entry["username"] = p["bgg_username"]
            result.append(entry)
        return result

    async def _maybe_upload(self, play_id: int, upload: bool | None) -> dict[str, Any]:
        """Upload nach dem lokalen Speichern; Fehler verwerfen die Partie nicht."""
        if upload is None:
            upload = self._options.get(CONF_UPLOAD_PLAYS, DEFAULT_UPLOAD_PLAYS)
        if not upload:
            return {"play_id": play_id}
        try:
            return await self.async_upload_play(play_id)
        except HomeAssistantError as err:
            _LOGGER.warning("Partie %s nicht auf BGG geloggt: %s", play_id, err)
            persistent_notification.async_create(
                self.hass,
                f"Die Partie wurde lokal gespeichert, aber nicht auf BGG geloggt: {err}\n\n"
                f"Erneut versuchen mit `{DOMAIN}.upload_play` und `play_id: {play_id}`.",
                title="BGG: Partie nicht hochgeladen",
                notification_id=f"{DOMAIN}_upload_{play_id}",
            )
            return {"play_id": play_id, "bgg_error": str(err)}

    async def async_get_plays(self, bgg_id: int | None, limit: int) -> list[dict[str, Any]]:
        return await self.hass.async_add_executor_job(
            self.coordinator.db.get_plays, self._username, bgg_id, limit
        )

    # ---- Vorschläge -------------------------------------------------------
    def _reset_suggestion_state(self) -> None:
        self.suggestion = {"phase": "idle"}
        self._queue = []
        self._filters = {}

    def suggest(
        self, players: int | None, mode: str, game_type: str, max_playtime: int | None
    ) -> dict[str, Any]:
        matches = filter_games(self.coordinator.data["games"], players, mode, game_type, max_playtime)
        random.shuffle(matches)
        self._queue = [g["bgg_id"] for g in matches]
        self._filters = {
            "players": players,
            "filter_mode": mode,
            "filter_type": game_type,
            "match_count": len(matches),
        }
        self._advance()
        return self.suggestion

    def _advance(self) -> None:
        by_id = {g["bgg_id"]: g for g in self.coordinator.data["games"]}
        while self._queue:
            game = by_id.get(self._queue.pop(0))
            if game is None:
                continue
            self.suggestion = {
                "phase": "suggested",
                "bgg_id": game["bgg_id"],
                "name": game["name"],
                "year": game["year"],
                "location": game["location"],
                "summary": summary_line(game),
                "min_players": game["min_players"],
                "max_players": game["max_players"],
                "playing_time": game["playing_time"],
                "mode": game["mode"],
                "game_types": game["game_types"],
                "thumbnail": game["thumbnail"],
                "queue_remaining": len(self._queue),
                **self._filters,
            }
            self.coordinator.async_update_listeners()
            return
        self.suggestion = {"phase": "none", **self._filters}
        self.coordinator.async_update_listeners()

    def reject(self) -> None:
        if self.suggestion.get("phase") == "suggested":
            self._advance()

    def accept(self) -> None:
        if self.suggestion.get("phase") == "suggested":
            self.suggestion = {**self.suggestion, "phase": "accepted"}
            self.coordinator.async_update_listeners()

    def reset(self) -> None:
        self._reset_suggestion_state()
        self.coordinator.async_update_listeners()
