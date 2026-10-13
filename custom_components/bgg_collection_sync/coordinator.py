"""Koordinator: holt Sammlung + Erweiterungen von BGG und speichert sie in SQLite."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EXPANSION, PLAYS_PAGE_SIZE, THING_BATCH_SIZE, BggAuthError, BggClient, BggError
from .const import (
    CONF_INCLUDE_EXPANSIONS,
    CONF_INTERVAL_HOURS,
    CONF_ONLY_OWNED,
    DEFAULT_INCLUDE_EXPANSIONS,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_ONLY_OWNED,
    DOMAIN,
)
from .db import SEP, BggDatabase
from .manager import BggManager

_LOGGER = logging.getLogger(__name__)
UNKNOWN_LANGUAGE = "Unbekannt"
PAUSE_BETWEEN_REQUESTS = 2
MAX_PLAY_PAGES = 50  # Sicherheitsgrenze: 5000 Partien pro Sync


class BggCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Synchronisiert Sammlung und Erweiterungen; hält den lokalen Zustand aktuell."""

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
        self.manager = BggManager(hass, self)
        self._full_play_sync = True  # beim ersten Sync nach dem Start alles abgleichen

    # ---- lokaler Zustand --------------------------------------------------
    def _load_local(self) -> tuple[Any, ...]:
        user = self.client.username
        plays = self.db.get_plays(user, limit=1)
        return (
            self.db.get_games(user),
            self.db.get_active_play(user),
            self.db.count_plays(user),
            plays[0] if plays else None,
            self.db.get_players(user),
        )

    def _summarize(self, local: tuple[Any, ...], last_sync: datetime) -> dict[str, Any]:
        everything, active, play_count, last_play, players = local
        # "Früher besessen" separat halten: zählt nicht zur Sammlung, kein Vorschlag
        prev_owned = [g for g in everything if g["prev_owned"] and not g["owned"]]
        current = [g for g in everything if not (g["prev_owned"] and not g["owned"])]
        games = [g for g in current if g["subtype"] != EXPANSION]
        expansions = [g for g in current if g["subtype"] == EXPANSION]

        if active:
            game = next((g for g in everything if g["bgg_id"] == active["bgg_id"]), None)
            active["location"] = game["location"] if game else ""

        languages: Counter[str] = Counter()
        for game in games:
            for lang in game["languages"] or [UNKNOWN_LANGUAGE]:
                languages[lang] += 1

        return {
            "count": len(games),
            "expansion_count": len(expansions),
            "languages": dict(languages.most_common()),
            "games": games,
            "expansions": expansions,
            "prev_owned": prev_owned,
            "active_play": active,
            "play_count": play_count,
            "last_play": last_play,
            "players": players,
            "last_sync": last_sync,
        }

    async def async_refresh_local(self) -> None:
        """Liest DB neu ein (nach Metadaten-/Partie-Änderungen), ohne BGG abzufragen."""
        local = await self.hass.async_add_executor_job(self._load_local)
        self.async_set_updated_data(self._summarize(local, self.data["last_sync"]))

    # ---- BGG-Partien (Mitspieler) -----------------------------------------
    async def _sync_bgg_plays(self, full: bool) -> None:
        """Lädt geloggte Partien von BGG (neueste zuerst).

        Inkrementell: stoppt bei der ersten Seite, die nur bekannte Partien enthält.
        """
        run = self.hass.async_add_executor_job
        user = self.client.username
        known = await run(self.db.known_bgg_play_ids, user)
        for page in range(1, MAX_PLAY_PAGES + 1):
            if page > 1:
                await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)
            total, plays = await self.client.fetch_plays(page)
            if not plays:
                break
            await run(self.db.save_bgg_plays, user, plays)
            if not full and all(p.play_id in known for p in plays):
                break
            if page * PLAYS_PAGE_SIZE >= total:
                break

    async def async_sync_players(self, full: bool = True) -> None:
        """Partien/Mitspieler sofort von BGG abgleichen (Service sync_players)."""
        try:
            await self._sync_bgg_plays(full)
        except BggAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        await self.async_refresh_local()

    # ---- BGG-Sync ---------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        opts = self.config_entry.options
        only_owned = opts.get(CONF_ONLY_OWNED, DEFAULT_ONLY_OWNED)
        include_exp = opts.get(CONF_INCLUDE_EXPANSIONS, DEFAULT_INCLUDE_EXPANSIONS)
        try:
            items = await self.client.fetch_collection(only_owned)
            if include_exp:
                await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)
                seen = {i.bgg_id for i in items}
                for exp in await self.client.fetch_collection(only_owned, expansions=True):
                    if exp.bgg_id not in seen:
                        exp.subtype = EXPANSION
                        items.append(exp)

            # Versionen/Sprachen, Spielinfos und Verknüpfungen nur bei Bedarf nachladen
            run = self.hass.async_add_executor_job
            known = await run(self.db.known_version_ids)
            checked = await run(self.db.checked_expansion_ids)
            known_info = await run(self.db.known_info_ids)
            expansion_ids = {i.bgg_id for i in items if i.subtype == EXPANSION}
            need = sorted(
                {
                    i.bgg_id
                    for i in items
                    if (i.version_id and i.version_id not in known)
                    or i.bgg_id not in known_info
                    or (i.bgg_id in expansion_ids and i.bgg_id not in checked)
                }
            )
            for start in range(0, len(need), THING_BATCH_SIZE):
                chunk = need[start : start + THING_BATCH_SIZE]
                try:
                    things = await self.client.fetch_things(chunk)
                except BggAuthError:
                    raise
                except BggError as err:
                    _LOGGER.warning("Details konnten nicht geladen werden: %s", err)
                    break
                await run(self.db.upsert_versions, things.versions)
                await run(self.db.upsert_info, things.info)
                await run(
                    self.db.save_expansion_links,
                    [i for i in chunk if i in expansion_ids],
                    things.base_links,
                )
                await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)

            cached = await run(self.db.get_version_languages)
            synced_at = dt_util.utcnow()
            rows = []
            for it in items:
                langs = it.languages or cached.get(it.version_id, [])
                rows.append(
                    {
                        "bgg_id": it.bgg_id,
                        "subtype": it.subtype,
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
                        "collid": it.collid,
                        "prev_owned": int(it.prev_owned),
                        "for_trade": int(it.for_trade),
                        "status": it.status,
                    }
                )
            await run(self.db.replace_games, self.client.username, rows, synced_at.isoformat())

            # Mitspieler aus BGG-Partien; Fehler hier blockieren den Sammlungs-Sync nicht
            await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)
            try:
                await self._sync_bgg_plays(self._full_play_sync)
                self._full_play_sync = False
            except BggAuthError:
                raise
            except BggError as err:
                _LOGGER.warning("Partien/Mitspieler konnten nicht geladen werden: %s", err)
            local = await run(self._load_local)
        except BggAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BggError as err:
            raise UpdateFailed(str(err)) from err

        return self._summarize(local, synced_at)
