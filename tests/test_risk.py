import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import RiskConfig
from app.db import Account, Database
from app.risk import RiskEngine


class RiskRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "risk-test.db"
        self.db = Database(f"sqlite:///{db_path}")
        self.db.init()
        with self.db.session() as sess:
            sess.add(Account(name="acc1", status="active", risk_state={}))
            sess.commit()
        cfg = RiskConfig(recover_success_needed=3, min_request_gap_seconds=0)
        self.risk = RiskEngine(cfg, self.db)

    def tearDown(self):
        self.db.engine.dispose()
        self.tmp.cleanup()

    def _account(self):
        with self.db.session() as sess:
            return sess.scalar(select(Account).where(Account.name == "acc1"))

    def test_recovery_persists_active_status_and_does_not_repeat_probe(self):
        account = self._account()
        self.risk.report_risk_signal(account, "http_429")
        state = self.risk.state_of(account)
        self.assertEqual(state["level"], 1)
        self.assertEqual(account.status, "cooldown")

        state["cooldown_until"] = (
            datetime.now() - timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        account.risk_state = state
        self.risk._save(account, state)

        self.risk.check(account, "search")
        state = self.risk.state_of(account)
        self.assertTrue(state["probe_pending"])
        self.assertIsNone(state["cooldown_until"])

        self.risk.report_success(account)
        self.assertFalse(self.risk.state_of(account)["probe_pending"])
        self.risk.check(account, "search")
        self.assertFalse(self.risk.state_of(account)["probe_pending"])

        self.risk.report_success(account)
        self.risk.report_success(account)

        restored = self._account()
        self.assertEqual(restored.status, "active")
        self.assertEqual(self.risk.state_of(restored)["level"], 0)
        self.assertIsNone(self.risk.state_of(restored)["cooldown_until"])


if __name__ == "__main__":
    unittest.main()
