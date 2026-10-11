"""BGG Collection Sync: BoardGameGeek-Sammlung in Home Assistant + SQLite."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import BggClient
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

PLATFORMS = ["sensor"]

type BggConfigEntry = ConfigEntry[BggCoordinator]

GET_GAMES_SCHEMA = vol.Schema(
    {vol.Optional("language"): cv.string, vol.Optional("entry_id"): cv.string}
)


def _loaded_entries(hass: HomeAssistant, entry_id: str | None = None) -> list[BggConfigEntry]:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and (entry_id is None or e.entry_id == entry_id)
    ]
    if not entries:
        raise ServiceValidationError("Keine geladene BGG-Integration gefunden")
    return entries


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_sync_now(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            await entry.runtime_data.async_refresh()

    async def handle_get_games(call: ServiceCall) -> dict:
        wanted = (call.data.get("language") or "").strip().lower()
        games = []
        for entry in _loaded_entries(hass, call.data.get("entry_id")):
            for game in entry.runtime_data.data["games"]:
                if wanted and wanted not in (lang.lower() for lang in game["languages"]):
                    continue
                games.append(
                    {
                        "username": entry.data[CONF_USERNAME],
                        "bgg_id": game["bgg_id"],
                        "name": game["name"],
                        "year": game["year"],
                        "version": game["version_name"],
                        "language": game["version_language"],
                        "languages": game["languages"],
                        "num_plays": game["num_plays"],
                        "user_rating": game["user_rating"],
                        "thumbnail": game["thumbnail"],
                    }
                )
        return {"count": len(games), "games": games}

    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, handle_sync_now)
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_GAMES,
        handle_get_games,
        schema=GET_GAMES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: BggConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BggConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
