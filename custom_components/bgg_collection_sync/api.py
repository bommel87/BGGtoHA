"""Asynchroner Client für die BoardGameGeek XML API2."""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://boardgamegeek.com/xmlapi2"
USER_AGENT = "HomeAssistant-bgg-collection-sync/0.1.0"
MAX_ATTEMPTS = 8
RETRY_DELAY = 5  # Sekunden (BGG antwortet bei Collections zuerst oft mit 202)
THING_BATCH_SIZE = 20  # BGG erlaubt max. 20 IDs pro /thing-Abfrage
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

# ---- Schreiben (inoffizielle Website-Schnittstelle) ----------------------
# Die XML API kann nur lesen. Status ändern/löschen geht nur wie im Browser:
# Login mit Benutzername + Passwort, dann geekcollection.php mit Session-Cookie.
WEB_BASE = "https://boardgamegeek.com"
STATUS_FLAGS = (
    "own", "prevowned", "fortrade", "want", "wanttoplay", "wanttobuy", "wishlist", "preordered"
)
# Zielstatus -> Flags, die gesetzt werden (alle anderen Flags bleiben wie sie sind)
STATUS_TARGETS = {
    "owned": {"own": True, "prevowned": False, "fortrade": False},
    "for_sale": {"own": True, "prevowned": False, "fortrade": True},
    "prev_owned": {"own": False, "prevowned": True, "fortrade": False},
}


# Cloudflare vor BGG blockt unbekannte Programme -> wie ein Browser auftreten
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
CLOUDFLARE_MSG = "Cloudflare (Schutz vor BGG) hat die Anfrage blockiert – später erneut versuchen"


def _is_cloudflare(status: int, text: str) -> bool:
    return status in (403, 503) and "cloudflare" in text.lower()


class BggWebClient:
    """Ändert die Sammlung über die BGG-Webseite (benötigt das BGG-Passwort)."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session  # eigene Session mit Cookie-Jar
        self._username = username
        self._password = password
        self._logged_in = False

    async def login(self) -> None:
        try:
            async with self._session.post(
                f"{WEB_BASE}/login/api/v1",
                json={"credentials": {"username": self._username, "password": self._password}},
                headers=WEB_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                _LOGGER.debug("BGG-Login: HTTP %s %s", resp.status, text[:200])
                if _is_cloudflare(resp.status, text):
                    raise BggError(CLOUDFLARE_MSG)
                if resp.status in (400, 401, 403):
                    raise BggAuthError("BGG-Login fehlgeschlagen (Benutzername/Passwort prüfen)")
                if resp.status >= 300:
                    raise BggError(f"BGG-Login: HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BggError(f"Verbindung zu BGG fehlgeschlagen: {err}") from err
        if not self._session.cookie_jar.filter_cookies(URL(WEB_BASE)):
            raise BggAuthError("BGG-Login ohne Sitzung (Benutzername/Passwort prüfen)")
        self._logged_in = True

    async def _post(self, data: dict[str, str]) -> None:
        for attempt in range(2):
            if not self._logged_in:
                await self.login()
            try:
                async with self._session.post(
                    f"{WEB_BASE}/geekcollection.php",
                    data=data,
                    headers={
                        **WEB_HEADERS,
                        "Origin": WEB_BASE,
                        "Referer": f"{WEB_BASE}/collection/user/{self._username}",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                    status = resp.status
            except (aiohttp.ClientError, TimeoutError) as err:
                raise BggError(f"Verbindung zu BGG fehlgeschlagen: {err}") from err
            _LOGGER.debug("BGG %s: HTTP %s %s", data.get("action"), status, text[:300])
            if _is_cloudflare(status, text):
                raise BggError(CLOUDFLARE_MSG)
            needs_login = status in (401, 403) or "must login" in text.lower()
            if needs_login and attempt == 0:
                self._logged_in = False  # Session abgelaufen -> einmal neu anmelden
                continue
            if needs_login:
                raise BggAuthError("BGG hat die Änderung abgelehnt (nicht angemeldet)")
            if status != 200:
                raise BggError(f"BGG antwortete mit HTTP {status}")
            return
    async def set_status(
        self, collid: int, bgg_id: int, current: dict[str, str], target: str
    ) -> dict[str, str]:
        """Setzt den Status; liefert die neuen Flags zurück."""
        flags = {k: current.get(k) == "1" for k in STATUS_FLAGS}
        flags.update(STATUS_TARGETS[target])
        data = {
            "ajax": "1",
            "action": "savedata",
            "collid": str(collid),
            "objecttype": "thing",
            "objectid": str(bgg_id),
            "fieldname": "status",
        }
        data.update({k: "1" for k, on in flags.items() if on})
        if flags["wishlist"]:
            data["wishlistpriority"] = current.get("wishlistpriority") or "3"
        await self._post(data)
        new = {k: "1" if on else "0" for k, on in flags.items()}
        if "wishlistpriority" in current:
            new["wishlistpriority"] = current["wishlistpriority"]
        return new

    async def delete(self, collid: int) -> None:
        await self._post({"ajax": "1", "action": "delete", "collid": str(collid)})
