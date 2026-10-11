"""Konstanten für BGG Collection Sync."""

DOMAIN = "bgg_collection_sync"

CONF_USERNAME = "username"
CONF_TOKEN = "token"
CONF_INTERVAL_HOURS = "interval_hours"
CONF_ONLY_OWNED = "only_owned"

DEFAULT_INTERVAL_HOURS = 12
DEFAULT_ONLY_OWNED = True

DB_FILENAME = "bgg_collection_sync.db"

SERVICE_SYNC_NOW = "sync_now"
SERVICE_GET_GAMES = "get_games"
