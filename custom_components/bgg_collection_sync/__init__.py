"""BGG Collection Sync: BoardGameGeek-Sammlung, Standorte, Partien und Spielvorschläge."""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import BggClient, BggError
from .const import (
    CONF_TOKEN,
    CONF_USERNAME,
    DB_FILENAME,
    DOMAIN,
    SERVICE_GET_GAMES,
    SERVICE_SYNC_NOW,
)
from .coordinator import BggCoordinator
from .db import BggDatabase
from .logic import GAME_TYPES, MODES

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

CARD_URL = "/bgg_collection_sync/bgg-shelf-card.js"
CARD_VERSION = "0.8.0"

type BggConfigEntry = ConfigEntry[BggCoordinator]

_ENTRY = {vol.Optional("entry_id"): cv.string}
_GAME = {**_ENTRY, vol.Optional("bgg_id"): vol.Coerce(int), vol.Optional("name"): cv.string}
_PLAYERS = vol.All(vol.Coerce(int), vol.Range(min=1, max=99))
_PLAYER_NAMES = vol.All(cv.ensure_list, [cv.string])
_SCORES = vol.Schema({cv.string: vol.Coerce(float)})

GET_GAMES_SCHEMA = vol.Schema(
    {
        vol.Optional("language"): cv.string,
        vol.Optional("type", default="all"): vol.In(["all", "games", "expansions"]),
        vol.Optional("include_prev_owned", default=False): cv.boolean,
        **_ENTRY,
    }
)
SET_META_SCHEMA = vol.Schema(
    {
        **_GAME,
        vol.Optional("location"): cv.string,
        vol.Optional("nfc_tag"): cv.string,
        vol.Optional("spotify_uri"): cv.string,
        vol.Optional("light_scene"): cv.string,
    }
)
START_PLAY_SCHEMA = vol.Schema(
    {**_GAME, vol.Optional("players"): _PLAYERS, vol.Optional("player_names"): _PLAYER_NAMES}
)
END_PLAY_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Optional("winner"): cv.string,
        vol.Optional("note"): cv.string,
        vol.Optional("players"): _PLAYERS,
        vol.Optional("upload_to_bgg"): cv.boolean,
        vol.Optional("player_names"): _PLAYER_NAMES,
        vol.Optional("scores"): _SCORES,
    }
)
LOG_PLAY_SCHEMA = vol.Schema(
    {
        **_GAME,
        vol.Optional("played_at"): cv.datetime,
        vol.Optional("duration_min"): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
        vol.Optional("players"): _PLAYERS,
        vol.Optional("winner"): cv.string,
        vol.Optional("note"): cv.string,
        vol.Optional("upload_to_bgg"): cv.boolean,
        vol.Optional("player_names"): _PLAYER_NAMES,
        vol.Optional("scores"): _SCORES,
    }
)
UPLOAD_PLAY_SCHEMA = vol.Schema({**_ENTRY, vol.Required("play_id"): vol.Coerce(int)})
BGSTATS_LINK_SCHEMA = vol.Schema({**_ENTRY, vol.Optional("play_id"): vol.Coerce(int)})
SET_PLAYER_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Required("name"): cv.string, vol.Optional("bgg_username"): cv.string}
)
REMOVE_PLAYER_SCHEMA = vol.Schema({**_ENTRY, vol.Required("name"): cv.string})
GET_PLAYS_SCHEMA = vol.Schema(
    {
        **_GAME,
        vol.Optional("limit", default=20): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
    }
)
SUGGEST_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Optional("players"): _PLAYERS,
        vol.Optional("mode", default="any"): vol.In(MODES),
        vol.Optional("game_type", default="any"): vol.In(GAME_TYPES),
        vol.Optional("max_playtime"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
    }
)
ENTRY_ONLY_SCHEMA = vol.Schema(_ENTRY)
ASSIGN_TAG_SCHEMA = vol.Schema(_GAME)


def _loaded_entries(hass: HomeAssistant, entry_id: str | None = None) -> list[BggConfigEntry]:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and (entry_id is None or e.entry_id == entry_id)
    ]
    if not entries:
        raise ServiceValidationError("Keine geladene BGG-Integration gefunden")
    return entries


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> BggCoordinator:
    return _loaded_entries(hass, call.data.get("entry_id"))[0].runtime_data


def _game(coordinator: BggCoordinator, call: ServiceCall) -> dict:
    return coordinator.manager.find_game(call.data.get("bgg_id"), call.data.get("name"))


