"""Reine Hilfsfunktionen (ohne Home-Assistant-Abhängigkeit)."""
from __future__ import annotations

import re
from typing import Any

GAME_TYPES = ("any", "family", "strategy", "party", "abstract", "thematic", "wargame", "children")
MODES = ("any", "cooperative", "competitive")

MODE_LABELS = {
    "cooperative": "Kooperativ",
    "competitive": "Wettkampf",
    "semi": "Semi-kooperativ",
}

_SPOTIFY_URL = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?"
    r"(playlist|album|artist|track|show|episode)/([A-Za-z0-9]+)"
)


def normalize_spotify(value: str) -> str:
    """Wandelt Spotify-Links in URIs um. Leer = löschen. ValueError bei Unsinn."""
    v = (value or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"spotify:(playlist|album|artist|track|show|episode):[A-Za-z0-9]+", v):
        return v
    m = _SPOTIFY_URL.match(v)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    raise ValueError(v)


def spotify_media_type(uri: str) -> str:
    parts = uri.split(":")
    return parts[1] if len(parts) >= 3 else "playlist"


def matches(
    game: dict[str, Any],
    players: int | None,
    mode: str,
    game_type: str,
    max_playtime: int | None,
) -> bool:
    """Passt ein Spiel zu den Filtern? Unbekannte Daten passen nur zu "egal"."""
    if players is not None:
        lo, hi = game.get("min_players"), game.get("max_players")
        if lo is None or hi is None or not (lo <= players <= hi):
            return False
    game_mode = game.get("mode")
    if mode == "cooperative" and game_mode != "cooperative":
        return False
    if mode == "competitive" and game_mode not in ("competitive", "semi"):
        return False
    if game_type != "any" and game_type not in (game.get("game_types") or []):
        return False
    if max_playtime and (game.get("playing_time") or 0) > max_playtime:
        return False
    return True


def filter_games(
    games: list[dict[str, Any]],
    players: int | None = None,
    mode: str = "any",
    game_type: str = "any",
    max_playtime: int | None = None,
) -> list[dict[str, Any]]:
    return [
        g
        for g in games
        if g.get("owned") and matches(g, players, mode, game_type, max_playtime)
    ]


def summary_line(game: dict[str, Any]) -> str:
    """Kurzbeschreibung für kleine Displays, z. B. '2-4 Spieler | 45 Min | Kooperativ'."""
    parts = []
    lo, hi = game.get("min_players"), game.get("max_players")
    if lo and hi:
        parts.append(f"{lo} Spieler" if lo == hi else f"{lo}-{hi} Spieler")
    if game.get("playing_time"):
        parts.append(f"{game['playing_time']} Min")
    if game.get("mode") in MODE_LABELS:
        parts.append(MODE_LABELS[game["mode"]])
    return " | ".join(parts)
