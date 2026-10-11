"""Koordinator: holt die Sammlung von BGG und speichert sie in SQLite."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import THING_BATCH_SIZE, BggAuthError, BggClient, BggError
from .const import (
    CONF_INTERVAL_HOURS,
    CONF_ONLY_OWNED,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_ONLY_OWNED,
    DOMAIN,
)
from .db import SEP, BggDatabase

_LOGGER = logging.getLogger(__name__)
UNKNOWN_LANGUAGE = "Unbekannt"
PAUSE_BETWEEN_REQUESTS = 2


class BggCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Synchronisiert die Sammlung."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: BggClient, db: BggDatabase
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{client.username}",
            config_entry=entry,
            update_interval=timedelta(
                hours=entry.options.get(CONF_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS)
            ),
        )
        self.client = client
        self.db = db

    async def _async_update_data(self) -> dict[str, Any]:
        only_owned = self.config_entry.options.get(CONF_ONLY_OWNED, DEFAULT_ONLY_OWNED)
        try:
            items = await self.client.fetch_collection(only_owned)

            # Versionen (und deren Sprachen) nur für unbekannte Versionen nachladen
            known = await self.hass.async_add_executor_job(self.db.known_version_ids)
            missing_games = sorted(
                {i.bgg_id for i in items if i.version_id and i.version_id not in known}
            )
            for start in range(0, len(missing_games), THING_BATCH_SIZE):
                chunk = missing_games[start : start + THING_BATCH_SIZE]
                try:
                    versions = await self.client.fetch_versions(chunk)
                except BggAuthError:
                    raise
                except BggError as err:
                    _LOGGER.warning("Versionen konnten nicht geladen werden: %s", err)
                    break
                await self.hass.async_add_executor_job(self.db.upsert_versions, versions)
                await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)

            cached = await self.hass.async_add_executor_job(self.db.get_version_languages)
            synced_at = dt_util.utcnow()
            rows = []
            for it in items:
                langs = it.languages or cached.get(it.version_id, [])
                rows.append(
                    {
                        "bgg_id": it.bgg_id,
                        "name": it.name,
                        "year": it.year,
                        "image": it.image,
                        "thumbnail": it.thumbnail,
                        "owned": int(it.owned),
                        "wishlist": int(it.wishlist),
                        "want_to_play": int(it.want_to_play),
                        "num_plays": it.num_plays,
                        "user_rating": it.user_rating,
                        "bgg_rating": it.bgg_rating,
                        "version_id": it.version_id,
                        "version_name": it.version_name,
                        "version_language": SEP.join(langs) or None,
                        "last_modified": it.last_modified,
                    }
                )
            username = self.client.username
            await self.hass.async_add_executor_job(
                self.db.replace_games, username, rows, synced_at.isoformat()
            )
            games = await self.hass.async_add_executor_job(self.db.get_games, username)
        except BggAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BggError as err:
            raise UpdateFailed(str(err)) from err

        languages: Counter[str] = Counter()
        for game in games:
            for lang in game["languages"] or [UNKNOWN_LANGUAGE]:
                languages[lang] += 1

        return {
            "count": len(games),
            "languages": dict(languages.most_common()),
            "games": games,
            "last_sync": synced_at,
        }
