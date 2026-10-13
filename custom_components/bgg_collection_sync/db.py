"""SQLite-Speicher für die BGG-Sammlung, Metadaten und Partien."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from .api import VersionInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    username         TEXT    NOT NULL,
    bgg_id           INTEGER NOT NULL,
    subtype          TEXT    NOT NULL DEFAULT 'boardgame',
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
    collid           INTEGER,
    prev_owned       INTEGER NOT NULL DEFAULT 0,
    for_trade        INTEGER NOT NULL DEFAULT 0,
    status           TEXT,
    PRIMARY KEY (username, bgg_id)
);
CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY,
    bgg_id     INTEGER NOT NULL,
    name       TEXT,
    year       INTEGER,
    languages  TEXT
);
CREATE TABLE IF NOT EXISTS expansion_links (
    expansion_id INTEGER NOT NULL,
    base_id      INTEGER NOT NULL,
    base_name    TEXT,
    PRIMARY KEY (expansion_id, base_id)
);
CREATE TABLE IF NOT EXISTS expansion_checked (
    expansion_id INTEGER PRIMARY KEY
);
-- Spielinfos von BGG (Spielerzahl, Dauer, Typ, Modus)
CREATE TABLE IF NOT EXISTS game_info (
    bgg_id       INTEGER PRIMARY KEY,
    min_players  INTEGER,
    max_players  INTEGER,
    min_playtime INTEGER,
    max_playtime INTEGER,
    playing_time INTEGER,
    weight       REAL,
    categories   TEXT,
    mechanics    TEXT,
    game_types   TEXT,
    mode         TEXT
);
-- Eigene Zuordnungen je Spiel (überleben jeden Sync)
CREATE TABLE IF NOT EXISTS game_meta (
    username     TEXT    NOT NULL,
    bgg_id       INTEGER NOT NULL,
    location     TEXT,
    nfc_tag      TEXT,
    spotify_uri  TEXT,
    light_scene  TEXT,
    PRIMARY KEY (username, bgg_id)
);
-- Partien; ended_at IS NULL = läuft gerade
CREATE TABLE IF NOT EXISTS plays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    bgg_id       INTEGER NOT NULL,
    game_name    TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT,
    duration_min INTEGER,
    players      INTEGER,
    winner       TEXT,
    note         TEXT,
    source       TEXT    NOT NULL DEFAULT 'session'
);
CREATE INDEX IF NOT EXISTS idx_games_language ON games (username, version_language);
CREATE INDEX IF NOT EXISTS idx_plays_game ON plays (username, bgg_id);
"""

SEP = ", "
META_FIELDS = ("location", "nfc_tag", "spotify_uri", "light_scene")


def _split(value: str | None) -> list[str]:
    return [x for x in (value or "").split(SEP) if x]


