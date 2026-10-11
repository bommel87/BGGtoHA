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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BggConfigEntry
from .const import DOMAIN
from .coordinator import BggCoordinator


@dataclass(frozen=True, kw_only=True)
class BggSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


SENSORS = (
    BggSensorDescription(
        key="collection",
        translation_key="collection",
        icon="mdi:dice-multiple",
        value_fn=lambda d: d["count"],
    ),
    BggSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d["last_sync"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BggConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(BggSensor(entry.runtime_data, desc) for desc in SENSORS)


class BggSensor(CoordinatorEntity[BggCoordinator], SensorEntity):
    """Sensor mit Spieleanzahl bzw. letztem Sync."""

    _attr_has_entity_name = True
    entity_description: BggSensorDescription
    # Große Attribute nicht in die Recorder-Datenbank schreiben
    _unrecorded_attributes = frozenset({"games", "languages"})

    def __init__(self, coordinator: BggCoordinator, description: BggSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        username = coordinator.client.username
        self._attr_unique_id = f"{username.lower()}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, username.lower())},
            name=f"BoardGameGeek {username}",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://boardgamegeek.com/collection/user/{username}",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "collection":
            return None
        data = self.coordinator.data
        return {
            "languages": data["languages"],
            "games": [
                {
                    "name": g["name"],
                    "year": g["year"],
                    "language": g["version_language"] or "",
                    "version": g["version_name"] or "",
                }
                for g in data["games"]
            ],
        }