async def _async_register_card(hass: HomeAssistant) -> None:
    """Lovelace-Karte (Billy-Regal) bereitstellen und automatisch laden.

    Fehler hier dürfen die Integration nicht blockieren – dann fehlt nur die Karte.
    """
    try:
        from homeassistant.components.frontend import add_extra_js_url

        path = str(Path(__file__).parent / "frontend" / "bgg-shelf-card.js")
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, path, False)])
        except ImportError:  # HA < 2024.7
            hass.http.register_static_path(CARD_URL, path, False)
        add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("BGG-Regalkarte konnte nicht registriert werden")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await _async_register_card(hass)

    async def handle_sync_now(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            await entry.runtime_data.async_refresh()

    async def handle_get_games(call: ServiceCall) -> dict:
        wanted = (call.data.get("language") or "").strip().lower()
        kind = call.data["type"]
        games = []
        for entry in _loaded_entries(hass, call.data.get("entry_id")):
            data = entry.runtime_data.data
            pool = []
            if kind in ("all", "games"):
                pool += [("game", g) for g in data["games"]]
            if kind in ("all", "expansions"):
                pool += [("expansion", g) for g in data["expansions"]]
            if call.data["include_prev_owned"]:
                pool += [
                    ("expansion" if g["subtype"] == "boardgameexpansion" else "game", g)
                    for g in data.get("prev_owned", [])
                    if kind == "all"
                    or (kind == "expansions") == (g["subtype"] == "boardgameexpansion")
                ]
            for item_type, game in pool:
                if wanted and wanted not in (lang.lower() for lang in game["languages"]):
                    continue
                games.append(
                    {
                        "username": entry.data[CONF_USERNAME],
                        "type": item_type,
                        "bgg_id": game["bgg_id"],
                        "name": game["name"],
                        "year": game["year"],
                        "version": game["version_name"],
                        "language": game["version_language"],
                        "languages": game["languages"],
                        "num_plays": game["num_plays"],
                        "user_rating": game["user_rating"],
                        "thumbnail": game["thumbnail"],
                        "location": game["location"],
                        "nfc_tag": game["nfc_tag"],
                        "spotify_uri": game["spotify_uri"],
                        "light_scene": game["light_scene"],
                        "owned": game["owned"],
                        "for_trade": game["for_trade"],
                        "prev_owned": game["prev_owned"],
                        "min_players": game["min_players"],
                        "max_players": game["max_players"],
                        "playing_time": game["playing_time"],
                        "mode": game["mode"],
                        "game_types": game["game_types"],
                        "expansions": game["expansions"],
                        "base_games": game["base_games"],
                    }
                )
        return {"count": len(games), "games": games}

    async def handle_set_meta(call: ServiceCall) -> None:
        c = _coordinator(hass, call)
        fields = {
            k: call.data[k]
            for k in ("location", "nfc_tag", "spotify_uri", "light_scene")
            if k in call.data
        }
        if not fields:
            raise ServiceValidationError(
                "Mindestens location, nfc_tag, spotify_uri oder light_scene angeben"
            )
        await c.manager.async_set_meta(_game(c, call), fields)

    async def handle_assign_tag(call: ServiceCall) -> dict:
        c = _coordinator(hass, call)
        return {"nfc_tag": await c.manager.async_assign_scanned_tag(_game(c, call))}

    async def handle_start_play(call: ServiceCall) -> dict:
        c = _coordinator(hass, call)
        game = _game(c, call) if ("bgg_id" in call.data or "name" in call.data) else None
        return await c.manager.async_start_play(
            game, call.data.get("players"), call.data.get("player_names")
        )

    async def handle_end_play(call: ServiceCall) -> dict:
        c = _coordinator(hass, call)
        return await c.manager.async_end_play(
            call.data.get("winner"),
            call.data.get("note"),
            call.data.get("players"),
            call.data.get("upload_to_bgg"),
            call.data.get("player_names"),
            call.data.get("scores"),
        )

    async def handle_cancel_play(call: ServiceCall) -> None:
        await _coordinator(hass, call).manager.async_cancel_play()

    async def handle_log_play(call: ServiceCall) -> dict:
        c = _coordinator(hass, call)
        played_at = call.data.get("played_at")
        return await c.manager.async_log_play(
            _game(c, call),
            dt_util.as_utc(played_at) if played_at else None,
            call.data.get("duration_min"),
            call.data.get("players"),
            call.data.get("winner"),
            call.data.get("note"),
            call.data.get("upload_to_bgg"),
            call.data.get("player_names"),
            call.data.get("scores"),
        )

    async def handle_sync_players(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call.data.get("entry_id")):
            try:
                await entry.runtime_data.async_sync_players()
            except BggError as err:
                raise HomeAssistantError(f"Partien von BGG nicht geladen: {err}") from err

    async def handle_set_player(call: ServiceCall) -> dict:
        return await _coordinator(hass, call).manager.async_set_player(
            call.data["name"], call.data.get("bgg_username")
        )

    async def handle_remove_player(call: ServiceCall) -> None:
        await _coordinator(hass, call).manager.async_remove_player(call.data["name"])

    async def handle_get_players(call: ServiceCall) -> dict:
        players = _coordinator(hass, call).data.get("players", [])
        return {"count": len(players), "players": players}

    async def handle_bgstats_link(call: ServiceCall) -> dict:
        return await _coordinator(hass, call).manager.async_bgstats_link(call.data.get("play_id"))

    async def handle_upload_play(call: ServiceCall) -> dict:
        return await _coordinator(hass, call).manager.async_upload_play(call.data["play_id"])

    async def handle_get_plays(call: ServiceCall) -> dict:
        c = _coordinator(hass, call)
        bgg_id = None
        if "bgg_id" in call.data or "name" in call.data:
            bgg_id = _game(c, call)["bgg_id"]
        plays = await c.manager.async_get_plays(bgg_id, call.data["limit"])
        return {"count": len(plays), "plays": plays}

    async def handle_suggest(call: ServiceCall) -> dict:
        c = _coordinator(hass, call)
        return dict(
            c.manager.suggest(
                call.data.get("players"),
                call.data["mode"],
                call.data["game_type"],
                call.data.get("max_playtime"),
            )
        )

    async def handle_reject(call: ServiceCall) -> None:
        _coordinator(hass, call).manager.reject()

    async def handle_accept(call: ServiceCall) -> None:
        _coordinator(hass, call).manager.accept()

    async def handle_reset(call: ServiceCall) -> None:
        _coordinator(hass, call).manager.reset()

    reg = hass.services.async_register
    reg(DOMAIN, SERVICE_SYNC_NOW, handle_sync_now)
    reg(DOMAIN, SERVICE_GET_GAMES, handle_get_games, schema=GET_GAMES_SCHEMA,
        supports_response=SupportsResponse.ONLY)
    reg(DOMAIN, "set_game_meta", handle_set_meta, schema=SET_META_SCHEMA)
    reg(DOMAIN, "assign_scanned_tag", handle_assign_tag, schema=ASSIGN_TAG_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "start_play", handle_start_play, schema=START_PLAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "end_play", handle_end_play, schema=END_PLAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "cancel_play", handle_cancel_play, schema=ENTRY_ONLY_SCHEMA)
    reg(DOMAIN, "log_play", handle_log_play, schema=LOG_PLAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "sync_players", handle_sync_players, schema=ENTRY_ONLY_SCHEMA)
    reg(DOMAIN, "set_player", handle_set_player, schema=SET_PLAYER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "remove_player", handle_remove_player, schema=REMOVE_PLAYER_SCHEMA)
    reg(DOMAIN, "get_players", handle_get_players, schema=ENTRY_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY)
    reg(DOMAIN, "bgstats_link", handle_bgstats_link, schema=BGSTATS_LINK_SCHEMA,
        supports_response=SupportsResponse.ONLY)
    reg(DOMAIN, "upload_play", handle_upload_play, schema=UPLOAD_PLAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "get_plays", handle_get_plays, schema=GET_PLAYS_SCHEMA,
        supports_response=SupportsResponse.ONLY)
    reg(DOMAIN, "suggest_game", handle_suggest, schema=SUGGEST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    reg(DOMAIN, "reject_suggestion", handle_reject, schema=ENTRY_ONLY_SCHEMA)
    reg(DOMAIN, "accept_suggestion", handle_accept, schema=ENTRY_ONLY_SCHEMA)
    reg(DOMAIN, "reset_suggestion", handle_reset, schema=ENTRY_ONLY_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BggConfigEntry) -> bool:
    client = BggClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data.get(CONF_TOKEN),
    )
    db = BggDatabase(hass.config.path(DB_FILENAME))
    await hass.async_add_executor_job(db.setup)

    coordinator = BggCoordinator(hass, entry, client, db)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    coordinator.manager.async_start()
    entry.async_on_unload(coordinator.manager.async_stop)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, coordinator.async_background_play_sync(), f"{DOMAIN}_play_sync"
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: BggConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BggConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
