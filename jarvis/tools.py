"""Eigene Tools für J.A.R.V.I.S.: Kursdaten, Kennzahlen, Ticker-Suche, Krypto.

Datenquellen ohne API-Key:
- Yahoo Finance (inoffizielle Chart- und Such-Endpunkte)
- CoinGecko (öffentliche API)
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (JARVIS market agent)"}
TIMEOUT = httpx.Timeout(15.0)

RANGES = ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"]
# Intervall je Zeitraum, damit die Datenpunkte überschaubar bleiben
INTERVALS = {"5d": "1h", "1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d", "2y": "1wk", "5y": "1wk"}


class ToolError(Exception):
    """Fehler, der als is_error-Ergebnis an Claude zurückgeht."""


# ---------------------------------------------------------------------------
# Tool-Definitionen für die Claude API
# ---------------------------------------------------------------------------

CUSTOM_TOOLS: list[dict[str, Any]] = [
    {
        "name": "market_data",
        "description": (
            "Holt Kursverlauf und berechnete Kennzahlen für ein Wertpapier, einen Index, "
            "ein Währungspaar oder eine Kryptowährung über Yahoo-Finance-Ticker "
            "(z. B. AAPL, NVDA, SAP.DE, ^GDAXI für den DAX, ^GSPC für den S&P 500, "
            "EURUSD=X, BTC-USD, GC=F für Gold). Liefert aktuellen Kurs, Veränderung, "
            "Hoch/Tief, gleitende Durchschnitte (SMA20/50/200), RSI(14), annualisierte "
            "Volatilität und maximalen Drawdown. Nutze das für jede Marktanalyse; "
            "für mehrere Werte rufe das Tool mehrfach parallel auf. Kennst du den "
            "Ticker nicht sicher, nutze zuerst symbol_search."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Yahoo-Finance-Ticker, z. B. 'MSFT' oder '^GDAXI'"},
                "range": {
                    "type": "string",
                    "enum": RANGES,
                    "description": "Betrachtungszeitraum, Standard '6mo'",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "symbol_search",
        "description": (
            "Sucht Ticker-Symbole zu einem Firmen-, Index- oder Fondsnamen "
            "(z. B. 'Siemens', 'Rheinmetall', 'MSCI World'). Gibt passende Symbole mit "
            "Börse und Typ zurück."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Suchbegriff"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "crypto_overview",
        "description": (
            "Aktuelle Preise (EUR und USD), 24h-Veränderung, Marktkapitalisierung und "
            "24h-Volumen für Kryptowährungen über CoinGecko. IDs sind CoinGecko-IDs wie "
            "'bitcoin', 'ethereum', 'solana'."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CoinGecko-IDs, max. 10",
                }
            },
            "required": ["ids"],
            "additionalProperties": False,
        },
    },
]


def validate_input(name: str, args: Any) -> dict[str, Any]:
    """Prüft die Tool-Eingabe selbst (bei eager_input_streaming validiert die API nicht)."""
    if not isinstance(args, dict):
        raise ToolError("Eingabe muss ein Objekt sein.")
    if name == "market_data":
        symbol = args.get("symbol")
        rng = args.get("range", "6mo")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 30:
            raise ToolError("'symbol' fehlt oder ist ungültig.")
        if rng not in RANGES:
            raise ToolError(f"'range' muss einer von {RANGES} sein.")
        return {"symbol": symbol.strip().upper(), "range": rng}
    if name == "symbol_search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 100:
            raise ToolError("'query' fehlt oder ist ungültig.")
        return {"query": query.strip()}
    if name == "crypto_overview":
        ids = args.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
            raise ToolError("'ids' muss eine nicht-leere Liste von Strings sein.")
        return {"ids": [i.strip().lower() for i in ids[:10]]}
    raise ToolError(f"Unbekanntes Tool: {name}")


# ---------------------------------------------------------------------------
# Kennzahlen
# ---------------------------------------------------------------------------

def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):  # Wilder-Glättung
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _volatility(values: list[float], periods_per_year: float) -> float | None:
    if len(values) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(values[:-1], values[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year) * 100


def _max_drawdown(values: list[float]) -> float:
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return mdd * 100


def _r(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)


def compute_metrics(closes: list[float], interval: str) -> dict[str, Any]:
    per_year = {"1h": 252 * 7, "1d": 252, "1wk": 52}.get(interval, 252)
    last, first = closes[-1], closes[0]
    sma20, sma50, sma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
    trend = None
    if sma20 and sma50:
        trend = "aufwärts" if last > sma20 > sma50 else "abwärts" if last < sma20 < sma50 else "seitwärts/gemischt"
    return {
        "change_range_pct": _r((last / first - 1) * 100),
        "high_range": _r(max(closes)),
        "low_range": _r(min(closes)),
        "sma20": _r(sma20),
        "sma50": _r(sma50),
        "sma200": _r(sma200),
        "rsi14": _r(_rsi(closes), 1),
        "volatility_annual_pct": _r(_volatility(closes, per_year), 1),
        "max_drawdown_pct": _r(_max_drawdown(closes)),
        "trend_signal": trend,
    }


def _downsample(points: list[tuple[int, float]], max_points: int = 120) -> list[tuple[int, float]]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    out = [points[int(i * step)] for i in range(max_points)]
    out[-1] = points[-1]
    return out


# ---------------------------------------------------------------------------
# Tool-Implementierungen
# ---------------------------------------------------------------------------

async def market_data(client: httpx.AsyncClient, symbol: str, range: str = "6mo") -> tuple[dict, dict]:
    """Gibt (Ergebnis für Claude, Chart-Daten für die UI) zurück."""
    interval = INTERVALS[range]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
    r = await client.get(url, params={"range": range, "interval": interval, "includePrePost": "false"})
    if r.status_code == 404:
        raise ToolError(f"Symbol '{symbol}' nicht gefunden. Versuche symbol_search.")
    r.raise_for_status()
    chart = r.json().get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise ToolError(f"Keine Daten für '{symbol}': {chart.get('error')}")
    res = chart["result"][0]
    meta = res.get("meta", {})
    stamps = res.get("timestamp") or []
    closes_raw = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    points = [(t, c) for t, c in zip(stamps, closes_raw) if c is not None]
    if len(points) < 2:
        raise ToolError(f"Zu wenige Kursdaten für '{symbol}'.")
    closes = [c for _, c in points]

    price = meta.get("regularMarketPrice") or closes[-1]
    prev = meta.get("chartPreviousClose") if range == "5d" else meta.get("previousClose")
    prev = prev or (closes[-2] if len(closes) > 1 else None)
    result = {
        "symbol": meta.get("symbol", symbol),
        "name": max([meta.get("longName") or "", meta.get("shortName") or ""], key=len) or symbol,
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "price": _r(price, 4 if price and price < 10 else 2),
        "day_change_pct": _r((price / prev - 1) * 100) if prev else None,
        "52w_high": meta.get("fiftyTwoWeekHigh"),
        "52w_low": meta.get("fiftyTwoWeekLow"),
        "range": range,
        "interval": interval,
        "data_points": len(closes),
        "as_of": datetime.fromtimestamp(points[-1][0], tz=timezone.utc).isoformat(timespec="minutes"),
        **compute_metrics(closes, interval),
        # Kompakter Verlauf, damit Claude das Muster sehen kann
        "recent_closes": [_r(c) for c in closes[-15:]],
    }
    chart_payload = {
        "symbol": result["symbol"],
        "name": result["name"],
        "currency": result["currency"],
        "price": result["price"],
        "change_pct": result["change_range_pct"],
        "day_change_pct": result["day_change_pct"],
        "range": range,
        "rsi14": result["rsi14"],
        "volatility": result["volatility_annual_pct"],
        "series": [[t * 1000, round(c, 4)] for t, c in _downsample(points)],
    }
    return result, chart_payload


async def symbol_search(client: httpx.AsyncClient, query: str) -> dict:
    r = await client.get(
        "https://query2.finance.yahoo.com/v1/finance/search",
        params={"q": query, "quotesCount": 8, "newsCount": 0, "lang": "de-DE"},
    )
    r.raise_for_status()
    quotes = r.json().get("quotes", [])
    return {
        "query": query,
        "results": [
            {
                "symbol": q.get("symbol"),
                "name": q.get("longname") or q.get("shortname"),
                "exchange": q.get("exchDisp") or q.get("exchange"),
                "type": q.get("typeDisp") or q.get("quoteType"),
            }
            for q in quotes
            if q.get("symbol")
        ],
    }


async def crypto_overview(client: httpx.AsyncClient, ids: list[str]) -> dict:
    r = await client.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": ",".join(ids),
            "vs_currencies": "eur,usd",
            "include_24hr_change": "true",
            "include_market_cap": "true",
            "include_24hr_vol": "true",
        },
    )
    r.raise_for_status()
    data = r.json()
    missing = [i for i in ids if i not in data]
    return {"coins": data, "not_found": missing}


async def run_custom_tool(name: str, raw_input: Any) -> tuple[str, bool, dict | None]:
    """Führt ein Tool aus. Rückgabe: (Inhalt, is_error, optionale UI-Chartdaten)."""
    try:
        args = validate_input(name, raw_input)
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            if name == "market_data":
                result, chart = await market_data(client, **args)
                return json.dumps(result, ensure_ascii=False), False, chart
            if name == "symbol_search":
                return json.dumps(await symbol_search(client, **args), ensure_ascii=False), False, None
            if name == "crypto_overview":
                return json.dumps(await crypto_overview(client, **args), ensure_ascii=False), False, None
        raise ToolError(f"Unbekanntes Tool: {name}")
    except ToolError as e:
        return str(e), True, None
    except httpx.HTTPStatusError as e:
        return f"Datenquelle antwortete mit HTTP {e.response.status_code}.", True, None
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        return f"Datenquelle nicht erreichbar: {type(e).__name__}", True, None
    except (ValueError, KeyError, TypeError) as e:
        return f"Unerwartetes Datenformat: {e}", True, None
