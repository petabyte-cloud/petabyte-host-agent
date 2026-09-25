"""Pool parsing contract is fail-closed; no real wallet or mining required (DOGE only)."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mining_income as m


class IncomeTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PETABYTE_MINING_DEDICATED_WALLETS": "true",
                   "PETABYTE_MINING_WORKER": "pb-test", "DOGE_ADD": "D" * 34}, clear=True).start()
        self.other_worker = False
        self.referrals = "0"

        def fetch(client, path):
            if path.startswith("address"):
                return {"uuid": "b3f41b11-8f44-4aaa-8171-78380c44d255", "enabled": True, "fresh": False}
            if path.endswith("workers"):
                return {"fishhash": {"workers": [{"name": "other" if self.other_worker else "pb-test"}]}}
            return {"coin": "DOGE", "balance_referral": self.referrals, "rewarded": {"past_24h": "1"}}
        patch.object(m, "fetch", side_effect=fetch).start()
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "error": [], "result": {"DOGE/USD": {"c": [".10"]}}})
        self.client = SimpleNamespace(get=lambda *a, **kw: response)

    def test_combines_real_reward_amount_with_doge_quote(self):
        out = m.sample(self.client)
        self.assertEqual(out["doge_usd"], "0.10")
        self.assertEqual(out["hours"], 24)
        self.assertNotIn("xmr_usd", out)

    def test_shared_wallet_and_referrals_cannot_be_per_machine_income(self):
        self.other_worker = True
        with self.assertRaises(ValueError):
            m.sample(self.client)
        self.other_worker = False
        self.referrals = "1"
        with self.assertRaises(ValueError):
            m.sample(self.client)


if __name__ == "__main__":
    unittest.main()
