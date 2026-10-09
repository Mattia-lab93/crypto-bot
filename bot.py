"""Bot di trading crypto su conto demo Alpaca (paper trading).

Pensato per girare su GitHub Actions una volta all'ora: ogni esecuzione scarica
le candele orarie chiuse, decide entrate/uscite, invia gli ordini e termina.
Alle 21:00 (ora italiana) manda un report giornaliero su Telegram.

Strategia "breakout con filtro di trend" (solo long, Alpaca non permette short
sulle crypto):
  - entrata: chiusura sopra il massimo delle 120 ore (5 giorni) precedenti E
    sopra la media mobile a 700 ore (trend di fondo rialzista), solo se anche
    BTC e' sopra la sua media a 700 ore (mercato crypto in salute);
  - uscita: chandelier stop = massimo delle ultime 22 ore - 6 x ATR(22);
  - size: ogni posizione rischia ~1% del conto fino allo stop, max 15% del conto
    per coin, max 6 posizioni contemporanee.

Variabili d'ambiente: ALPACA_KEY_ID, ALPACA_SECRET_KEY, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID. Opzionali: DRY_RUN=1 (non invia ordini), FORCE_REPORT=1.
"""

import json
import logging
import math
import statistics
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --- Parametri della strategia ---------------------------------------------
# Scelto l'8/10/2026: coin con spread basso su Alpaca (costo nascosto a ogni
# operazione), volumi decenti e niente risultati negativi in entrambe le meta'
# dell'ultimo anno. Esclusi LTC/AVAX/BCH (spread ~0,57%), DOGE/DOT/AAVE
# (negativi sia nella 1a che nella 2a meta'), meme/illiquidi (TRUMP, POL, YFI...).
BASKET = [
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "UNI/USD", "CRV/USD", "SUSHI/USD",
    "ADA/USD", "ARB/USD", "FIL/USD", "GRT/USD", "RENDER/USD", "XRP/USD",
]
BREAKOUT_BARS = 120
TREND_SMA_BARS = 700
ATR_BARS = 22
CHANDELIER_MULT = 6.0
RISK_PER_TRADE = 0.01
MAX_POSITION_PCT = 0.15
MAX_POSITIONS = 6
MIN_ORDER_USD = 10.0
# Esecuzione: ordini limite IOC al massimo MAX_SLIPPAGE oltre il prezzo medio, e mai piu'
# di DEPTH_SHARE della liquidita' disponibile nel book entro quel limite. Il 6/10
# un ordine a mercato da 15k$ su AVAX ha "scalato" il book pagando +2,6%.
MAX_SLIPPAGE = 0.005
EXIT_SLIPPAGE = 0.015  # in uscita conta piu' uscire che risparmiare
DEPTH_SHARE = 0.8
HISTORY_HOURS = 800  # margine sopra le 700 candele della media lenta
REGIME_SYMBOL = "BTC/USD"
# Volatility targeting (9/10/2026): le nuove posizioni si riducono quando la
# volatilita' di BTC a 30 giorni supera il 42% annuo (mediana dell'ultimo anno).
VOL_TARGET = 0.42
VOL_BARS = 720
REGIME_FAST_BARS = 168  # BTC deve stare anche sopra la media dell'ultima settimana

REPORT_HOUR = 21
# Strategia attiva: "daily1" = +1% al giorno su BTC (daily1.py, dal 9/10/2026,
# scelta dell'utente); "trend" = trend following orario (run_cycle).
STRATEGY = "daily1"
TZ = ZoneInfo("Europe/Rome")

TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
STATE_FILE = Path(__file__).with_name("state.json")
# Registro operazioni e saldo serale, salvati nel repo per le analisi
TRADES_FILE = Path(__file__).with_name("trades.csv")
EQUITY_FILE = Path(__file__).with_name("equity.csv")
# Jarvis (Render) riceve ogni ora lo stato del conto per il tasto "📈 Crypto"
JARVIS_URL = os.environ.get("JARVIS_URL", "https://jarvis-let1.onrender.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

DRY_RUN = os.environ.get("DRY_RUN") == "1"


# --- Alpaca ----------------------------------------------------------------
def _secret(name: str) -> str:
    value = os.environ[name].strip()
    if not value.isascii() or any(c.isspace() for c in value):
        raise RuntimeError(f"{name} contiene caratteri non validi (es. '…' o spazi): "
                           "ricopiala intera col pulsante di copia e aggiorna il secret")
    return value


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": _secret("ALPACA_KEY_ID"),
        "APCA-API-SECRET-KEY": _secret("ALPACA_SECRET_KEY"),
    }


