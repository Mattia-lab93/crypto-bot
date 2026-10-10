# Crypto Bot

Bot di trading automatico su **conto demo Alpaca** (soldi virtuali). Gira gratis su
GitHub Actions (repo pubblico, minuti illimitati): ogni job resta acceso ~6 ore e
controlla il mercato ogni ora, perche' GitHub salta molti avvii programmati, e alle 21:00 manda un report
su Telegram tramite il bot di Jarvis.

## Strategia +1% al giorno (attiva 9-10/10/2026, ora spenta)

Vedi `daily1.py`: a mezzanotte fissa il saldo di partenza; all'1:00 compra BTC con tutto
il conto ogni giorno (filtri disattivati dal 10/10, `USE_FILTERS` in `daily1.py`);
vendita limite al prezzo che porta il conto a +1%; stop a -3% del saldo del mattino;
chiusura alle 23:55. Backtest 1 anno con costi reali: -2% (96 combinazioni testate, tutte in
perdita). Si cambia strategia con `STRATEGY` in cima a `bot.py`.

## Strategia attiva: trend following (dal 10/10/2026, rischio 2% per operazione)

Breakout con filtro di trend, solo long, candele da 1 ora, paniere di 13 crypto in USD:
BTC, ETH, SOL, LINK, UNI, CRV, SUSHI, ADA, ARB, FIL, GRT, RENDER, XRP (scelte per spread
basso su Alpaca e risultati non negativi in entrambe le meta' dell'ultimo anno).

- **Entrata**: chiusura sopra il massimo delle 120 ore precedenti, sopra la media a 700 ore,
  e BTC sopra la sua media a 700 ore e a 168 ore (ultima settimana).
- **Uscita**: chandelier stop = massimo delle ultime 22 ore − 6 × ATR(22), mai sotto lo stop
  fissato all'entrata (cosi' la perdita massima resta ~1% del conto).
- **Size**: ~2% del conto a rischio per operazione, max 15% per coin, max 6 posizioni; ridotta
  in proporzione quando la volatilita' di BTC a 30 giorni supera il 42% annuo (volatility targeting).
- **Posizione di riserva** (regola dell'utente): se passano 24 ore senza posizioni, compra il 10%
  del conto della coin piu' forte degli ultimi 7 giorni tra BTC, ETH e SOL, con lo stesso stop.
- **Esecuzione**: ordini limite IOC (+0,5% acquisti, −1,5% vendite), max 80% della liquidita' del book.

Backtest ott 2025 → ott 2026 (anno ribassista, paniere −40%): circa −4%, max drawdown ~36%,
109 operazioni. I risultati passati non garantiscono quelli futuri.

I parametri sono in cima a `bot.py`.

## Setup

1. Crea un account paper su https://alpaca.markets e genera le API key **Paper**.
2. Crea un repository **privato** su GitHub e fai il push di questa cartella.
3. In *Settings → Secrets and variables → Actions* aggiungi:
   `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `TELEGRAM_BOT_TOKEN` (quello di Jarvis), `TELEGRAM_CHAT_ID`.
4. In *Actions → Crypto Bot → Run workflow* lancia una prova con "Simula" e "Invia subito il report".

## Esecuzione manuale locale

```
set ALPACA_KEY_ID=...
set ALPACA_SECRET_KEY=...
set TELEGRAM_BOT_TOKEN=...
set TELEGRAM_CHAT_ID=...
set DRY_RUN=1
py bot.py
```

## Registri

- `trades.csv`: ogni acquisto e vendita del bot.
- `equity.csv`: saldo di ogni sera, al momento del report.
