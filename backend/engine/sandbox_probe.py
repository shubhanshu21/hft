"""Probe what Upstox's SANDBOX accepts, to settle the order-quantity questions without a real order.

    python3 -m engine.sandbox_probe [--json out.json]

Sends a small matrix of orders to the sandbox (never the live API) and prints accepted / rejected with Upstox's own error text: for each traded contract the
quantity as lots AND as units, both MARKET and LIMIT. Question it answers: does the sandbox enforce lot / unit rules for MCX and currency, and which
quantity does it accept? If it accepts both (or rejects both for an unrelated reason) the sandbox does not enforce the rule and the question stays open --
that itself is a finding. Results are saved to var/logs/sandbox_probe.json. Needs UPSTOX_SANDBOX_TOKEN in backend/.env.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from core.paths import LOG_DIR
from services.broker import sandbox_client

# (label, symbol, [quantities to try], meaning of the smallest one)
MATRIX = [
    ("equity",            "RELIANCE",  [1, 100],       "shares"),
    ("currency (USDINR)", "USDINR",    [1, 1000, 4000], "1 = one lot if lots; 1000 = one lot if units"),
    ("commodity crude",   "CRUDEOILM", [1, 10, 100],   "1 = one lot if lots; 10 = one lot if units (10 bbl)"),
    ("commodity silver",  "SILVERMIC", [1, 3],         "1 kg lot: identical in lots and units"),
    ("commodity gold",    "GOLDM",     [1, 10, 100],   "master lot 100 g, price basis 10 g: the ambiguous one"),
]


def _keys() -> dict[str, str]:
    from engine.live_dryrun import SYMBOL_MAP
    return SYMBOL_MAP


def run(place=sandbox_client.place, keys: dict[str, str] | None = None, matrix=MATRIX) -> list[dict]:
    keys = _keys() if keys is None else keys
    rows = []
    for label, sym, quantities, meaning in matrix:
        key = keys.get(sym)
        if not key:
            rows.append({"label": label, "symbol": sym, "quantity": None, "ok": None, "message": "instrument key not resolved"})
            continue
        for q in quantities:
            for order_type, price in (("MARKET", 0.0), ("LIMIT", 1.0)):
                r = place(key, q, "BUY", "I", order_type, price, tag="PROBE")
                rows.append({"label": label, "symbol": sym, "key": key, "quantity": q, "order_type": order_type, "ok": r["ok"],
                             "http_status": r["http_status"], "error_code": r["error_code"], "message": r["message"], "meaning": meaning})
    return rows


def format_rows(rows: list[dict]) -> str:
    out = []
    last = None
    for r in rows:
        if r["symbol"] != last:
            out.append(f"\n{r['label']}  ({r['symbol']})  {r.get('meaning', '')}")
            last = r["symbol"]
        if r["quantity"] is None:
            out.append(f"   {r['message']}")
            continue
        verdict = "ACCEPTED" if r["ok"] else f"REJECTED [{r['error_code']}] {r['message'][:110]}"
        out.append(f"   qty {r['quantity']:>5}  {r['order_type']:6s} -> {verdict}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(LOG_DIR / "sandbox_probe.json"))
    args = ap.parse_args(argv)
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT
    load_dotenv(BACKEND_ROOT / ".env")
    if not sandbox_client.token():
        print("UPSTOX_SANDBOX_TOKEN is empty. Upstox Developer Apps -> your sandbox app -> Access Token -> Generate, paste it into backend/.env, run again.")
        return 2
    rows = run()
    print(format_rows(rows))
    with open(args.json, "w") as fh:
        json.dump({"ran_at": datetime.now().isoformat(timespec="seconds"), "rows": rows}, fh, indent=1)
    print(f"\nsaved {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
