"""Asynchroner Client für die BoardGameGeek XML API2."""
from __future__ import annotations

import asyncio
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://boardgamegeek.com/xmlapi2"
USER_AGENT = "HomeAssistant-bgg-collection-sync/0.1.0"
MAX_ATTEMPTS = 8
RETRY_DELAY = 5  # Sekunden (BGG antwortet bei Collections zuerst oft mit 202)
THING_BATCH_SIZE = 20  # BGG erlaubt max. 20 IDs pro /thing-Abfrage
PLAYS_PAGE_SIZE = 100  # BGG liefert 100 Partien pro Seite
EXPANSION = "boardgameexpansion"

# BGG-Subdomains (Rank-Namen) -> interner Spieletyp
SUBDOMAIN_TYPES = {
    "strategygames": "strategy",
    "familygames": "family",
    "partygames": "party",
    "abstracts": "abstract",
    "thematic": "thematic",
    "wargames": "wargame",
    "childrensgames": "children",
}
# Fallback für nicht gerankte Spiele
CATEGORY_TYPES = {
    "Party Game": "party",
    "Children's Game": "children",
    "Abstract Strategy": "abstract",
    "Wargame": "wargame",
}
MECHANIC_COOP = "Cooperative Game"
MECHANIC_SEMI = "Semi-Cooperative Game"


class BggError(Exception):
    """Allgemeiner API-Fehler."""


class BggAuthError(BggError):
    """Token fehlt oder ist ungültig."""


class BggNotFoundError(BggError):
    """Benutzer existiert nicht."""


@dataclass
class CollectionItem:
    """Ein Eintrag der Collection."""

    bgg_id: int
    name: str
    year: int | None
    image: str | None
    thumbnail: str | None
    owned: bool
    wishlist: bool
    want_to_play: bool
    num_plays: int
    user_rating: float | None
    bgg_rating: float | None
    last_modified: str | None
    version_id: int | None = None
    version_name: str | None = None
    languages: list[str] = field(default_factory=list)  # falls die Collection sie liefert
    subtype: str = "boardgame"
    collid: int | None = None  # ID des Sammlungseintrags (zum Ändern/Löschen auf BGG)
    prev_owned: bool = False
    for_trade: bool = False
    status: dict[str, str] = field(default_factory=dict)  # alle BGG-Statusflags


@dataclass
class VersionInfo:
    """Eine Ausgabe (Version) eines Spiels."""

    version_id: int
    bgg_id: int
    name: str | None
    year: int | None
    languages: list[str]


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _child_int(el: ET.Element, tag: str) -> int | None:
    child = el.find(tag)
    return _to_int(child.get("value")) if child is not None else None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class PlayPlayer:
    """Ein Mitspieler einer BGG-Partie."""

    name: str
    bgg_username: str | None
    bgg_userid: int | None
    win: bool
    score: str | None


@dataclass
class BggPlay:
    """Eine auf BGG geloggte Partie."""

    play_id: int
    date: str | None
    bgg_id: int | None
    game_name: str
    quantity: int
    length: int | None
    players: list[PlayPlayer]


@dataclass
class ThingData:
    """Ergebnis einer /thing-Abfrage."""

    versions: list[VersionInfo]
    # Erweiterung -> Liste der Basisspiele (id, name), die sie erweitert
    base_links: dict[int, list[tuple[int, str]]]
    # Spielinfos (Spielerzahl, Dauer, Typ, Modus ...) je BGG-ID
    info: dict[int, dict[str, Any]]


