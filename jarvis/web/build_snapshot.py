"""Erzeugt snapshot.json: Kursdaten + Kennzahlen für die claude.ai-Webversion von JARVIS.

Die Webversion darf selbst keine Kurse aus dem Internet laden. Deshalb werden die Daten
hiermit abgeholt und als Datei neben die Seite gelegt.  Aufruf:  python web/build_snapshot.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

import tools  # noqa: E402

SYMBOLS = {
    "Indizes": ["^GDAXI", "^STOXX50E", "^GSPC", "^NDX", "^DJI", "^N225"],
    "Devisen & Rohstoffe": ["EURUSD=X", "GC=F", "CL=F"],
    "Krypto": ["BTC-USD", "ETH-USD", "SOL-USD"],
    "Deutschland & Europa": ["SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "RHM.DE", "MBG.DE", "BMW.DE", "VOW3.DE",
                             "BAS.DE", "IFX.DE", "ASML.AS"],
    "USA": ["AAPL", "MSFT", "NVDA", "AMD", "AMZN", "GOOGL", "META", "TSLA", "NFLX"],
}
NAMES = {"^GDAXI": "DAX", "^STOXX50E": "Euro Stoxx 50", "^GSPC": "S&P 500", "^NDX": "Nasdaq 100",
         "^DJI": "Dow Jones", "^N225": "Nikkei 225", "EURUSD=X": "Euro / US-Dollar", "GC=F": "Gold",
         "CL=F": "Rohöl WTI", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana"}


def pct(a: float, b: float) -> float | None:
    return round((b / a - 1) * 100, 2) if a else None


async def one(client: httpx.AsyncClient, sym: str, group: str) -> dict | None:
    try:
        res, chart = await tools.market_data(client, sym, "1y")
    except Exception as e:  # noqa: BLE001
        print("skip", sym, e, file=sys.stderr)
        return None
    closes = [p[1] for p in chart["series"]]
    n = len(closes)
    # ca. 21 Handelstage = 1 Monat, auf die ausgedünnte Reihe umgerechnet
    step = max(1, round(res["data_points"] / n))
    def back(days: int) -> float | None:
        i = n - 1 - max(1, round(days / step))
        return pct(closes[i], closes[-1]) if i >= 0 else None
    return {
        "symbol": res["symbol"], "group": group,
        "name": NAMES.get(sym) or res["name"], "currency": res["currency"], "exchange": res["exchange"],
        "price": res["price"], "day_change_pct": res["day_change_pct"],
        "change_1m_pct": back(21), "change_6m_pct": back(126), "change_1y_pct": res["change_range_pct"],
        "high_1y": res["high_range"], "low_1y": res["low_range"],
        "sma20": res["sma20"], "sma50": res["sma50"], "sma200": res["sma200"], "rsi14": res["rsi14"],
        "volatility_annual_pct": res["volatility_annual_pct"], "max_drawdown_1y_pct": res["max_drawdown_pct"],
        "trend_signal": res["trend_signal"], "as_of": res["as_of"],
        "series": [[t // 1000, round(c, 4)] for t, c in chart["series"]],
    }


async def main() -> None:
    async with httpx.AsyncClient(headers=tools.HEADERS, timeout=tools.TIMEOUT) as client:
        jobs = [one(client, s, g) for g, syms in SYMBOLS.items() for s in syms]
        rows = [r for r in await asyncio.gather(*jobs) if r]
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "assets": rows}
    path = Path(__file__).with_name("snapshot.json")
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(rows)} Werte -> {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())
