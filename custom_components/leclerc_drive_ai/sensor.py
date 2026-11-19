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
            LeclercRemindersSensor(coordinator, entry),
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
        rep = self._coordinator.last_report or {}
        return {
            "orders_in_history": self._coordinator.orders_count,
            "cart_items_count": self._coordinator.cart_count,
            "saison_france": rep.get("saison_france"),
            "derniere_commande": rep.get("derniere_commande"),
            "mode_courses": rep.get("mode_courses"),
            "articles_ajoutes": rep.get("added", []),
            "articles_manquants": rep.get("missing", []),
            "doutes_et_arbitrages": rep.get("doutes_et_arbitrages", []),
            "rappels_oublis_habituels": rep.get("rappels_oublis_habituels", []),
            "last_report": rep,
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


class LeclercRemindersSensor(_BaseLeclercSensor):
    _attr_name = "Rappels & Doutes Courses E.Leclerc"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reminders_and_doubts"

    @property
    def native_value(self) -> str:
        rep = self._coordinator.last_report or {}
        missing_n = len(rep.get("missing", []))
        doubts_n = len(rep.get("doutes_et_arbitrages", []))
        rem_n = len(rep.get("rappels_oublis_habituels", []))
        return f"{missing_n} manquant(s) | {doubts_n} doute(s) | {rem_n} rappel(s)"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rep = self._coordinator.last_report or {}
        return {
            "saison_france": rep.get("saison_france"),
            "mode_courses": rep.get("mode_courses"),
            "articles_manquants_ou_introuvables": rep.get("missing", []),
            "doutes_et_arbitrages_ia": rep.get("doutes_et_arbitrages", []),
            "rappels_oublis_habituels": [
                f"{r.get('name')} ({r.get('reason')})"
                for r in rep.get("rappels_oublis_habituels", [])
                if isinstance(r, dict)
            ],
        }
