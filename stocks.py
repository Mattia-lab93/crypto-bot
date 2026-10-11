"""Robot v2 - modulo azioni (dal 12/10/2026).

Regola: se il Nasdaq (QQQ) e' sopra la media mobile dei 200 giorni, il conto tiene
TQQQ (Nasdaq a leva 3x) per 1,25 volte il saldo; altrimenti niente azioni e il
capitale passa al trend following crypto. Decisione una volta al giorno, pochi
minuti prima della chiusura di Wall Street.

Backtest dal 2010 (dati giornalieri): TQQQ x1 +33,6%/anno (calo max -56%), x1,5
+38,2% (calo max -75%), oltre la crescita scende (Kelly). La leva e' 1,25 e non
1,5 perche' Alpaca chiede il 75% di margine di mantenimento sugli ETF 3x.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import bot

SIGNAL = "QQQ"
ASSET = "TQQQ"
SMA_DAYS = 200
LEVERAGE = 1.25
REBALANCE_BAND = 0.05      # ribilancia solo se la leva si scosta di oltre il 5% del conto
DECIDE_MINUTES = 12        # decide negli ultimi 12 minuti prima della chiusura

log = logging.getLogger("bot")


def _data(path: str, **params) -> dict:
    r = requests.get(f"{bot.DATA_URL}{path}", params=params, headers=bot._headers(), timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Dati azioni {path} -> {r.status_code}: {r.text}")
    return r.json()


def signal() -> tuple[bool, float, float]:
    """(sopra la media?, prezzo QQQ attuale, media 200 giorni con il prezzo di oggi)."""
    start = (datetime.now(timezone.utc) - timedelta(days=330)).strftime("%Y-%m-%d")
    bars = _data("/v2/stocks/bars", symbols=SIGNAL, timeframe="1Day", start=start,
                 limit=1000, feed="iex", adjustment="all")["bars"].get(SIGNAL, [])
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    closes = [b["c"] for b in bars if not b["t"].startswith(today)]  # la barra di oggi e' parziale
    price = _data("/v2/stocks/trades/latest", symbols=SIGNAL, feed="iex")["trades"][SIGNAL]["p"]
    if len(closes) < SMA_DAYS - 1:
        raise RuntimeError(f"Storico {SIGNAL} insufficiente: {len(closes)} giorni")
    sma = (sum(closes[-(SMA_DAYS - 1):]) + price) / SMA_DAYS
    return price > sma, price, sma


def _asset_value() -> float:
    pos = next((p for p in bot.api("GET", "/v2/positions") if p["symbol"] == ASSET), None)
    return float(pos["market_value"]) if pos else 0.0


def _sell_crypto() -> None:
    """Libera il capitale: le crypto non si comprano a margine su Alpaca."""
    for p in bot.api("GET", "/v2/positions"):
        if p.get("asset_class") == "crypto" and float(p["market_value"]) >= bot.MIN_ORDER_USD:
            sym = p["symbol"][:-3] + "/USD"
            order = bot.limit_ioc(sym, "sell", qty=float(p["qty"]))
            if order:
                px = float(order["filled_avg_price"])
                bot.log_trade(sym, "vendita", float(order["filled_qty"]) * px,
                              {"close": px, "stop": 0}, "passaggio a TQQQ")


def tick() -> list[str]:
    """Da chiamare ogni pochi minuti: agisce solo vicino alla chiusura, una volta al giorno.
    Ritorna le righe per il report."""
    state = bot.load_state()
    st = state.setdefault("stocks", {})
    lines = ["", "<b>Azioni (Robot v2)</b>"]
    clock = bot.api("GET", "/v2/clock")
    if clock["is_open"]:
        close = datetime.fromisoformat(clock["next_close"].replace("Z", "+00:00"))
        minutes = (close - datetime.now(timezone.utc)).total_seconds() / 60
        today = close.date().isoformat()
        if minutes <= DECIDE_MINUTES and st.get("date") != today:
            on, price, sma = signal()
            equity = float(bot.api("GET", "/v2/account")["equity"])
            log.info("AZIONI: QQQ %.2f vs media 200g %.2f -> %s", price, sma,
                     f"TQQQ x{LEVERAGE}" if on else "fuori dalle azioni")
            if not bot.DRY_RUN:
                if on:
                    _sell_crypto()
                    account = bot.api("GET", "/v2/account")
                    equity = float(account["equity"])
                    diff = LEVERAGE * equity - _asset_value()
                    if diff > REBALANCE_BAND * equity:
                        usd = min(diff, float(account["buying_power"]) * 0.95)
                        bot.api("POST", "/v2/orders", json={
                            "symbol": ASSET, "notional": f"{usd:.2f}", "side": "buy",
                            "type": "market", "time_in_force": "day"})
                        bot.log_trade(ASSET, "acquisto", usd, {"close": price, "stop": sma}, "QQQ sopra media 200g")
                    elif diff < -REBALANCE_BAND * equity:
                        bot.api("POST", "/v2/orders", json={
                            "symbol": ASSET, "notional": f"{-diff:.2f}", "side": "sell",
                            "type": "market", "time_in_force": "day"})
                        bot.log_trade(ASSET, "vendita", -diff, {"close": price, "stop": sma}, "ribilanciamento leva")
                elif _asset_value() > 0:
                    value = _asset_value()
                    bot.api("DELETE", f"/v2/positions/{ASSET}")
                    bot.log_trade(ASSET, "vendita", value, {"close": price, "stop": sma}, "QQQ sotto media 200g")
            st.update(date=today, on=on, qqq=round(price, 2), sma=round(sma, 2))
            bot.save_state(state)
            bot.commit_state("Operazioni del bot")
    if "on" in st:
        lines.append(f"QQQ {st['qqq']} vs media 200 giorni {st['sma']}: "
                     + (f"in TQQQ x{LEVERAGE} ✅" if st["on"] else "fuori dalle azioni, capitale sulle crypto"))
        value = _asset_value()
        if value:
            lines.append(f"TQQQ: {bot._money(value)}")
    else:
        lines.append("In attesa della prima decisione (alla chiusura di Wall Street)")
    return lines
