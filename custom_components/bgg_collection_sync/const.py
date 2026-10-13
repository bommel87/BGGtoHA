"""Konstanten für BGG Collection Sync."""

DOMAIN = "bgg_collection_sync"

CONF_USERNAME = "username"
CONF_TOKEN = "token"
CONF_INTERVAL_HOURS = "interval_hours"
CONF_ONLY_OWNED = "only_owned"

DEFAULT_INTERVAL_HOURS = 12
DEFAULT_ONLY_OWNED = True
CONF_INCLUDE_EXPANSIONS = "include_expansions"
DEFAULT_INCLUDE_EXPANSIONS = True

DB_FILENAME = "bgg_collection_sync.db"

SERVICE_SYNC_NOW = "sync_now"
SERVICE_GET_GAMES = "get_games"
CONF_MEDIA_PLAYER = "media_player"
CONF_END_SCENE = "end_scene"
CONF_PASSWORD = "bgg_password"  # nur zum Loggen von Partien auf BGG
CONF_UPLOAD_PLAYS = "upload_plays"
DEFAULT_UPLOAD_PLAYS = False
