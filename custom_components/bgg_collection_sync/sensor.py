"""Sensoren für BGG Collection Sync."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BggConfigEntry
from .const import DOMAIN
from .coordinator import BggCoordinator


@dataclass(frozen=True, kw_only=True)
class BggSensorDescription(SensorEntityDescription):
    value_fn: Callable[[BggCoordinator], Any]


SENSORS = (
    BggSensorDescription(
        key="collection",
        translation_key="collection",
        icon="mdi:dice-multiple",
        value_fn=lambda c: c.data["count"],
    ),
    BggSensorDescription(
        key="expansions",
        translation_key="expansions",
        icon="mdi:puzzle",
        value_fn=lambda c: c.data["expansion_count"],
    ),
    BggSensorDescription(
        key="plays",
        translation_key="plays",
        icon="mdi:counter",
        value_fn=lambda c: c.data["play_count"],
    ),
    BggSensorDescription(
        key="session",
        translation_key="session",
        icon="mdi:play-circle",
        value_fn=lambda c: "running" if c.data["active_play"] else "idle",
    ),
    BggSensorDescription(
        key="suggestion",
        translation_key="suggestion",
        icon="mdi:dice-multiple-outline",
        value_fn=lambda c: c.manager.suggestion["phase"],
    ),
    BggSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data["last_sync"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BggConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(BggSensor(coordinator, desc) for desc in SENSORS)

    # Je Mitspieler ein Sensor; neue Mitspieler kommen nach Sync/Anlegen dazu
    known: set[str] = set()

    @callback
    def _add_players() -> None:
        new = [p for p in coordinator.data.get("players", []) if p["key"] not in known]
        if new:
            known.update(p["key"] for p in new)
            async_add_entities(BggPlayerSensor(coordinator, p) for p in new)

    _add_players()
    entry.async_on_unload(coordinator.async_add_listener(_add_players))


def _device_info(username: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, username.lower())},
        name=f"BoardGameGeek {username}",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=f"https://boardgamegeek.com/collection/user/{username}",
    )


class BggSensor(CoordinatorEntity[BggCoordinator], SensorEntity):
    """Sensor für Sammlung, Partien, Sitzung und Vorschlag."""

    _attr_has_entity_name = True
    entity_description: BggSensorDescription
    # Große Attribute nicht in die Recorder-Datenbank schreiben
    _unrecorded_attributes = frozenset({"games", "expansions", "languages"})

    def __init__(self, coordinator: BggCoordinator, description: BggSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        username = coordinator.client.username
        self._attr_unique_id = f"{username.lower()}_{description.key}"
        self._attr_device_info = _device_info(username)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        key = self.entity_description.key
        if key == "collection":
            return {
                "languages": data["languages"],
                "games": [
                    {
                        "name": g["name"],
                        "year": g["year"],
                        "language": g["version_language"] or "",
                        "version": g["version_name"] or "",
                        "location": g["location"],
                        "expansions": [e["name"] for e in g["expansions"]],
                    }
                    for g in data["games"]
                ],
            }
        if key == "expansions":
            return {
                "expansions": [
                    {
                        "name": e["name"],
                        "year": e["year"],
                        "language": e["version_language"] or "",
                        "base_games": [b["name"] for b in e["base_games"]],
                    }
                    for e in data["expansions"]
                ]
            }
        if key == "plays":
            return {"last_play": data["last_play"]} if data["last_play"] else None
        if key == "session":
            active = data["active_play"]
            me = {"bgg_user": self.coordinator.client.username}
            if not active:
                return me
            return {
                **me,
                "name": active["game_name"],
                "bgg_id": active["bgg_id"],
                "started_at": active["started_at"],
                "players": active["players"],
                "location": active["location"],
                "player_names": [p["name"] for p in active["player_names"]],
            }
        if key == "suggestion":
            return {k: v for k, v in self.coordinator.manager.suggestion.items() if k != "phase"}
        return None


class BggPlayerSensor(CoordinatorEntity[BggCoordinator], SensorEntity):
    """Ein Mitspieler; Zustand = gemeinsame Partien."""

    _attr_has_entity_name = True
    _attr_translation_key = "player"
    _attr_icon = "mdi:account"

    def __init__(self, coordinator: BggCoordinator, player: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._key = player["key"]
        username = coordinator.client.username
        self._attr_unique_id = f"{username.lower()}_player_{self._key}"
        self._attr_device_info = _device_info(username)
        self._attr_translation_placeholders = {"name": player["name"]}

    @property
    def _player(self) -> dict[str, Any] | None:
        return next(
            (p for p in self.coordinator.data.get("players", []) if p["key"] == self._key), None
        )

    @property
    def available(self) -> bool:
        return super().available and self._player is not None

    @property
    def native_value(self) -> int | None:
        player = self._player
        return player["plays"] if player else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        p = self._player
        if p is None:
            return None
        return {
            "name": p["name"],
            "bgg_username": p["bgg_username"],
            "bgg_profile": (
                f"https://boardgamegeek.com/user/{p['bgg_username']}" if p["bgg_username"] else None
            ),
            "wins": p["wins"],
            "win_rate": p["win_rate"],
            "last_played": p["last_played"],
            "top_games": p["top_games"],
            "manual": p["manual"],
        }
