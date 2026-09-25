"""Pool-reported 24h wallet rewards, never a promised profit or a benchmark.

Use dedicated payout addresses per machine. A shared wallet cannot establish a
per-machine floor. Network errors/missing data preserve the last server floor.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from decimal import Decimal

import httpx
import idle_mining

report = None


def number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid mining income")
    return result


def fetch(client, path):
    response = client.get("https://api.unminable.com/v4/" + path)
    response.raise_for_status()
    body = response.json()
    if body.get("success") is not True:
        raise ValueError("Pool data unavailable")
    return body["data"]


def sample(client):
    if os.getenv("PETABYTE_MINING_DEDICATED_WALLETS") != "true":
        raise ValueError("Per-machine floor requires dedicated mining payout addresses")
    worker = os.environ["PETABYTE_MINING_WORKER"]
    if not re.fullmatch(r"pb-[a-zA-Z0-9_-]{1,48}", worker):
        raise ValueError("Invalid worker")
    rewards = {}
    for coin in ("DOGE",):
        address = os.environ[coin + "_ADD"]
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{26,110}", address):
            raise ValueError("Invalid address")
        account = fetch(client, f"address/{address}?coin={coin}")
        uid = str(uuid.UUID(account["uuid"]))
        if account.get("fresh") or account.get("enabled") is not True:
            raise ValueError("Wallet has no established mining history")
        stats = fetch(client, f"account/{uid}/stats")
        # Referrals and shared wallets do not measure this computer's output.
        if stats.get("coin") != coin or number(stats["balance_referral"]) != 0:
            raise ValueError("Unattributable pool income")
        workers = fetch(client, f"account/{uid}/workers")
        found = [w for group in workers.values() for w in group.get("workers", [])]
        if not found or any(w.get("name") != worker for w in found):
            raise ValueError("Pool wallet must be exclusive to this machine's worker")
        rewards[coin] = number(stats["rewarded"]["past_24h"])
    response = client.get("https://api.kraken.com/0/public/Ticker", params={
        "pair": "DOGEUSD", "assetVersion": "1"})
    response.raise_for_status()
    ticker = response.json()
    if ticker.get("error"):
        raise ValueError("USD quotes unavailable")
    prices = ticker["result"]
    doge = rewards["DOGE"] * number(prices["DOGE/USD"]["c"][0])
    return {"doge_usd": str(doge), "hours": 24,
            "sampled_at": int(time.time()), "source": "unmineable_24h_kraken_spot"}


def heartbeat_report():
    return report


def loop():
    global report
    while True:
        if idle_mining.controller.enabled() and os.getenv("PETABYTE_MINING_FLOOR") == "true":
            try:
                with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
                    report = sample(client)
                idle_mining.STATE.mkdir(parents=True, exist_ok=True)
                (idle_mining.STATE / "income.json").write_text(json.dumps(report))
            except Exception as exc:  # noqa: BLE001 -- preserve prior floor on any upstream failure
                logging.getLogger(__name__).warning("Mining floor unavailable: %s", type(exc).__name__)
        time.sleep(600)


def start():
    if os.getenv("PETABYTE_MINING_FLOOR") == "true":
        threading.Thread(target=loop, daemon=True).start()
