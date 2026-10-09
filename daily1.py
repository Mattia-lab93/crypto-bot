"""Strategia "+1% al giorno" (attiva dal 9/10/2026, scelta dell'utente).

Regole (le migliori tra 96 combinazioni testate su un anno di candele da 15
minuti; nel backtest -2% annuo con costi reali, quindi attesa in perdita):
  - a mezzanotte (ora italiana) fissa il saldo di partenza della giornata;
  - all'1:00 compra BTC con tutto il conto se nella prima ora BTC e' salito
    almeno dello 0,2% e la chiusura di ieri e' sopra la media di 20 giorni;
  - mette subito una vendita limite al prezzo che porta il conto a +1%;
  - se il conto scende a -3% rispetto alla mattina vende tutto;
  - alle 23:55 chiude comunque; una sola operazione al giorno.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

import bot

SYMBOL = "BTC/USD"
TARGET = 0.01
DAY_STOP = 0.03
ENTRY_MIN_MOVE = 0.002
TREND_DAYS = 20
MAKER_FEE = 0.0015
ENTRY_WINDOW = ((1, 0), (1, 45))  # ora italiana
CLOSE_AT = (23, 55)

log = logging.getLogger("bot")


def _bars_15m(start: datetime) -> list[dict]:
    rows, params = [], {"symbols": SYMBOL, "timeframe": "15Min", "limit": 10000,
                        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    while True:
        r = requests.get(f"{bot.DATA_URL}/v1beta3/crypto/us/bars", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows += (data.get("bars") or {}).get(SYMBOL, [])
        if not data.get("next_page_token"):
            break
        params["page_token"] = data["next_page_token"]
    now = datetime.now(timezone.utc)
    return [b for b in rows
            if datetime.fromisoformat(b["t"].replace("Z", "+00:00")) + timedelta(minutes=15) <= now]


def _rome_day(b: dict):
    return datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(bot.TZ).date()


def entry_signal(now: datetime) -> tuple[bool, str]:
    rows = _bars_15m(now - timedelta(days=TREND_DAYS + 3))
    today = now.date()
    closes: dict = {}
    for b in rows:
        closes[_rome_day(b)] = b["c"]  # ultima chiusura di ogni giorno
    past = sorted(d for d in closes if d < today)[-TREND_DAYS:]
    today_rows = [b for b in rows if _rome_day(b) == today]
    if len(past) < TREND_DAYS or not today_rows:
        return False, "dati insufficienti"
    trend = closes[past[-1]] > sum(closes[d] for d in past) / TREND_DAYS
    move = today_rows[-1]["c"] / today_rows[0]["o"] - 1
    why = f"prima ora {move:+.2%}, trend {'su' if trend else 'giu'}"
    return trend and move > ENTRY_MIN_MOVE, why


def _position() -> dict | None:
    return next((p for p in bot.api("GET", "/v2/positions")
                 if p["symbol"] == bot.to_position_symbol(SYMBOL)), None)


def _cancel(order_id: str | None) -> None:
    if not order_id:
        return
    try:
        bot.api("DELETE", f"/v2/orders/{order_id}")
    except RuntimeError as e:
        log.info("Annullamento ordine %s: %s", order_id, e)


def _sell_all(d: dict, reason: str) -> None:
    _cancel(d.pop("tp_id", None))
    for _ in range(5):  # se il book non basta, ripete
        pos = _position()
        if not pos or float(pos["market_value"]) < bot.MIN_ORDER_USD:
            break
        order = bot.limit_ioc(SYMBOL, "sell", qty=float(pos["qty"]))
        if order:
            px = float(order["filled_avg_price"])
            bot.log_trade(SYMBOL, "vendita", float(order["filled_qty"]) * px,
                          {"close": px, "stop": 0}, reason)
    log.info("Chiusura BTC: %s", reason)


def _place_target(d: dict) -> None:
    pos = _position()
    if not pos:
        return
    cash = float(bot.api("GET", "/v2/account")["cash"])
    info = bot.ASSET_INFO.get(SYMBOL, {})
    qty = bot._round_down(float(pos["qty"]), float(info.get("min_trade_increment") or 0))
    px = (d["e0"] * (1 + TARGET) - cash) / (qty * (1 - MAKER_FEE))
    step = float(info.get("price_increment") or 0)
    if step:
        px = bot._round_down(px, step) + step
    order = bot.api("POST", "/v2/orders", json={
        "symbol": SYMBOL, "qty": f"{qty:.10g}", "side": "sell", "type": "limit",
        "limit_price": f"{px:.10g}", "time_in_force": "gtc",
    })
    d["tp_id"] = order["id"]
    d["tp_price"] = px
    log.info("Vendita obiettivo +1%% piazzata a %.2f", px)


def tick() -> None:
    bot.tradable_symbols()  # carica incrementi di prezzo/quantita'
    state = bot.load_state()
    d = state.setdefault("daily1", {})
    now = datetime.now(bot.TZ)
    today = now.date().isoformat()

    if d.get("date") != today:
        if _position():
            _sell_all(d, "residuo del giorno prima")
        equity = float(bot.api("GET", "/v2/account")["equity"])
        d.clear()
        d.update(date=today, e0=equity, done=False, tried=False, result="in attesa del segnale")
        log.info("Nuovo giorno %s: saldo di partenza %.2f, obiettivo %.2f", today, equity, equity * (1 + TARGET))

    equity = float(bot.api("GET", "/v2/account")["equity"])
    e0 = d["e0"]
    pos = _position()
    hm = (now.hour, now.minute)

    if pos:
        if equity <= e0 * (1 - DAY_STOP):
            _sell_all(d, "stop giornaliero -3%")
            d.update(done=True, result="stop -3%")
        elif hm >= CLOSE_AT:
            _sell_all(d, "fine giornata")
            d.update(done=True, result="chiuso a fine giornata")
        elif not d.get("tp_id"):
            _place_target(d)
    elif d.get("tp_id"):
        order = bot.api("GET", f"/v2/orders/{d['tp_id']}")
        if order["status"] == "filled":
            px = float(order["filled_avg_price"])
            bot.log_trade(SYMBOL, "vendita", float(order["filled_qty"]) * px,
                          {"close": px, "stop": 0}, "obiettivo +1%")
            d.update(done=True, result="obiettivo +1% raggiunto ✅")
        d.pop("tp_id", None)

    if not pos and not d["done"] and not d["tried"] and ENTRY_WINDOW[0] <= hm < ENTRY_WINDOW[1]:
        d["tried"] = True
        ok, why = entry_signal(now)
        log.info("Segnale delle 1:00: %s -> %s", why, "COMPRA" if ok else "niente oggi")
        if ok and not bot.DRY_RUN:
            cash = float(bot.api("GET", "/v2/account")["cash"])
            order = bot.limit_ioc(SYMBOL, "buy", usd=cash * 0.98)
            if order:
                px = float(order["filled_avg_price"])
                bot.log_trade(SYMBOL, "acquisto", float(order["filled_qty"]) * px,
                              {"close": px, "stop": e0 * (1 - DAY_STOP)}, "+1% al giorno")
                d["result"] = "in posizione su BTC"
                _place_target(d)
        else:
            d.update(done=True, result=f"nessun acquisto ({why})")

    bot.save_state(state)
    bot.commit_state("Operazioni del bot")
    day_pct = equity / e0 - 1
    extra = ["", f"<b>Obiettivo +1% al giorno</b>",
             f"Oggi: {bot._pct(day_pct * 100)} (partenza {bot._money(e0)}) — {d['result']}"]
    bot.maybe_send_report({}, extra=extra)
    if now.minute < 5:
        bot.push_status_to_jarvis({}, extra=extra)
