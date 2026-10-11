"""Asynchroner Client für die BoardGameGeek XML API2."""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://boardgamegeek.com/xmlapi2"
USER_AGENT = "HomeAssistant-bgg-collection-sync/0.1.0"
MAX_ATTEMPTS = 8
RETRY_DELAY = 5  # Sekunden (BGG antwortet bei Collections zuerst oft mit 202)
THING_BATCH_SIZE = 20  # BGG erlaubt max. 20 IDs pro /thing-Abfrage


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


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    async def fetch_collection(self, only_owned: bool) -> list[CollectionItem]:
        """Lädt die Collection inkl. der in der Collection gewählten Version."""
        params: dict = {
            "username": self.username,
            "subtype": "boardgame",
            "version": 1,
            "stats": 1,
        }
        if only_owned:
            params["own"] = 1
        root = await self._get("collection", params)
        return [self._parse_item(el) for el in root.findall("item")]

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
        )

    async def fetch_versions(self, game_ids: list[int]) -> list[VersionInfo]:
        """Lädt alle Versionen (inkl. Sprachen) für bis zu 20 Spiele."""
        root = await self._get(
            "thing", {"id": ",".join(map(str, game_ids)), "versions": 1}
        )
        result: list[VersionInfo] = []
        for game in root.findall("item"):
            game_id = _to_int(game.get("id"))
            for ver in game.findall("versions/item"):
                version_id = _to_int(ver.get("id"))
                if game_id is None or version_id is None:
                    continue
                name_el = ver.find("name")
                year_el = ver.find("yearpublished")
                result.append(
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
        return result
