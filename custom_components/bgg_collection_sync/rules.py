"""Spielregeln: Text aus eigenen Regel-PDFs lesen und dem LLM (HA Assist) als Werkzeug anbieten."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

try:  # HA ab 2026.12 validiert Werkzeug-Parameter mit probatio
    import probatio as vol
except ImportError:
    import voluptuous as vol

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RULES_DIR = "spielregeln"
CACHE_DIR = ".text"
API_ID = f"{DOMAIN}_rules"

API_PROMPT = (
    "Du beantwortest Fragen zu Brettspielregeln. Hole dir die Anleitung immer mit "
    "get_game_rules und antworte nur auf Grundlage dieses Textes. Steht die Antwort nicht "
    "in der Anleitung, sag das offen, statt zu raten. Nennt die Frage kein Spiel, ist das "
    "laufende Spiel gemeint. Antworte kurz und auf Deutsch, bei Bedarf mit Abschnitt oder Seite."
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", text.lower())


def _extract(pdf: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(pdf)
    pages = [
        f"[Seite {no}]\n{text.strip()}"
        for no, page in enumerate(reader.pages, 1)
        if (text := page.extract_text() or "").strip()
    ]
    return "\n\n".join(pages)


class RuleBooks:
    """Regel-PDFs unter /config/spielregeln, Text zwischengespeichert in .text/."""

    def __init__(self, path: str) -> None:
        self.dir = Path(path)

    def list(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(p for p in self.dir.iterdir() if p.suffix.lower() == ".pdf")

    def find(self, bgg_id: int | None, names: list[str]) -> list[Path]:
        """Alle PDFs eines Spiels: Dateiname = BGG-ID oder Spielname, optional mit Zusatz
        (z. B. 'Catan.pdf', 'Catan - Almanach.pdf', '13.pdf')."""
        wanted = [_norm(n) for n in names if n]
        hits = []
        for pdf in self.list():
            stem = pdf.stem
            head = _norm(re.split(r"\s+[-–_]\s+", stem, maxsplit=1)[0])
            if bgg_id is not None and re.fullmatch(rf"{bgg_id}(\D.*)?", stem):
                hits.append(pdf)
            elif head and head in wanted:
                hits.append(pdf)
        return hits

    def text(self, pdf: Path) -> str:
        cache = self.dir / CACHE_DIR / f"{pdf.stem}.txt"
        if cache.exists() and cache.stat().st_mtime >= pdf.stat().st_mtime:
            return cache.read_text(encoding="utf-8")
        text = _extract(pdf)
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text


def _coordinators(hass: HomeAssistant) -> list[Any]:
    return [
        e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]


def _resolve_game(hass: HomeAssistant, name: str | None) -> dict[str, Any] | None:
    """Spiel aus der Sammlung oder das laufende Spiel; None, wenn unbekannt."""
    for c in _coordinators(hass):
        if not name:
            if active := c.data.get("active_play"):
                return {"bgg_id": active["bgg_id"], "name": active["game_name"]}
            continue
        try:
            return c.manager.find_game(name=name)
        except HomeAssistantError:
            continue
    return None


class GetGameRulesTool(llm.Tool):
    name = "get_game_rules"
    description = (
        "Liefert den Text der Spielanleitung (aus den Regel-PDFs) für ein Brettspiel. "
        "Ohne Spielnamen wird das gerade laufende Spiel genommen."
    )
    parameters = vol.Schema(
        {vol.Optional("game", description="Name des Spiels, z. B. 'Catan'"): str}
    )
    integration = DOMAIN

    def __init__(self, books: RuleBooks) -> None:
        self.books = books

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        name = (tool_input.tool_args.get("game") or "").strip()
        game = _resolve_game(hass, name)
        if game is None and not name:
            return _result({"error": "Es läuft gerade kein Spiel. Frag nach, welches gemeint ist."})
        names = [name, game["name"] if game else None]
        files = await hass.async_add_executor_job(
            self.books.find, game["bgg_id"] if game else None, names
        )
        if not files:
            return _result(
                {
                    "game": game["name"] if game else name,
                    "error": "Für dieses Spiel liegt keine Anleitung vor.",
                }
            )
        parts = []
        for pdf in files:
            text = await hass.async_add_executor_job(self.books.text, pdf)
            parts.append({"file": pdf.name, "text": text or "(kein Text lesbar, evtl. Scan)"})
        return _result({"game": game["name"] if game else name, "rulebooks": parts})


class ListRulebooksTool(llm.Tool):
    name = "list_rulebooks"
    description = "Listet die Spiele, für die eine Anleitung als PDF vorliegt."
    integration = DOMAIN

    def __init__(self, books: RuleBooks) -> None:
        self.books = books

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        files = await hass.async_add_executor_job(self.books.list)
        return _result({"rulebooks": [p.stem for p in files]})


def _result(data: dict[str, Any]) -> Any:
    # ToolResult gibt es erst in neueren HA-Versionen; ältere erwarten ein dict
    tool_result = getattr(llm, "ToolResult", None)
    return tool_result(data=data) if tool_result else data


class RulesAPI(llm.API):
    """LLM-API 'Spielregeln (BGG)', im Assist-Agenten unter 'Steuern von Home Assistant'."""

    books: RuleBooks

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        prompt = API_PROMPT
        if game := _resolve_game(self.hass, None):
            prompt += f" Laufendes Spiel: {game['name']}."
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=[GetGameRulesTool(self.books), ListRulebooksTool(self.books)],
        )


@callback
def async_register_rules_api(hass: HomeAssistant) -> None:
    api = RulesAPI(hass=hass, id=API_ID, name="Spielregeln (BGG)")
    api.books = RuleBooks(hass.config.path(RULES_DIR))
    llm.async_register_api(hass, api)
