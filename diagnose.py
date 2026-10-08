"""Diagnostica di sola lettura del conto demo: saldo, posizioni, ultimi ordini
eseguiti e commissioni crypto (CFEE). Non invia ordini."""

import json

import bot


def main() -> None:
    account = bot.api("GET", "/v2/account")
    print("CONTO:", {k: account[k] for k in ("equity", "cash", "last_equity", "buying_power")})
    for p in bot.api("GET", "/v2/positions"):
        print("POSIZIONE:", {k: p[k] for k in ("symbol", "qty", "avg_entry_price", "cost_basis",
                                               "market_value", "unrealized_pl")})
    orders = bot.api("GET", "/v2/orders", params={"status": "all", "limit": 20, "direction": "desc"})
    for o in orders:
        print("ORDINE:", json.dumps({k: o.get(k) for k in (
            "submitted_at", "symbol", "side", "type", "notional", "qty", "filled_qty",
            "filled_avg_price", "status")}))
    for kind in ("CFEE", "FEE", "FILL"):
        acts = bot.api("GET", f"/v2/account/activities/{kind}", params={"direction": "desc", "page_size": 20})
        for a in acts:
            print(kind + ":", json.dumps(a))


if __name__ == "__main__":
    main()
