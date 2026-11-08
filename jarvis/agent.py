"""Agent-Schleife: streamt Claudes Antwort, führt Tools aus und meldet alles als UI-Events."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator

import anthropic

from tools import CUSTOM_TOOLS, run_custom_tool

MODEL = os.getenv("JARVIS_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("JARVIS_EFFORT", "medium")  # low | medium | high | xhigh | max
MAX_STEPS = 12

SERVER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 6,
        "user_location": {"type": "approximate", "country": "DE", "timezone": "Europe/Berlin"},
    },
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4},
]
TOOLS = SERVER_TOOLS + CUSTOM_TOOLS

SYSTEM_PROMPT = """Du bist J.A.R.V.I.S., ein persönlicher KI-Assistent im Stil des Assistenten aus den Iron-Man-Filmen: \
präzise, ruhig, höflich, mit trockenem britischem Humor. Du sprichst Deutsch (außer der Nutzer schreibt in einer anderen Sprache) \
und sprichst den Nutzer mit "Sir" an, ohne es zu übertreiben.

Deine Fähigkeiten:
- Websuche und Webseiten lesen (web_search, web_fetch) für aktuelle Informationen, Nachrichten und Recherchen.
- Marktanalysen mit echten Kursdaten (market_data, symbol_search, crypto_overview).

Arbeitsweise:
- Für alles, was aktuell sein muss (Kurse, Nachrichten, Ereignisse), nutze die Tools statt deines Gedächtnisses.
- Eine Marktanalyse enthält: aktuelle Lage (Kurs, Veränderung), Trend und technische Kennzahlen (SMA, RSI, Volatilität, \
Drawdown), relevante aktuelle Nachrichten/Treiber aus der Websuche, Chancen und Risiken sowie ein kurzes Fazit.
- Unabhängige Abfragen startest du parallel.
- Antworte kompakt und gut gegliedert in Markdown (kurze Überschriften, Listen, Tabellen für Vergleiche). Nenne Quellen.
- Für einfache Gesprächsfragen antworte direkt und knapp, ohne Tools.
- Bei Finanzthemen: Du gibst Informationen und Einordnung, keine persönliche Anlageberatung. Erwähne das einmal kurz am Ende \
einer Analyse, nicht in jeder Antwort.

