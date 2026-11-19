"""Constantes de l'intégration Home Assistant E.Leclerc Drive AI."""

DOMAIN = "leclerc_drive_ai"
PLATFORMS = ["button", "sensor"]

# Configuration E.Leclerc Drive
CONF_LECLERC_EMAIL = "leclerc_email"
CONF_LECLERC_PASSWORD = "leclerc_password"
CONF_MAGASIN_SLUG = "magasin_slug"
CONF_OTP_CODE = "otp_code"

# Configuration Liste de courses (Google Keep ou Todo HA)
CONF_LIST_SOURCE = "list_source"
SOURCE_GOOGLE_KEEP = "google_keep"
SOURCE_HA_TODO = "ha_todo"

CONF_GOOGLE_EMAIL = "google_email"
CONF_GOOGLE_APP_PASSWORD = "google_app_password"
CONF_GOOGLE_MASTER_TOKEN = "google_master_token"
CONF_KEEP_NOTE_TITLE = "keep_note_title"
CONF_TODO_ENTITY_ID = "todo_entity_id"

# Configuration IA (Entité conversation.* Home Assistant ou Ollama qwen2.5:1.5b par défaut)
CONF_AI_PROVIDER = "ai_provider"
AI_DEFAULT_OLLAMA = "ollama_qwen2_5_1_5b"
CONF_CONVERSATION_ENTITY = "conversation_entity"
CONF_OLLAMA_URL = "ollama_url"
CONF_OLLAMA_MODEL = "ollama_model"
CONF_PLURAL_ITEMS_COUNT = "plural_items_count"

DEFAULT_MAGASIN_SLUG = "magasin-125401-125401-Vandoeuvre-les-Nancy"
DEFAULT_KEEP_NOTE_TITLE = "Liste de courses"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:1.5b"
DEFAULT_PLURAL_COUNT = 3
