"""SQLite-Speicher für die BGG-Sammlung, Metadaten und Partien."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from typing import Any

from .api import BggPlay, VersionInfo

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
    source       TEXT    NOT NULL DEFAULT 'session',
    bgg_play_id  INTEGER,
    player_names TEXT
);
-- Von BGG geladene Partien (Quelle für Mitspieler-Statistik)
CREATE TABLE IF NOT EXISTS bgg_plays (
    username   TEXT    NOT NULL,
    play_id    INTEGER NOT NULL,
    date       TEXT,
    bgg_id     INTEGER,
    game_name  TEXT,
    quantity   INTEGER NOT NULL DEFAULT 1,
    length     INTEGER,
    PRIMARY KEY (username, play_id)
);
CREATE TABLE IF NOT EXISTS bgg_play_players (
    username     TEXT    NOT NULL,
    play_id      INTEGER NOT NULL,
    pos          INTEGER NOT NULL,
    name         TEXT,
    bgg_username TEXT,
    bgg_userid   INTEGER,
    win          INTEGER NOT NULL DEFAULT 0,
    score        TEXT,
    PRIMARY KEY (username, play_id, pos)
);
-- Eigene Mitspieler-Einträge / Verknüpfung Name -> BGG-Account (überleben jeden Sync)
CREATE TABLE IF NOT EXISTS players (
    username     TEXT NOT NULL,
    name         TEXT NOT NULL COLLATE NOCASE,
    bgg_username TEXT,
    PRIMARY KEY (username, name)
);
CREATE INDEX IF NOT EXISTS idx_games_language ON games (username, version_language);
CREATE INDEX IF NOT EXISTS idx_plays_game ON plays (username, bgg_id);
"""

SEP = ", "
META_FIELDS = ("location", "nfc_tag", "spotify_uri", "light_scene")


def _split(value: str | None) -> list[str]:
    return [x for x in (value or "").split(SEP) if x]


def _dumps(value: list | None) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


def _loads(value: str | None) -> list:
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []


def _play(row: sqlite3.Row) -> dict[str, Any]:
    play = dict(row)
    play["player_names"] = _loads(play.get("player_names"))
    return play