def _loads(value: str | None) -> list[str]:
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []


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
            cols = {r["name"] for r in con.execute("PRAGMA table_info(games)")}
            if "subtype" not in cols:  # Migration von 0.1.0
                con.execute(
                    "ALTER TABLE games ADD COLUMN subtype TEXT NOT NULL DEFAULT 'boardgame'"
                )
            for col, decl in (  # Migration von 0.4.x
                ("collid", "INTEGER"),
                ("prev_owned", "INTEGER NOT NULL DEFAULT 0"),
                ("for_trade", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT"),
            ):
                if col not in cols:
                    con.execute(f"ALTER TABLE games ADD COLUMN {col} {decl}")

    # ---- Sync-Cache -------------------------------------------------------
    def known_version_ids(self) -> set[int]:
        with closing(self._connect()) as con:
            return {r[0] for r in con.execute("SELECT version_id FROM versions")}

    def checked_expansion_ids(self) -> set[int]:
        with closing(self._connect()) as con:
            return {r[0] for r in con.execute("SELECT expansion_id FROM expansion_checked")}

    def known_info_ids(self) -> set[int]:
        with closing(self._connect()) as con:
            return {r[0] for r in con.execute("SELECT bgg_id FROM game_info")}

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

    def upsert_info(self, info: dict[int, dict[str, Any]]) -> None:
        with closing(self._connect()) as con, con:
            con.executemany(
                "INSERT OR REPLACE INTO game_info (bgg_id, min_players, max_players, "
                "min_playtime, max_playtime, playing_time, weight, categories, mechanics, "
                "game_types, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        bgg_id,
                        d["min_players"],
                        d["max_players"],
                        d["min_playtime"],
                        d["max_playtime"],
                        d["playing_time"],
                        d["weight"],
                        json.dumps(d["categories"], ensure_ascii=False),
                        json.dumps(d["mechanics"], ensure_ascii=False),
                        json.dumps(d["game_types"]),
                        d["mode"],
                    )
                    for bgg_id, d in info.items()
                ],
            )

    def save_expansion_links(
        self, checked_ids: list[int], links: dict[int, list[tuple[int, str]]]
    ) -> None:
        with closing(self._connect()) as con, con:
            for exp_id in checked_ids:
                con.execute("DELETE FROM expansion_links WHERE expansion_id = ?", (exp_id,))
                con.executemany(
                    "INSERT OR REPLACE INTO expansion_links "
                    "(expansion_id, base_id, base_name) VALUES (?, ?, ?)",
                    [(exp_id, base_id, name) for base_id, name in links.get(exp_id, [])],
                )
                con.execute(
                    "INSERT OR IGNORE INTO expansion_checked (expansion_id) VALUES (?)",
                    (exp_id,),
                )

    def replace_games(self, username: str, rows: list[dict[str, Any]], synced_at: str) -> None:
        with closing(self._connect()) as con, con:
            con.executemany(
                """INSERT OR REPLACE INTO games
                   (username, bgg_id, subtype, name, year, image, thumbnail, owned,
                    wishlist, want_to_play, num_plays, user_rating, bgg_rating,
                    version_id, version_name, version_language, last_modified, synced_at,
                    collid, prev_owned, for_trade, status)
                   VALUES (:username, :bgg_id, :subtype, :name, :year, :image, :thumbnail,
                    :owned, :wishlist, :want_to_play, :num_plays, :user_rating,
                    :bgg_rating, :version_id, :version_name, :version_language,
                    :last_modified, :synced_at, :collid, :prev_owned, :for_trade, :status)""",
                [
                    {**r, "status": json.dumps(r["status"]), "username": username,
                     "synced_at": synced_at}
                    for r in rows
                ],
            )
            con.execute(
                "DELETE FROM games WHERE username = ? AND synced_at <> ?",
                (username, synced_at),
            )

    # ---- Lesen ------------------------------------------------------------
    def get_games(self, username: str) -> list[dict[str, Any]]:
        """Alle Einträge inkl. Spielinfos, Metadaten und gegenseitiger Verknüpfung."""
        with closing(self._connect()) as con:
            rows = con.execute(
                """SELECT g.*, i.min_players, i.max_players, i.min_playtime, i.max_playtime,
                          i.playing_time, i.weight, i.categories, i.mechanics, i.game_types,
                          i.mode, m.location, m.nfc_tag, m.spotify_uri, m.light_scene
                   FROM games g
                   LEFT JOIN game_info i ON i.bgg_id = g.bgg_id
                   LEFT JOIN game_meta m ON m.username = g.username AND m.bgg_id = g.bgg_id
                   WHERE g.username = ? ORDER BY g.name COLLATE NOCASE""",
                (username,),
            ).fetchall()
            links = con.execute(
                "SELECT expansion_id, base_id, base_name FROM expansion_links"
            ).fetchall()

        games = []
        for row in rows:
            game = dict(row)
            for key in ("owned", "wishlist", "want_to_play", "prev_owned", "for_trade"):
                game[key] = bool(game[key])
            try:
                game["status"] = json.loads(game["status"]) if game["status"] else {}
            except ValueError:
                game["status"] = {}
            game["languages"] = _split(game["version_language"])
            for key in ("categories", "mechanics", "game_types"):
                game[key] = _loads(game[key])
            for key in META_FIELDS:
                game[key] = game[key] or ""
            game["expansions"] = []
            game["base_games"] = []
            games.append(game)

        by_id = {g["bgg_id"]: g for g in games}
        for link in links:
            exp = by_id.get(link["expansion_id"])
            if exp is None:
                continue
            base = by_id.get(link["base_id"])
            exp["base_games"].append(
                {
                    "bgg_id": link["base_id"],
                    "name": base["name"] if base else link["base_name"],
                    "in_collection": base is not None,
                }
            )
            if base is not None:
                base["expansions"].append(
                    {
                        "bgg_id": exp["bgg_id"],
                        "name": exp["name"],
                        "language": exp["version_language"],
                    }
                )
        for game in games:
            game["expansions"].sort(key=lambda e: e["name"].lower())
            game["base_games"].sort(key=lambda b: b["name"].lower())
        return games

    # ---- Metadaten --------------------------------------------------------
    def set_meta(self, username: str, bgg_id: int, fields: dict[str, str]) -> None:
        """Setzt nur die übergebenen Felder; leerer String löscht das Feld."""
        assert fields and set(fields) <= set(META_FIELDS)
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT OR IGNORE INTO game_meta (username, bgg_id) VALUES (?, ?)",
                (username, bgg_id),
            )
            sets = ", ".join(f"{k} = ?" for k in fields)
            con.execute(
                f"UPDATE game_meta SET {sets} WHERE username = ? AND bgg_id = ?",
                (*[v or None for v in fields.values()], username, bgg_id),
            )

    # ---- Partien ----------------------------------------------------------
    def start_play(
        self, username: str, bgg_id: int, name: str, players: int | None, started_at: str
    ) -> int:
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "INSERT INTO plays (username, bgg_id, game_name, started_at, players, source) "
                "VALUES (?, ?, ?, ?, ?, 'session')",
                (username, bgg_id, name, started_at, players),
            )
            return cur.lastrowid

    def get_active_play(self, username: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM plays WHERE username = ? AND ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def end_play(
        self,
        play_id: int,
        ended_at: str,
        duration_min: int,
        players: int | None,
        winner: str | None,
        note: str | None,
    ) -> None:
        with closing(self._connect()) as con, con:
            con.execute(
                "UPDATE plays SET ended_at = ?, duration_min = ?, players = ?, winner = ?, "
                "note = ? WHERE id = ?",
                (ended_at, duration_min, players, winner, note, play_id),
            )

    def cancel_play(self, play_id: int) -> None:
        with closing(self._connect()) as con, con:
            con.execute("DELETE FROM plays WHERE id = ? AND ended_at IS NULL", (play_id,))

    def add_play(
        self,
        username: str,
        bgg_id: int,
        name: str,
        started_at: str,
        ended_at: str,
        duration_min: int | None,
        players: int | None,
        winner: str | None,
        note: str | None,
    ) -> int:
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "INSERT INTO plays (username, bgg_id, game_name, started_at, ended_at, "
                "duration_min, players, winner, note, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
                (username, bgg_id, name, started_at, ended_at, duration_min, players, winner, note),
            )
            return cur.lastrowid

    def get_plays(
        self, username: str, bgg_id: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM plays WHERE username = ? AND ended_at IS NOT NULL"
        args: list[Any] = [username]
        if bgg_id is not None:
            sql += " AND bgg_id = ?"
            args.append(bgg_id)
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        args.append(limit)
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def count_plays(self, username: str) -> int:
        with closing(self._connect()) as con:
            return con.execute(
                "SELECT COUNT(*) FROM plays WHERE username = ? AND ended_at IS NOT NULL",
                (username,),
            ).fetchone()[0]