def api(method: str, path: str, **kwargs):
    r = requests.request(method, TRADING_URL + path, headers=_headers(), timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca {method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.text else None


ASSET_INFO: dict[str, dict] = {}


def tradable_symbols() -> list[str]:
    assets = api("GET", "/v2/assets", params={"asset_class": "crypto", "status": "active"})
    ASSET_INFO.update({a["symbol"]: a for a in assets})
    available = {a["symbol"] for a in assets if a.get("tradable")}
    missing = [s for s in BASKET if s not in available]
    if missing:
        log.warning("Non tradabili su Alpaca, saltati: %s", ", ".join(missing))
    return [s for s in BASKET if s in available]


def fetch_bars(symbols: list[str]) -> dict[str, list[dict]]:
    """Candele orarie CHIUSE per ogni simbolo, dalla piu' vecchia alla piu' recente."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=HISTORY_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars: dict[str, list[dict]] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": "1Hour", "start": start, "limit": 10000}
    while True:
        r = requests.get(f"{DATA_URL}/v1beta3/crypto/us/bars", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for sym, rows in (data.get("bars") or {}).items():
            bars[sym].extend(rows)
        token = data.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    # l'ultima candela e' ancora in formazione se non e' passata un'ora dal suo inizio
    for sym, rows in bars.items():
        if rows:
            last_start = datetime.fromisoformat(rows[-1]["t"].replace("Z", "+00:00"))
            if last_start + timedelta(hours=1) > now:
                rows.pop()
    return bars


# --- Indicatori ------------------------------------------------------------
def analyze(rows: list[dict]) -> dict | None:
    if len(rows) < TREND_SMA_BARS + 1:
        return None
    highs = [b["h"] for b in rows]
    lows = [b["l"] for b in rows]
    closes = [b["c"] for b in rows]
    close = closes[-1]

    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(len(rows) - ATR_BARS, len(rows))
    ]
    atr = sum(true_ranges) / ATR_BARS
    breakout_level = max(highs[-BREAKOUT_BARS - 1:-1])
    sma = sum(closes[-TREND_SMA_BARS:]) / TREND_SMA_BARS
    stop = max(highs[-ATR_BARS:]) - CHANDELIER_MULT * atr

    return {
        "close": close,
        "atr": atr,
        "breakout_level": breakout_level,
        "sma": sma,
        "sma_fast": sum(closes[-REGIME_FAST_BARS:]) / REGIME_FAST_BARS,
        "stop": stop,
        "entry": close > breakout_level and close > sma and close > stop,
        "exit": close < stop,
        "strength": (close - breakout_level) / atr if atr else 0.0,
    }


# --- Trading ---------------------------------------------------------------
def to_position_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


def orderbook(symbol: str) -> dict:
    r = requests.get(f"{DATA_URL}/v1beta3/crypto/us/latest/orderbooks",
                     params={"symbols": symbol}, timeout=30)
    r.raise_for_status()
    return r.json()["orderbooks"][symbol]


def _round_down(x: float, step: float) -> float:
    return int(x / step) * step if step > 0 else x


def limit_ioc(symbol: str, side: str, usd: float | None = None, qty: float | None = None) -> dict | None:
    """Ordine limite IOC entro MAX_SLIPPAGE dal prezzo medio, dimensionato sulla
    liquidita' del book. Ritorna l'ordine eseguito (anche parziale) o None."""
    ob = orderbook(symbol)
    mid = (ob["a"][0]["p"] + ob["b"][0]["p"]) / 2
    info = ASSET_INFO.get(symbol, {})
    qty_step = float(info.get("min_trade_increment") or 0)
    px_step = float(info.get("price_increment") or 0)
    if side == "buy":
        limit = mid * (1 + MAX_SLIPPAGE)
        depth = sum(lvl["s"] for lvl in ob["a"] if lvl["p"] <= limit)
        qty = min(usd / limit, depth * DEPTH_SHARE)
        limit = _round_down(limit, px_step) + px_step if px_step else limit
    else:
        limit = mid * (1 - EXIT_SLIPPAGE)
        depth = sum(lvl["s"] for lvl in ob["b"] if lvl["p"] >= limit)
        qty = min(qty, depth * DEPTH_SHARE)
        limit = _round_down(limit, px_step) if px_step else limit
    qty = _round_down(qty, qty_step)
    if qty <= 0 or qty * mid < MIN_ORDER_USD:
        log.info("%s %s saltato: liquidita' nel book insufficiente", side, symbol)
        return None
    order = api("POST", "/v2/orders", json={
        "symbol": symbol, "qty": f"{qty:.10g}", "side": side, "type": "limit",
        "limit_price": f"{limit:.10g}", "time_in_force": "ioc",
    })
    order = wait_for_fill(order["id"], timeout=30)
    filled = float(order.get("filled_qty") or 0)
    if filled <= 0:
        log.info("%s %s non eseguito (stato %s)", side, symbol, order["status"])
        return None
    avg = float(order["filled_avg_price"])
    log.info("%s %s eseguito: %.8g a %.6g (medio %.6g, slippage %+.2f%%)",
             side, symbol, filled, avg, mid, (avg / mid - 1) * 100)
    return order


def append_csv(path: Path, header: str, row: list) -> None:
    new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new:
            f.write(header + "\n")
        f.write(",".join(str(x) for x in row) + "\n")


def log_trade(symbol: str, side: str, usd: float, sig: dict, reason: str) -> None:
    if DRY_RUN:
        return
    append_csv(
        TRADES_FILE, "data_utc,simbolo,lato,importo_usd,prezzo,stop,motivo",
        [datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), symbol, side,
         f"{usd:.2f}", f"{sig['close']:.6g}", f"{sig['stop']:.6g}", reason],
    )


def vol_scale(btc_rows: list[dict]) -> float:
    """Fattore 0-1 per le nuove posizioni: VOL_TARGET / volatilita' annua di BTC."""
    closes = [b["c"] for b in btc_rows[-(VOL_BARS + 1):]]
    if len(closes) < VOL_BARS:
        return 1.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    vol = statistics.pstdev(rets) * math.sqrt(24 * 365)
    scale = min(1.0, VOL_TARGET / vol) if vol > 0 else 1.0
    log.info("Volatilita' BTC 30g %.0f%%: posizioni nuove al %.0f%%", vol * 100, scale * 100)
    return scale


def run_strategy(symbols: list[str], bars: dict[str, list[dict]]) -> dict[str, dict]:
    account = api("GET", "/v2/account")
    equity = float(account["equity"])
    cash = float(account["cash"])
    positions = {p["symbol"]: p for p in api("GET", "/v2/positions")}

    signals = {s: analyze(bars.get(s, [])) for s in symbols}
    held = [s for s in symbols if to_position_symbol(s) in positions]
    state = load_state()
    stops = state.setdefault("initial_stops", {})

    # Lo stop puo' scendere (finestra di 22 ore) ma mai sotto quello fissato
    # all'entrata: cosi' la perdita massima resta ~1% del conto (9/10/2026,
    # dopo AVAX: stop sceso da 10,75 a 10,25 e perdita 2,2% invece di 1%).
    for sym in held:
        sig = signals[sym]
        if sig and sym in stops:
            sig["stop"] = max(sig["stop"], stops[sym])
            sig["exit"] = sig["close"] < sig["stop"]

    for sym in list(held):
        sig = signals[sym]
        if sig and sig["exit"]:
            log.info("USCITA %s: close %.4f < stop %.4f", sym, sig["close"], sig["stop"])
            if not DRY_RUN:
                # se il book non basta per vendere tutto, il resto parte al giro dopo
                order = limit_ioc(sym, "sell", qty=float(positions[to_position_symbol(sym)]["qty"]))
                if order:
                    value = float(order["filled_qty"]) * float(order["filled_avg_price"])
                    cash += value
                    log_trade(sym, "vendita", value, sig, "stop")
                    if float(order["filled_qty"]) >= float(positions[to_position_symbol(sym)]["qty"]) * 0.999:
                        stops.pop(sym, None)
                        save_state(state)
            held.remove(sym)

    regime = signals.get(REGIME_SYMBOL)
    if not regime or regime["close"] <= regime["sma"] or regime["close"] <= regime["sma_fast"]:
        log.info("BTC sotto la media a %d o %d ore: nessuna nuova entrata", TREND_SMA_BARS, REGIME_FAST_BARS)
        return signals

    candidates = sorted(
        (s for s in symbols if s in BASKET and s not in held and signals[s] and signals[s]["entry"]),
        key=lambda s: signals[s]["strength"],
        reverse=True,
    )
    scale = vol_scale(bars.get(REGIME_SYMBOL, []))
    for sym in candidates[: max(0, MAX_POSITIONS - len(held))]:
        sig = signals[sym]
        stop_distance_pct = (sig["close"] - sig["stop"]) / sig["close"]
        notional = min(
            equity * RISK_PER_TRADE * scale / stop_distance_pct,
            equity * MAX_POSITION_PCT * scale,
            cash * 0.98,  # margine per le commissioni
        )
        if notional < MIN_ORDER_USD:
            log.info("Entrata %s saltata: liquidita' insufficiente (%.2f$)", sym, notional)
            continue
        log.info("ENTRATA %s: %.2f$ a ~%.4f, stop %.4f", sym, notional, sig["close"], sig["stop"])
        if not DRY_RUN:
            order = limit_ioc(sym, "buy", usd=notional)
            if not order:
                continue
            notional = float(order["filled_qty"]) * float(order["filled_avg_price"])
            log_trade(sym, "acquisto", notional, sig, "breakout")
            stops[sym] = sig["stop"]
            save_state(state)
        cash -= notional

    return signals


# --- Report Telegram -------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def send_telegram(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram -> {r.status_code}: {r.text}")


def _money(x: float) -> str:
    return f"{x:,.2f}$".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(x: float) -> str:
    return f"{x:+.2f}%".replace(".", ",")


def build_report(signals: dict[str, dict], title: str | None = None, extra: list[str] | None = None) -> str:
    account = api("GET", "/v2/account")
    equity = float(account["equity"])
    cash = float(account["cash"])
    positions = api("GET", "/v2/positions")

    # Variazione nelle ultime 24 ore e dall'inizio, dallo storico del portafoglio
    day_change = total_change = None
    try:
        hist = api("GET", "/v2/account/portfolio/history", params={"period": "1D", "timeframe": "1H"})
        values = [v for v in hist.get("equity") or [] if v]
        if values:
            day_change = equity - values[0]
        hist_all = api("GET", "/v2/account/portfolio/history", params={"period": "1A", "timeframe": "1D"})
        values = [v for v in hist_all.get("equity") or [] if v]
        if values:
            total_change = (equity - values[0], values[0])
    except RuntimeError as e:
        log.warning("Storico portafoglio non disponibile: %s", e)

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fills = api("GET", "/v2/account/activities/FILL", params={"after": since, "direction": "asc"})

    today = datetime.now(TZ).strftime("%d/%m/%Y")
    lines = [f"<b>{title or f'📈 Crypto Bot — report del {today}'}</b>", ""]
    lines.append(f"Saldo: <b>{_money(equity)}</b> (liquidità {_money(cash)})")
    if day_change is not None:
        lines.append(f"Ultime 24h: {_money(day_change)} ({_pct(day_change / (equity - day_change) * 100)})")
    if total_change is not None:
        delta, base = total_change
        lines.append(f"Dall'inizio: {_money(delta)} ({_pct(delta / base * 100)})")

    lines += ["", f"<b>Posizioni aperte ({len(positions)})</b>"]
    if not positions:
        lines.append("Nessuna — il bot è in liquidità.")
    by_pos_symbol = {to_position_symbol(s): sig for s, sig in signals.items()}
    for p in positions:
        pl = float(p["unrealized_pl"])
        plpc = float(p["unrealized_plpc"]) * 100
        line = f"• {p['symbol']}: {_money(float(p['market_value']))} — {_money(pl)} ({_pct(plpc)})"
        sig = by_pos_symbol.get(p["symbol"])
        if sig:
            line += f", stop {sig['stop']:.4g}"
        lines.append(line)

    lines += ["", f"<b>Operazioni ultime 24h ({len(fills)})</b>"]
    if not fills:
        lines.append("Nessuna.")
    for f in fills:
        when = datetime.fromisoformat(f["transaction_time"].replace("Z", "+00:00")).astimezone(TZ)
        side = "Acquisto" if f["side"] == "buy" else "Vendita"
        value = float(f["qty"]) * float(f["price"])
        lines.append(f"• {when:%H:%M} {side} {f['symbol']} — {_money(value)} a {float(f['price']):.4g}")

    # Quanto manca al segnale di acquisto: spiega perche' il bot non compra
    regime = signals.get(REGIME_SYMBOL)
    if regime:
        ok = regime["close"] > regime["sma"] and regime["close"] > regime.get("sma_fast", 0)
        lines += ["", f"<b>Filtro BTC</b>: {'via libera ✅' if ok else 'acquisti bloccati ⛔ (BTC in calo)'}"]
    waiting = sorted(
        ((s, sig["breakout_level"] / sig["close"] - 1) for s, sig in signals.items()
         if sig and to_position_symbol(s) not in {p["symbol"] for p in positions}),
        key=lambda x: x[1],
    )
    if waiting:
        lines += ["", "<b>Distanza dal segnale di acquisto</b>"]
        lines += [f"• {s.split('/')[0]}: {_pct(d * 100)}" if d > 0 else f"• {s.split('/')[0]}: sopra il livello"
                  for s, d in waiting]

    lines += extra or []
    lines += ["", "<i>Conto demo Alpaca — soldi virtuali.</i>"]
    return "\n".join(lines)


def maybe_send_report(signals: dict[str, dict], extra: list[str] | None = None) -> None:
    now = datetime.now(TZ)
    state = load_state()
    today = now.date().isoformat()
    force = os.environ.get("FORCE_REPORT") == "1"
    # ">=" invece di "==": se GitHub salta o ritarda l'esecuzione delle 21, il
    # report parte comunque alla prima esecuzione utile della serata.
    if not force and (now.hour < REPORT_HOUR or state.get("last_report_date") == today):
        return
    report = build_report(signals, extra=extra)
    log.info("Report:\n%s", report)
    send_telegram(report)
    if not force:
        account = api("GET", "/v2/account")
        append_csv(EQUITY_FILE, "data,saldo_usd,liquidita_usd,posizioni",
                   [today, account["equity"], account["cash"], len(api("GET", "/v2/positions"))])
        state["last_report_date"] = today
        save_state(state)
        commit_state()


def commit_state(message: str = "Report giornaliero inviato") -> None:
    """Su GitHub Actions salva subito state.json nel repo: se il job viene
    interrotto prima della fine, il report non viene rimandato due volte."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    if not subprocess.run(["git", "status", "--porcelain", "--", "state.json", "trades.csv", "equity.csv"],
                          capture_output=True, text=True).stdout.strip():
        return
    cmds = [
        ["git", "config", "user.name", "crypto-bot"],
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        ["git", "add", "--", *[f for f in ("state.json", "trades.csv", "equity.csv") if Path(f).exists()]],
        ["git", "commit", "-m", message],
        ["git", "pull", "--rebase", "-q"],
        ["git", "push", "-q"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("%s fallito: %s", " ".join(cmd), r.stderr.strip())
            return


def push_status_to_jarvis(signals: dict[str, dict], extra: list[str] | None = None) -> None:
    """Manda a Jarvis lo stato attuale (testo semplice, senza tag HTML)."""
    text = re.sub(r"</?[bi]>", "", build_report(signals, title="📈 Crypto Bot — conto demo", extra=extra))
    try:
        # Render free si addormenta: il primo colpo puo' metterci ~1 minuto a svegliarlo
        r = requests.post(
            f"{JARVIS_URL}/crypto/{os.environ['TELEGRAM_BOT_TOKEN'].strip()}",
            json={"text": text},
            timeout=90,
        )
        r.raise_for_status()
        log.info("Stato inviato a Jarvis")
    except requests.RequestException as e:
        # non blocca il trading: al prossimo giro riprova
        log.warning("Jarvis non raggiungibile: %s", e)


def run_cycle() -> None:
    symbols = tradable_symbols()
    # posizioni aperte su coin uscite dal paniere: continuo a gestirne lo stop
    for p in api("GET", "/v2/positions"):
        sym = p["symbol"][:-3] + "/USD" if p["symbol"].endswith("USD") and "/" not in p["symbol"] else p["symbol"]
        if sym not in symbols:
            symbols.append(sym)
    bars = fetch_bars(symbols)
    signals = run_strategy(symbols, bars)
    for sym, sig in signals.items():
        if sig:
            log.info("%-9s close %-10.4g breakout %-10.4g sma700 %-10.4g stop %-10.4g %s",
                     sym, sig["close"], sig["breakout_level"], sig["sma"], sig["stop"],
                     "ENTRY" if sig["entry"] else ("EXIT" if sig["exit"] else ""))
        else:
            log.info("%-9s storico insufficiente", sym)
    commit_state("Operazioni del bot")  # no-op se non ci sono state operazioni
    maybe_send_report(signals)
    push_status_to_jarvis(signals)


def seconds_to_next_check() -> float:
    """Secondi fino al prossimo HH:02 UTC: subito dopo la chiusura della candela
    oraria, prima che il prezzo scappi dopo un breakout."""
    now = datetime.now(timezone.utc)
    nxt = now.replace(minute=2, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    return (nxt - now).total_seconds()


def wait_for_fill(order_id: str, timeout: int = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        order = api("GET", f"/v2/orders/{order_id}")
        if order["status"] in ("filled", "canceled", "expired", "rejected") or time.monotonic() > deadline:
            return order
        time.sleep(2)


def test_order(symbol: str = "BTC/USD", notional: float = 100.0) -> int:
    """Prova end-to-end sul conto demo: compra `notional`$ di `symbol`, verifica
    l'esecuzione, rivende subito e manda l'esito su Telegram."""
    pos_symbol = to_position_symbol(symbol)
    if any(p["symbol"] == pos_symbol for p in api("GET", "/v2/positions")):
        send_telegram(f"🧪 Prova ordine annullata: c'e' gia' una posizione aperta su {symbol} "
                      "della strategia, non la tocco.")
        return 0
    lines = ["<b>🧪 Prova ordine — conto demo</b>", ""]
    ok = False
    try:
        buy = api("POST", "/v2/orders", json={
            "symbol": symbol, "notional": f"{notional:.2f}",
            "side": "buy", "type": "market", "time_in_force": "gtc",
        })
        buy = wait_for_fill(buy["id"])
        log.info("Acquisto: %s", buy)
        if buy["status"] != "filled":
            lines.append(f"❌ Acquisto non eseguito (stato: {buy['status']})")
        else:
            buy_px = float(buy["filled_avg_price"])
            lines.append(f"✅ Acquisto eseguito: {float(buy['filled_qty']):.8f} {symbol} a {buy_px:,.2f}$")
            sell = api("DELETE", f"/v2/positions/{pos_symbol}")
            sell = wait_for_fill(sell["id"])
            log.info("Vendita: %s", sell)
            if sell["status"] != "filled":
                lines.append(f"❌ Vendita non eseguita (stato: {sell['status']}): chiudi la posizione a mano")
            else:
                sell_px = float(sell["filled_avg_price"])
                pl = (sell_px - buy_px) * float(sell["filled_qty"])
                lines.append(f"✅ Vendita eseguita a {sell_px:,.2f}$ (risultato {_money(pl)}, commissioni incluse nel prezzo)")
                ok = True
    except RuntimeError as e:
        lines.append(f"❌ Errore: {e}")
    lines += ["", "Il bot riesce a comprare e vendere da solo." if ok else "Qualcosa non va: guardo il log."]
    send_telegram("\n".join(lines))
    log.info("\n".join(lines))
    return 0 if ok else 1


def main() -> int:
    if os.environ.get("TEST_ORDER") == "1":
        return test_order()
    # LOOP_MINUTES > 0: resta acceso e controlla ogni ora finche' non scade il
    # tempo. Serve perche' GitHub salta molti avvii programmati: cosi' basta
    # che ne parta uno ogni ~6 ore per coprire tutte le ore.
    loop_minutes = int(os.environ.get("LOOP_MINUTES") or 0)
    deadline = time.monotonic() + loop_minutes * 60
    failures = 0
    while True:
        try:
            if STRATEGY == "daily1":
                import daily1
                daily1.tick()
            else:
                run_cycle()
        except Exception:
            failures += 1
            log.exception("Ciclo fallito")
        os.environ.pop("FORCE_REPORT", None)  # il report forzato vale solo per il primo giro
        wait = seconds_to_next_check() if STRATEGY != "daily1" else 300 - time.time() % 300 + 20
        if time.monotonic() + wait >= deadline:
            break
        log.info("Prossimo controllo tra %d minuti", wait // 60)
        time.sleep(wait)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
