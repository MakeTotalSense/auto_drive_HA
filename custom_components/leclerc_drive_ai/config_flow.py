"""Config Flow pour configurer E.Leclerc Drive AI directement depuis l'interface Home Assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    AI_DEFAULT_OLLAMA,
    CONF_AI_PROVIDER,
    CONF_CONVERSATION_ENTITY,
    CONF_GOOGLE_APP_PASSWORD,
    CONF_GOOGLE_EMAIL,
    CONF_GOOGLE_MASTER_TOKEN,
    CONF_KEEP_NOTE_TITLE,
    CONF_LECLERC_EMAIL,
    CONF_LECLERC_PASSWORD,
    CONF_LIST_SOURCE,
    CONF_MAGASIN_SLUG,
    CONF_OLLAMA_MODEL,
    CONF_OLLAMA_URL,
    CONF_OTP_CODE,
    CONF_PLURAL_ITEMS_COUNT,
    CONF_TODO_ENTITY_ID,
    DEFAULT_KEEP_NOTE_TITLE,
    DEFAULT_MAGASIN_SLUG,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_PLURAL_COUNT,
    DOMAIN,
    SOURCE_GOOGLE_KEEP,
    SOURCE_HA_TODO,
)
from .leclerc_worker import LeclercDriveWorker, resolve_google_keep_master_token


def _get_conversation_choices(hass: HomeAssistant) -> dict[str, str]:
    """Liste l'option qwen2.5:1.5b par défaut + toutes les entités conversation.* présentes dans Home Assistant."""
    choices: dict[str, str] = {
        AI_DEFAULT_OLLAMA: f"Ollama local : {DEFAULT_OLLAMA_MODEL} (Par défaut - Léger Raspberry Pi)"
    }
    for state in hass.states.async_all("conversation"):
        friendly = state.attributes.get("friendly_name") or state.entity_id
        choices[state.entity_id] = f"Entité Home Assistant : {friendly} ({state.entity_id})"
    return choices


class LeclercDriveAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Flux de configuration pas-à-pas dans l'interface Home Assistant."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._worker: LeclercDriveWorker | None = None

    def _get_worker(self) -> LeclercDriveWorker:
        if self._worker is None:
            storage_dir = Path(self.hass.config.path(".storage", DOMAIN))
            self._worker = LeclercDriveWorker(
                storage_dir=storage_dir,
                magasin_slug=self._data.get(CONF_MAGASIN_SLUG, DEFAULT_MAGASIN_SLUG),
            )
        return self._worker

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Étape 1 : Identifiants E.Leclerc Drive et magasin."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            worker = self._get_worker()

            login_res = await self.hass.async_add_executor_job(
                worker.login_leclerc,
                user_input[CONF_LECLERC_EMAIL],
                user_input[CONF_LECLERC_PASSWORD],
            )

            status = login_res.get("status")
            if status == "otp_required":
                return await self.async_step_otp()
            if status == "connected":
                # Lance l'aspiration automatique de l'historique des commandes en arrière-plan
                await self.hass.async_add_executor_job(worker.auto_scrape_order_history, 15)
                return await self.async_step_shopping_list()

            errors["base"] = "leclerc_auth_failed"

        schema = vol.Schema(
            {
                vol.Required(CONF_LECLERC_EMAIL): str,
                vol.Required(CONF_LECLERC_PASSWORD): str,
                vol.Required(CONF_MAGASIN_SLUG, default=DEFAULT_MAGASIN_SLUG): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_otp(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Étape 1-bis (si déclenchée par E.Leclerc) : Code à 6 chiffres reçu par e-mail."""
        errors: dict[str, str] = {}

        if user_input is not None:
            worker = self._get_worker()
            otp_res = await self.hass.async_add_executor_job(
                worker.submit_leclerc_otp,
                user_input[CONF_OTP_CODE],
            )
            if otp_res.get("status") == "connected":
                await self.hass.async_add_executor_job(worker.auto_scrape_order_history, 15)
                return await self.async_step_shopping_list()
            errors["base"] = "invalid_otp"

        schema = vol.Schema({vol.Required(CONF_OTP_CODE): str})
        return self.async_show_form(step_id="otp", data_schema=schema, errors=errors)

    async def async_step_shopping_list(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Étape 2 : Configuration de Google Keep (Mot de passe d'application 16 caractères ou Token) ou Todo HA."""
        errors: dict[str, str] = {}

        if user_input is not None:
            source = user_input.get(CONF_LIST_SOURCE, SOURCE_GOOGLE_KEEP)
            self._data.update(user_input)

            if source == SOURCE_GOOGLE_KEEP:
                g_email = user_input.get(CONF_GOOGLE_EMAIL, "").strip()
                g_secret = user_input.get(CONF_GOOGLE_APP_PASSWORD, "").strip()
                if not g_email or not g_secret:
                    errors["base"] = "missing_google_credentials"
                else:
                    try:
                        master_token = await self.hass.async_add_executor_job(
                            resolve_google_keep_master_token,
                            g_email,
                            g_secret,
                        )
                        self._data[CONF_GOOGLE_MASTER_TOKEN] = master_token
                        return await self.async_step_ai_model()
                    except Exception:
                        errors["base"] = "google_keep_auth_failed"
            else:
                return await self.async_step_ai_model()

        todo_entities = [s.entity_id for s in self.hass.states.async_all("todo")]
        default_todo = todo_entities[0] if todo_entities else "todo.shopping_list"

        schema = vol.Schema(
            {
                vol.Required(CONF_LIST_SOURCE, default=SOURCE_GOOGLE_KEEP): vol.In(
                    {
                        SOURCE_GOOGLE_KEEP: "Google Keep (Mot de passe d'application Google 16 lettres ou Master Token)",
                        SOURCE_HA_TODO: "Liste de courses Home Assistant (entité todo.*)",
                    }
                ),
                vol.Optional(CONF_GOOGLE_EMAIL, default=self._data.get(CONF_LECLERC_EMAIL, "")): str,
                vol.Optional(CONF_GOOGLE_APP_PASSWORD, default=""): str,
                vol.Optional(CONF_KEEP_NOTE_TITLE, default=DEFAULT_KEEP_NOTE_TITLE): str,
                vol.Optional(CONF_TODO_ENTITY_ID, default=default_todo): str,
            }
        )
        return self.async_show_form(step_id="shopping_list", data_schema=schema, errors=errors)

    async def async_step_ai_model(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Étape 3 : Choix de l'entité conversation.* Home Assistant OU qwen2.5:1.5b par défaut."""
        if user_input is not None:
            self._data.update(user_input)
            provider = user_input[CONF_AI_PROVIDER]
            if provider == AI_DEFAULT_OLLAMA:
                self._data[CONF_CONVERSATION_ENTITY] = None
            else:
                self._data[CONF_CONVERSATION_ENTITY] = provider

            return self.async_create_entry(
                title=f"E.Leclerc Drive ({self._data[CONF_LECLERC_EMAIL]})",
                data=self._data,
            )

        conv_choices = _get_conversation_choices(self.hass)
        schema = vol.Schema(
            {
                vol.Required(CONF_AI_PROVIDER, default=AI_DEFAULT_OLLAMA): vol.In(conv_choices),
                vol.Optional(CONF_OLLAMA_URL, default=DEFAULT_OLLAMA_URL): str,
                vol.Optional(CONF_OLLAMA_MODEL, default=DEFAULT_OLLAMA_MODEL): str,
                vol.Optional(CONF_PLURAL_ITEMS_COUNT, default=DEFAULT_PLURAL_COUNT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=6)
                ),
            }
        )
        return self.async_show_form(step_id="ai_model", data_schema=schema)
