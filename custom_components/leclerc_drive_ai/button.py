"""Boutons d'action pour Home Assistant (Remplir le panier / Synchroniser l'historique)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LeclercDriveCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LeclercDriveCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LeclercFillCartButton(coordinator, entry),
            LeclercSyncHistoryButton(coordinator, entry),
        ]
    )


class LeclercFillCartButton(ButtonEntity):
    """Bouton 1-clic dans Home Assistant pour lancer le remplissage du panier E.Leclerc Drive."""

    _attr_has_entity_name = True
    _attr_name = "Remplir le panier E.Leclerc"
    _attr_icon = "mdi:cart-arrow-down"

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_fill_cart"

    async def async_press(self) -> None:
        await self._coordinator.async_fill_cart()


class LeclercSyncHistoryButton(ButtonEntity):
    """Bouton 1-clic dans Home Assistant pour aspirer automatiquement l'historique des commandes."""

    _attr_has_entity_name = True
    _attr_name = "Synchroniser l'historique des commandes"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: LeclercDriveCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_sync_history"

    async def async_press(self) -> None:
        await self._coordinator.async_sync_history()