class BggClient:
    """Dünner Client um die BGG XML API2."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, token: str | None = None
    ) -> None:
        self._session = session
        self.username = username
        self._token = token or None

    async def _get(self, endpoint: str, params: dict) -> ET.Element:
        headers = {"User-Agent": USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._session.get(
                    f"{API_BASE}/{endpoint}",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 202:  # Anfrage in Warteschlange
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    if resp.status == 429:  # Rate-Limit
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    if resp.status in (401, 403):
                        raise BggAuthError("BGG lehnt den Zugriff ab (Token prüfen)")
                    if resp.status != 200:
                        raise BggError(f"BGG antwortete mit HTTP {resp.status}")
                    text = await resp.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise BggError(f"Verbindung zu BGG fehlgeschlagen: {err}") from err

            try:
                return ET.fromstring(text)
            except ET.ParseError as err:
                raise BggError("Ungültige XML-Antwort von BGG") from err

        raise BggError("BGG hat die Daten nicht rechtzeitig geliefert")

    async def validate_user(self) -> None:
        """Prüft Token und Benutzernamen."""
        root = await self._get("user", {"name": self.username})
        if not root.get("id"):
            raise BggNotFoundError(self.username)

    async def fetch_plays(self, page: int = 1) -> tuple[int, list[BggPlay]]:
        """Eine Seite der geloggten Partien (neueste zuerst); liefert (Gesamtzahl, Partien)."""
        root = await self._get("plays", {"username": self.username, "page": page})
        plays = []
        for el in root.findall("play"):
            play_id = _to_int(el.get("id"))
            if play_id is None:
                continue
            item = el.find("item")
            players = [
                PlayPlayer(
                    name=(p.get("name") or "").strip(),
                    bgg_username=(p.get("username") or "").strip() or None,
                    bgg_userid=_to_int(p.get("userid")) or None,
                    win=p.get("win") == "1",
                    score=(p.get("score") or "").strip() or None,
                )
                for p in el.findall("players/player")
            ]
            plays.append(
                BggPlay(
                    play_id=play_id,
                    date=el.get("date"),
                    bgg_id=_to_int(item.get("objectid")) if item is not None else None,
                    game_name=item.get("name", "") if item is not None else "",
                    quantity=_to_int(el.get("quantity")) or 1,
                    length=_to_int(el.get("length")) or None,
                    players=[p for p in players if p.name or p.bgg_username],
                )
            )
        return _to_int(root.get("total")) or 0, plays

    async def user_exists(self, username: str) -> bool:
        """Prüft, ob es den BGG-Benutzer gibt."""
        root = await self._get("user", {"name": username})
        return bool(root.get("id"))

    async def fetch_collection(
        self, only_owned: bool, expansions: bool = False
    ) -> list[CollectionItem]:
        """Lädt Spiele (oder Erweiterungen) inkl. der in der Collection gewählten Version."""
        params: dict = {
            "username": self.username,
            "version": 1,
            "stats": 1,
        }
        if expansions:
            params["subtype"] = EXPANSION
        else:
            # BGG liefert Erweiterungen sonst im Standard-Subtype mit
            params["subtype"] = "boardgame"
            params["excludesubtype"] = EXPANSION
        root = await self._get("collection", params)
        items = [self._parse_item(el) for el in root.findall("item")]
        if only_owned:
            # Lokal filtern statt own=1, damit auch "früher besessen" mitkommt
            items = [i for i in items if i.owned or i.prev_owned]
        return items

    @staticmethod
    def _parse_item(item: ET.Element) -> CollectionItem:
        status = item.find("status")

        def flag(name: str) -> bool:
            return status is not None and status.get(name) == "1"

        rating_el = item.find("stats/rating")
        avg_el = item.find("stats/rating/average")

        ver = item.find("version/item")
        version_id = version_name = None
        languages: list[str] = []
        if ver is not None:
            version_id = _to_int(ver.get("id"))
            name_el = ver.find("name")
            version_name = name_el.get("value") if name_el is not None else None
            languages = [
                link.get("value")
                for link in ver.findall("link[@type='language']")
                if link.get("value")
            ]

        return CollectionItem(
            bgg_id=int(item.get("objectid")),
            name=(item.findtext("name") or "").strip(),
            year=_to_int(item.findtext("yearpublished")),
            image=item.findtext("image"),
            thumbnail=item.findtext("thumbnail"),
            owned=flag("own"),
            wishlist=flag("wishlist"),
            want_to_play=flag("wanttoplay"),
            num_plays=_to_int(item.findtext("numplays")) or 0,
            user_rating=_to_float(rating_el.get("value")) if rating_el is not None else None,
            bgg_rating=_to_float(avg_el.get("value")) if avg_el is not None else None,
            last_modified=status.get("lastmodified") if status is not None else None,
            version_id=version_id,
            version_name=version_name,
            languages=languages,
            subtype=item.get("subtype") or "boardgame",
            collid=_to_int(item.get("collid")),
            prev_owned=flag("prevowned"),
            for_trade=flag("fortrade"),
            status={k: v for k, v in (status.attrib if status is not None else {}).items()
                    if k != "lastmodified"},
        )

    async def fetch_things(self, ids: list[int]) -> ThingData:
        """Lädt Versionen, Erweiterungs-Verknüpfungen und Spielinfos für bis zu 20 IDs."""
        root = await self._get(
            "thing", {"id": ",".join(map(str, ids)), "versions": 1, "stats": 1}
        )
        versions: list[VersionInfo] = []
        base_links: dict[int, list[tuple[int, str]]] = {}
        info: dict[int, dict[str, Any]] = {}
        for game in root.findall("item"):
            game_id = _to_int(game.get("id"))
            if game_id is None:
                continue

            # inbound="true" bei einer Erweiterung = Basisspiel(e), die sie erweitert
            for link in game.findall("link[@type='boardgameexpansion']"):
                base_id = _to_int(link.get("id"))
                if link.get("inbound") == "true" and base_id is not None:
                    base_links.setdefault(game_id, []).append(
                        (base_id, link.get("value") or "")
                    )

            categories = [
                l.get("value") for l in game.findall("link[@type='boardgamecategory']")
            ]
            mechanics = [
                l.get("value") for l in game.findall("link[@type='boardgamemechanic']")
            ]
            types = {CATEGORY_TYPES[c] for c in categories if c in CATEGORY_TYPES}
            for rank in game.findall("statistics/ratings/ranks/rank[@type='family']"):
                kind = SUBDOMAIN_TYPES.get(rank.get("name"))
                if kind and rank.get("value") not in (None, "Not Ranked"):
                    types.add(kind)
            if MECHANIC_COOP in mechanics:
                mode = "cooperative"
            elif MECHANIC_SEMI in mechanics:
                mode = "semi"
            else:
                mode = "competitive"
            weight_el = game.find("statistics/ratings/averageweight")
            info[game_id] = {
                "min_players": _child_int(game, "minplayers"),
                "max_players": _child_int(game, "maxplayers"),
                "min_playtime": _child_int(game, "minplaytime"),
                "max_playtime": _child_int(game, "maxplaytime"),
                "playing_time": _child_int(game, "playingtime"),
                "weight": _to_float(weight_el.get("value")) if weight_el is not None else None,
                "categories": categories,
                "mechanics": mechanics,
                "game_types": sorted(types),
                "mode": mode,
            }

            for ver in game.findall("versions/item"):
                version_id = _to_int(ver.get("id"))
                if version_id is None:
                    continue
                name_el = ver.find("name")
                year_el = ver.find("yearpublished")
                versions.append(
                    VersionInfo(
                        version_id=version_id,
                        bgg_id=game_id,
                        name=name_el.get("value") if name_el is not None else None,
                        year=_to_int(year_el.get("value")) if year_el is not None else None,
                        languages=[
                            link.get("value")
                            for link in ver.findall("link[@type='language']")
                            if link.get("value")
                        ],
                    )
                )
        return ThingData(versions=versions, base_links=base_links, info=info)


# ---- Partien auf BGG loggen (inoffizielle Website-Schnittstelle) ---------
# Die XML API kann nur lesen. Partien speichert die BGG-Webseite über
# geekplay.php – dafür ist ein Login mit Benutzername + Passwort nötig.
WEB_BASE = "https://boardgamegeek.com"
WEB_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
}
CLOUDFLARE_MSG = "Cloudflare (Schutz vor BGG) hat die Anfrage blockiert – später erneut versuchen"


def _is_cloudflare(resp: aiohttp.ClientResponse, text: str) -> bool:
    if resp.headers.get("cf-mitigated"):
        return True
    lowered = text[:2000].lower()
    return resp.status in (403, 429, 503) and (
        "cloudflare" in lowered or "just a moment" in lowered
    )


def _parse_set_cookies(values: list[str]) -> dict[str, str]:
    """Set-Cookie-Header -> {Name: Wert}; gelöschte Cookies fallen weg.

    Bewusst ohne http.cookies: dessen strenger Parser verwirft bei ungewöhnlichen
    Werten/Attributen den ganzen Header (dann fehlt die BGG-Sitzung).
    """
    cookies: dict[str, str] = {}
    for raw in values:
        name, sep, value = raw.split(";", 1)[0].strip().partition("=")
        if not sep or not name:
            continue
        attrs = raw.lower()
        if value in ("", "deleted") or "max-age=0" in attrs or "expires=thu, 01-jan-1970" in attrs:
            cookies.pop(name, None)
        else:
            cookies[name] = value
    return cookies


class BggWebClient:
    """Loggt Partien über die BGG-Webseite (benötigt das BGG-Passwort)."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session  # eigene Session mit Cookie-Jar
        self._username = username
        self._password = password
        self._logged_in = False
        # Sitzungs-Cookies aus dem Login; werden zusätzlich zum Cookie-Jar explizit gesendet
        self._cookies: dict[str, str] = {}

    async def _request(
        self, url: str, payload: dict[str, Any]
    ) -> tuple[int, str, list[str]]:
        headers = {**WEB_HEADERS, "Origin": WEB_BASE, "Referer": f"{WEB_BASE}/"}
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        try:
            async with self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                if _is_cloudflare(resp, text):
                    _LOGGER.debug("Cloudflare-Block: HTTP %s %s", resp.status, text[:200])
                    raise BggError(CLOUDFLARE_MSG)
                return resp.status, text, resp.headers.getall("Set-Cookie", [])
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BggError(f"Verbindung zu BGG fehlgeschlagen: {err}") from err

    async def login(self) -> None:
        self._cookies = {}
        status, text, set_cookies = await self._request(
            f"{WEB_BASE}/login/api/v1",
            {"credentials": {"username": self._username, "password": self._password}},
        )
        self._cookies = _parse_set_cookies(set_cookies)
        _LOGGER.debug("BGG-Login: HTTP %s, Cookies: %s", status, sorted(self._cookies))
        if status in (400, 401, 403):
            raise BggAuthError("BGG-Login fehlgeschlagen (Benutzername/Passwort prüfen)")
        if status >= 300:
            raise BggError(f"BGG-Login: HTTP {status}")
        if not self._cookies:
            raise BggAuthError("BGG-Login ohne Sitzung: BGG hat keine Cookies gesetzt")
        self._logged_in = True

    async def log_play(
        self,
        bgg_id: int,
        playdate: str,
        length_min: int | None = None,
        comments: str | None = None,
        players: list[dict[str, Any]] | None = None,
    ) -> int | None:
        """Speichert eine Partie auf BGG; liefert die BGG-Play-ID."""
        payload: dict[str, Any] = {
            "ajax": 1,
            "action": "save",
            "objecttype": "thing",
            "objectid": bgg_id,
            "playdate": playdate,
            "quantity": 1,
            "length": length_min or 0,
            "comments": comments or "",
            "players": players or [],
        }
        for attempt in range(2):
            if not self._logged_in:
                await self.login()
            status, text, _ = await self._request(f"{WEB_BASE}/geekplay.php", payload)
            _LOGGER.debug("BGG geekplay: HTTP %s %s", status, text[:300])
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            error = str(data.get("error", "")) if isinstance(data, dict) else ""
            needs_login = status in (401, 403) or "login" in error.lower()
            if needs_login and attempt == 0:
                self._logged_in = False  # Session abgelaufen -> einmal neu anmelden
                continue
            if needs_login:
                raise BggAuthError(
                    "BGG hat die Partie abgelehnt (nicht angemeldet). "
                    f"Cookies vom Login: {', '.join(sorted(self._cookies)) or 'keine'}"
                )
            if status != 200 or not isinstance(data, dict):
                raise BggError(f"Unerwartete Antwort von BGG (HTTP {status})")
            if error:
                raise BggError(f"BGG: {error}")
            return _to_int(data.get("playid"))
        raise BggError("BGG-Upload fehlgeschlagen")