Heutiges Datum: {today}."""


@dataclass
class Session:
    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


client = anthropic.AsyncAnthropic()


def _summarize_input(name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("query", "url", "symbol"):
        if isinstance(data.get(key), str):
            extra = f" · {data['range']}" if key == "symbol" and data.get("range") else ""
            return data[key] + extra
    if isinstance(data.get("ids"), list):
        return ", ".join(map(str, data["ids"]))
    return ""


def _sources_from_block(block: Any) -> list[dict[str, str]]:
    """Extrahiert Titel/URL aus web_search- und web_fetch-Ergebnissen."""
    content = getattr(block, "content", None)
    items: list[dict[str, str]] = []
    if isinstance(content, list):  # Erfolg: Liste von web_search_result
        for r in content:
            url = getattr(r, "url", None)
            if url:
                items.append({"title": getattr(r, "title", None) or url, "url": url})
    elif content is not None and getattr(content, "url", None):  # web_fetch_result
        doc = getattr(content, "content", None)
        title = getattr(doc, "title", None) if doc is not None else None
        items.append({"title": title or content.url, "url": content.url})
    return items


def _is_error_result(block: Any) -> bool:
    content = getattr(block, "content", None)
    return content is not None and not isinstance(content, list) and getattr(content, "error_code", None) is not None


async def run_turn(session: Session, user_text: str) -> AsyncIterator[dict[str, Any]]:
    """Verarbeitet eine Nutzernachricht und liefert Events für die Oberfläche."""
    history_len = len(session.messages)
    session.messages.append({"role": "user", "content": user_text})
    today = date.today()
    weekday = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][today.weekday()]
    system = SYSTEM_PROMPT.format(today=f"{weekday}, {today:%d.%m.%Y}")
    wrote_text = False
    json_retries = 0

    try:
        step = 0
        while step < MAX_STEPS:
            step += 1
            yield {"type": "status", "state": "thinking"}
            try:
                async with client.beta.messages.stream(
                    model=MODEL,
                    max_tokens=64000,
                    system=system,
                    tools=TOOLS,
                    messages=session.messages,
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": EFFORT},
                    cache_control={"type": "ephemeral"},
                    # Bei einer Ablehnung durch Sicherheitsfilter übernimmt automatisch ein Ersatzmodell
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    async for event in stream:
                        if event.type == "thinking":
                            yield {"type": "thinking", "text": event.thinking}
                        elif event.type == "text":
                            if event.text:
                                yield {"type": "text", "text": event.text}
                                wrote_text = True
                        elif event.type == "content_block_start":
                            cb = event.content_block
                            if cb.type == "text":
                                yield {"type": "status", "state": "speaking"}
                                if wrote_text:
                                    yield {"type": "text", "text": "\n\n"}
                        elif event.type == "content_block_stop":
                            cb = event.content_block
                            if cb.type == "server_tool_use":
                                yield {
                                    "type": "tool",
                                    "id": cb.id,
                                    "name": cb.name,
                                    "detail": _summarize_input(cb.name, cb.input),
                                }
                                yield {"type": "status", "state": "searching"}
                            elif cb.type in ("web_search_tool_result", "web_fetch_tool_result"):
                                failed = _is_error_result(cb)
                                yield {"type": "tool_done", "id": cb.tool_use_id, "ok": not failed}
                                sources = _sources_from_block(cb)
                                if sources:
                                    yield {"type": "sources", "items": sources}
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool-Eingabe war kein parsebares JSON (eager_input_streaming) -> Schritt wiederholen
                json_retries += 1
                if json_retries > 2:
                    raise
                step -= 1
                continue

            session.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                yield {"type": "text", "text": "\n\n*Diese Anfrage kann ich leider nicht bearbeiten, Sir.*"}
                break
            if response.stop_reason == "pause_turn":
                continue  # Server-Tools laufen weiter; gleiche Historie erneut senden

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break
            if response.stop_reason == "max_tokens":
                yield {"type": "error", "message": "Antwort wurde abgeschnitten (max_tokens)."}
                break

            # Eigene Tools parallel ausführen
            yield {"type": "status", "state": "analyzing"}
            for tu in tool_uses:
                yield {"type": "tool", "id": tu.id, "name": tu.name, "detail": _summarize_input(tu.name, tu.input)}
            results = await asyncio.gather(*(run_custom_tool(tu.name, tu.input) for tu in tool_uses))

            tool_results = []
            for tu, (content, is_error, chart) in zip(tool_uses, results):
                yield {"type": "tool_done", "id": tu.id, "ok": not is_error, "error": content if is_error else None}
                if chart:
                    yield {"type": "market", "data": chart}
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error}
                )
            # Alle Ergebnisse in EINER Nutzernachricht zurückgeben
            session.messages.append({"role": "user", "content": tool_results})
        else:
            yield {"type": "error", "message": "Maximale Anzahl an Arbeitsschritten erreicht."}

    except anthropic.AuthenticationError:
        del session.messages[history_len:]
        yield {"type": "error", "message": "Ungültiger oder fehlender API-Key (ANTHROPIC_API_KEY)."}
    except anthropic.RateLimitError:
        del session.messages[history_len:]
        yield {"type": "error", "message": "Rate-Limit erreicht – bitte kurz warten."}
    except anthropic.APIStatusError as e:
        del session.messages[history_len:]
        yield {"type": "error", "message": f"API-Fehler {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError:
        del session.messages[history_len:]
        yield {"type": "error", "message": "Keine Verbindung zur Claude API."}
    except ValueError as e:
        del session.messages[history_len:]
        yield {"type": "error", "message": f"Ungültige Tool-Eingabe: {e}"}

    yield {"type": "status", "state": "idle"}
    yield {"type": "done"}
