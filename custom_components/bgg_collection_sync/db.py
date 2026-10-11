"""SQLite-Speicher für die BGG-Sammlung."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from .api import VersionInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    username         TEXT    NOT NULL,
    bgg_id           INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    year             INTEGER,
    image            TEXT,
    thumbnail        TEXT,
    owned            INTEGER NOT NULL DEFAULT 0,
    wishlist         INTEGER NOT NULL DEFAULT 0,
    want_to_play     INTEGER NOT NULL DEFAULT 0,
    num_plays        INTEGER NOT NULL DEFAULT 0,
    user_rating      REAL,
    bgg_rating       REAL,
    version_id       INTEGER,
    version_name     TEXT,
    version_language TEXT,
    last_modified    TEXT,
    synced_at        TEXT    NOT NULL,
    PRIMARY KEY (username, bgg_id)
);
CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY,
    bgg_id     INTEGER NOT NULL,
    name       TEXT,
    year       INTEGER,
    languages  TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_language ON games (username, version_language);
"""

SEP = ", "


def _split(value: str | None) -> list[str]:
    return [x for x in (value or "").split(SEP) if x]


class BggDatabase:
    """Alle Methoden sind blockierend -> im Executor aufrufen."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        return con

    def setup(self) -> None:
        with closing(self._connect()) as con, con:
            con.executescript(SCHEMA)

    def known_version_ids(self) -> set[int]:
        with closing(self._connect()) as con:
            return {r[0] for r in con.execute("SELECT version_id FROM versions")}

    def get_version_languages(self) -> dict[int, list[str]]:
        with closing(self._connect()) as con:
            return {
                r["version_id"]: _split(r["languages"])
                for r in con.execute("SELECT version_id, languages FROM versions")
            }

    def upsert_versions(self, versions: list[VersionInfo]) -> None:
        with closing(self._connect()) as con, con:
            con.executemany(
                "INSERT OR REPLACE INTO versions "
                "(version_id, bgg_id, name, year, languages) VALUES (?, ?, ?, ?, ?)",
                [
                    (v.version_id, v.bgg_id, v.name, v.year, SEP.join(v.languages))
                    for v in versions
                ],
            )

    def replace_games(self, username: str, rows: list[dict[str, Any]], synced_at: str) -> None:
        """Schreibt die Sammlung; nicht mehr vorhandene Spiele werden entfernt."""
        with closing(self._connect()) as con, con:
            con.executemany(
                """INSERT OR REPLACE INTO games
                   (username, bgg_id, name, year, image, thumbnail, owned, wishlist,
                    want_to_play, num_plays, user_rating, bgg_rating, version_id,
                    version_name, version_language, last_modified, synced_at)
                   VALUES (:username, :bgg_id, :name, :year, :image, :thumbnail, :owned,
                    :wishlist, :want_to_play, :num_plays, :user_rating, :bgg_rating,
                    :version_id, :version_name, :version_language, :last_modified,
                    :synced_at)""",
                [{**r, "username": username, "synced_at": synced_at} for r in rows],
            )
            con.execute(
                "DELETE FROM games WHERE username = ? AND synced_at <> ?",
                (username, synced_at),
            )

    def get_games(self, username: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM games WHERE username = ? ORDER BY name COLLATE NOCASE",
                (username,),
            ).fetchall()
        games = []
        for row in rows:
            game = dict(row)
            for key in ("owned", "wishlist", "want_to_play"):
                game[key] = bool(game[key])
            game["languages"] = _split(game["version_language"])
            games.append(game)
        return games