def _key(name: str | None, bgg_username: str | None) -> str:
    """Identität eines Mitspielers: BGG-Account, sonst Name."""
    return f"u:{bgg_username.lower()}" if bgg_username else f"n:{(name or '').lower()}"


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
            play_cols = {r["name"] for r in con.execute("PRAGMA table_info(plays)")}
            if "bgg_play_id" not in play_cols:  # Migration von 0.5.x
                con.execute("ALTER TABLE plays ADD COLUMN bgg_play_id INTEGER")
            if "player_names" not in play_cols:  # Migration von 0.6.x
                con.execute("ALTER TABLE plays ADD COLUMN player_names TEXT")

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
        self,
        username: str,
        bgg_id: int,
        name: str,
        players: int | None,
        started_at: str,
        player_names: list[dict[str, Any]] | None = None,
    ) -> int:
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "INSERT INTO plays (username, bgg_id, game_name, started_at, players, source, "
                "player_names) VALUES (?, ?, ?, ?, ?, 'session', ?)",
                (username, bgg_id, name, started_at, players, _dumps(player_names)),
            )
            return cur.lastrowid

    def get_active_play(self, username: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM plays WHERE username = ? AND ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (username,),
            ).fetchone()
        return _play(row) if row else None

    def end_play(
        self,
        play_id: int,
        ended_at: str,
        duration_min: int,
        players: int | None,
        winner: str | None,
        note: str | None,
        player_names: list[dict[str, Any]] | None = None,
    ) -> None:
        with closing(self._connect()) as con, con:
            con.execute(
                "UPDATE plays SET ended_at = ?, duration_min = ?, players = ?, winner = ?, "
                "note = ?, player_names = ? WHERE id = ?",
                (ended_at, duration_min, players, winner, note, _dumps(player_names), play_id),
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
        player_names: list[dict[str, Any]] | None = None,
    ) -> int:
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "INSERT INTO plays (username, bgg_id, game_name, started_at, ended_at, "
                "duration_min, players, winner, note, source, player_names) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
                (username, bgg_id, name, started_at, ended_at, duration_min, players, winner,
                 note, _dumps(player_names)),
            )
            return cur.lastrowid

    def get_play(self, username: str, play_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM plays WHERE username = ? AND id = ?", (username, play_id)
            ).fetchone()
        return _play(row) if row else None

    def set_bgg_play_id(self, play_id: int, bgg_play_id: int | None) -> None:
        with closing(self._connect()) as con, con:
            con.execute("UPDATE plays SET bgg_play_id = ? WHERE id = ?", (bgg_play_id, play_id))

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
            return [_play(r) for r in con.execute(sql, args).fetchall()]

    def count_plays(self, username: str) -> int:
        with closing(self._connect()) as con:
            return con.execute(
                "SELECT COUNT(*) FROM plays WHERE username = ? AND ended_at IS NOT NULL",
                (username,),
            ).fetchone()[0]

    # ---- Mitspieler -------------------------------------------------------
    def known_bgg_play_ids(self, username: str) -> set[int]:
        with closing(self._connect()) as con:
            return {
                r[0]
                for r in con.execute("SELECT play_id FROM bgg_plays WHERE username = ?", (username,))
            }

    def save_bgg_plays(self, username: str, plays: list[BggPlay]) -> None:
        with closing(self._connect()) as con, con:
            for p in plays:
                con.execute(
                    "INSERT OR REPLACE INTO bgg_plays (username, play_id, date, bgg_id, "
                    "game_name, quantity, length) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (username, p.play_id, p.date, p.bgg_id, p.game_name, p.quantity, p.length),
                )
                con.execute(
                    "DELETE FROM bgg_play_players WHERE username = ? AND play_id = ?",
                    (username, p.play_id),
                )
                con.executemany(
                    "INSERT INTO bgg_play_players (username, play_id, pos, name, bgg_username, "
                    "bgg_userid, win, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (username, p.play_id, i, pl.name, pl.bgg_username, pl.bgg_userid,
                         int(pl.win), pl.score)
                        for i, pl in enumerate(p.players)
                    ],
                )

    def set_player(self, username: str, name: str, bgg_username: str | None) -> None:
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT INTO players (username, name, bgg_username) VALUES (?, ?, ?) "
                "ON CONFLICT (username, name) DO UPDATE SET bgg_username = excluded.bgg_username",
                (username, name, bgg_username),
            )

    def remove_player(self, username: str, name: str) -> bool:
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "DELETE FROM players WHERE username = ? AND name = ?", (username, name)
            )
            return cur.rowcount > 0

    def get_players(self, username: str) -> list[dict[str, Any]]:
        """Alle Mitspieler (ohne dich selbst) mit Statistik aus BGG- und lokalen Partien."""
        with closing(self._connect()) as con:
            manual = con.execute(
                "SELECT name, bgg_username FROM players WHERE username = ?", (username,)
            ).fetchall()
            bgg_rows = con.execute(
                """SELECT p.play_id, p.date, p.game_name, pp.name, pp.bgg_username,
                          pp.bgg_userid, pp.win
                   FROM bgg_play_players pp
                   JOIN bgg_plays p ON p.username = pp.username AND p.play_id = pp.play_id
                   WHERE pp.username = ?
                   ORDER BY p.date""",
                (username,),
            ).fetchall()
            # Lokale Partien, die (noch) nicht auf BGG sind
            local_rows = con.execute(
                "SELECT id, started_at, game_name, player_names FROM plays "
                "WHERE username = ? AND ended_at IS NOT NULL AND bgg_play_id IS NULL "
                "AND player_names IS NOT NULL",
                (username,),
            ).fetchall()

        me = username.lower()
        # Name -> BGG-Account aus eigenen Einträgen (verknüpft auch reine Namen aus BGG-Partien)
        links = {r["name"].lower(): r["bgg_username"] for r in manual if r["bgg_username"]}
        players: dict[str, dict[str, Any]] = {}

        def entry(name: str | None, bgg_username: str | None) -> dict[str, Any] | None:
            bgg_username = bgg_username or links.get((name or "").lower())
            if bgg_username and bgg_username.lower() == me:
                return None
            key = _key(name, bgg_username)
            if key == "n:":
                return None
            return players.setdefault(
                key,
                {
                    "key": key,
                    "name": name or bgg_username,
                    "bgg_username": bgg_username,
                    "bgg_userid": None,
                    "manual": False,
                    "plays": set(),
                    "wins": 0,
                    "last_played": None,
                    "games": Counter(),
                },
            )

        def count(p: dict[str, Any], play_key: str, date: str | None, game: str, win: bool) -> None:
            if play_key in p["plays"]:
                return
            p["plays"].add(play_key)
            p["wins"] += int(win)
            p["games"][game] += 1
            if date and (p["last_played"] is None or date >= p["last_played"]):
                p["last_played"] = date

        # nach Datum sortiert -> der zuletzt verwendete Name bleibt stehen
        for r in bgg_rows:
            p = entry(r["name"], r["bgg_username"])
            if p is None:
                continue
            p["bgg_userid"] = p["bgg_userid"] or r["bgg_userid"]
            if r["name"]:
                p["name"] = r["name"]
            count(p, f"b{r['play_id']}", r["date"], r["game_name"] or "", bool(r["win"]))
        for r in local_rows:
            date = (r["started_at"] or "")[:10] or None
            for pl in _loads(r["player_names"]):
                p = entry(pl.get("name"), pl.get("bgg_username"))
                if p is not None:
                    count(p, f"l{r['id']}", date, r["game_name"], bool(pl.get("win")))
        for r in manual:  # eigene Einträge: Name hat Vorrang, auch ohne Partien
            p = entry(r["name"], r["bgg_username"])
            if p is not None:
                p["name"] = r["name"]
                p["manual"] = True

        result = []
        for p in players.values():
            plays = len(p.pop("plays"))
            games = p.pop("games")
            result.append(
                {
                    **p,
                    "plays": plays,
                    "win_rate": round(p["wins"] / plays * 100) if plays else None,
                    "top_games": [g for g, _ in games.most_common(5) if g],
                }
            )
        result.sort(key=lambda p: (-p["plays"], p["name"].lower()))
        return result
