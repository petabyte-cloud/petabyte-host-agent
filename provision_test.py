#!/usr/bin/env python3
"""provision_test.py — the node prices itself HONESTLY. Offline, no network.

Onboarding is one command, so most sellers never set PRICE_PER_HOUR. The agent used to
default that to a flat $1.0/hr for ANY GPU — a made-up number that mispriced every card
(a 4090 listed the same as a 2060). This proves the new behaviour:

  * an explicit PRICE_PER_HOUR always wins (the seller's number, untouched);
  * with no price set, the node asks the server for a FAIR, benchmark-anchored price for
    the GPU it detected (the same /pricing/suggest the /install page shows) — never a
    hardcoded flat rate;
  * a garbage PRICE_PER_HOUR is ignored, not crashed on, and falls through to auto-pricing;
  * if the pricing service is unreachable it falls back to a clearly-LABELLED placeholder
    and says so — it never silently invents a confident-looking rate.

Run: python provision_test.py
"""
import os

import provision

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body

    def json(self):
        return self._b


class _Client:
    """Minimal stand-in for httpx.Client — records the pricing call, returns a canned body."""
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if self._exc:
            raise self._exc
        return self._resp


def _clear():
    os.environ.pop("PRICE_PER_HOUR", None)


# 1) explicit price wins and never touches the network
_clear()
os.environ["PRICE_PER_HOUR"] = "2.25"
_c = _Client(resp=_Resp(200, {"suggested_price": 0.71, "basis": "benchmark"}))
price, basis = provision.resolve_price(_c, "RTX 4090")
ok("an explicit PRICE_PER_HOUR is used verbatim", price == 2.25 and basis == "seller-set")
ok("explicit price does NOT call the pricing service", _c.calls == [])

# 2) no price set -> benchmark-anchored suggestion for the DETECTED gpu
_clear()
_c = _Client(resp=_Resp(200, {"suggested_price": 0.71,
                              "basis": "priced on this GPU's FP16 benchmark"}))
price, basis = provision.resolve_price(_c, "RTX 4090")
ok("unset price -> uses the server's benchmark-anchored suggestion", price == 0.71)
ok("the suggestion is fetched for the detected GPU", _c.calls and _c.calls[0][1] == {"gpu_model": "RTX 4090"})
ok("the basis is labelled as auto (not a silent flat rate)", basis.startswith("auto"))

# 3) a bad PRICE_PER_HOUR is ignored, then auto-prices (no crash)
for bad in ("abc", "0", "-5", ""):
    _clear()
    os.environ["PRICE_PER_HOUR"] = bad
    _c = _Client(resp=_Resp(200, {"suggested_price": 0.5, "basis": "b"}))
    price, basis = provision.resolve_price(_c, "RTX 3060")
    ok(f"garbage PRICE_PER_HOUR={bad!r} is ignored and auto-prices instead",
       price == 0.5 and basis.startswith("auto"))

# 4) pricing service unreachable -> LABELLED placeholder, never a silent invented rate
_clear()
_c = _Client(exc=RuntimeError("connection refused"))
price, basis = provision.resolve_price(_c, "RTX 4090")
ok("unreachable pricing service -> falls back to a labelled placeholder", price == 1.0)
ok("the fallback is clearly marked as a fallback (honest, not confident)", "fallback" in basis)

# 5) a non-200 from the pricing service also falls back cleanly
_clear()
_c = _Client(resp=_Resp(503, {}))
price, basis = provision.resolve_price(_c, "RTX 4090")
ok("a 5xx from the pricing service falls back to the labelled placeholder",
   price == 1.0 and "fallback" in basis)

_clear()
print(f"\n=== provision: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
