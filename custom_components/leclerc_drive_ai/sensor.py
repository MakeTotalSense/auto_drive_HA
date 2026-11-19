"""Capteurs Home Assistant pour suivre le panier E.Leclerc Drive et le rapport d'exécution."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SIGNAL_STATE_UPDATED, LeclercDriveCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LeclercDriveCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LeclercStatusSensor(coordinator, entry),
            LeclercCartTotalSensor(coordinator, entry),
            LeclercAverageBasketSensor(coordinator, entry),
        ]
    )


class _BaseLeclercSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class LeclercStatusSensor(_BaseLeclercSensor):
    _attr_name = "Statut Courses E.Leclerc"
    _attr_icon = "mdi:shopping-search"

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        return self._coordinator.last_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "orders_in_history": self._coordinator.orders_count,
            "cart_items_count": self._coordinator.cart_count,
            "last_report": self._coordinator.last_report,
        }


class LeclercCartTotalSensor(_BaseLeclercSensor):
    _attr_name = "Panier E.Leclerc Actuel"
    _attr_icon = "mdi:cart"

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_cart_total"

    @property
    def native_value(self) -> str:
        return self._coordinator.cart_total_str

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"articles_dans_le_panier": self._coordinator.cart_count}


class LeclercAverageBasketSensor(_BaseLeclercSensor):
    _attr_name = "Panier Moyen Historique"
    _attr_icon = "mdi:cash-multiple"
    _attr_native_unit_of_measurement = "€"

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_avg_basket"

    @property
    def native_value(self) -> float:
        return self._coordinator.avg_basket_eur
