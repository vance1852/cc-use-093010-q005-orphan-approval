from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from metric_quality.errors import Conflict, InvalidState, NotFound, ValidationFailed
from metric_quality.service import MetricQualityService


class MetricQualityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService()
        self.service.auth.create_user("quality-lead", "quality-pass", "quality")
        self.service.auth.create_user("second-reviewer", "reviewer-pass", "quality")
        self.service.auth.create_user("operator", "operator-pass", "operator")
        self.service.auth.create_user("engineer", "engineer-pass", "engineer")
        self.quality = self.service.auth.login("quality-lead", "quality-pass")
        self.second = self.service.auth.login("second-reviewer", "reviewer-pass")
        self.operator = self.service.auth.login("operator", "operator-pass")
        self.engineer = self.service.auth.login("engineer", "engineer-pass")
        self.service.create_lot(self.engineer, "LOT-1", "service-index", "REV-1", 10)

    def _counts(self, lot_id: str) -> tuple[int, int]:
        approvals = self.service.db.execute("SELECT count(*) FROM approvals WHERE lot_id=?", (lot_id,)).fetchone()[0]
        events = self.service.db.execute("SELECT count(*) FROM lot_events WHERE lot_id=?", (lot_id,)).fetchone()[0]
        return approvals, events

    def test_approve_missing_lot_leaves_no_residue(self) -> None:
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "LOT-404", "release", "夜间导入误输编号", 1)
        self.assertEqual(self._counts("LOT-404"), (0, 0))
        self.assertEqual(
            self.service.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0,
        )

    def test_new_lot_does_not_inherit_failed_request(self) -> None:
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "LOT-LATE", "release", "失败请求", 1)
        self.service.create_lot(self.engineer, "LOT-LATE", "service-index", "REV-2", 5)
        lot = self.service.get_lot(self.quality, "LOT-LATE")
        self.assertEqual(lot["status"], "engineering")
        self.assertEqual(lot["revision"], 1)
        events = self.service.audit(self.quality, "LOT-LATE")
        self.assertEqual([e["event_type"] for e in events], ["created"])
        self.assertEqual(self._counts("LOT-LATE")[0], 0)

    def test_approve_hold_then_identical_replay_returns_original(self) -> None:
        first = self.service.approve(self.quality, "LOT-1", "hold", "等待复核", 1)
        self.assertFalse(first["replayed"])
        self.assertEqual(first["status"], "hold")
        self.assertEqual(first["revision"], 2)
        replay = self.service.approve(self.quality, "LOT-1", "hold", "等待复核", 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["decision"], first["decision"])
        self.assertEqual(replay["decided_at"], first["decided_at"])
        self.assertEqual(self._counts("LOT-1"), (1, 2))
        events = [e["event_type"] for e in self.service.audit(self.quality, "LOT-1")]
        self.assertEqual(events, ["created", "approval"])

    def test_changed_decision_reason_or_version_conflicts(self) -> None:
        self.service.approve(self.quality, "LOT-1", "hold", "等待复核", 1)
        for decision, reason, revision in (
            ("release", "等待复核", 2),
            ("hold", "改写理由", 2),
            ("hold", "等待复核", 7),
        ):
            with self.assertRaises(Conflict):
                self.service.approve(self.quality, "LOT-1", decision, reason, revision)
        self.assertEqual(self._counts("LOT-1"), (1, 2))
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "hold")

    def test_stale_version_conflicts_and_current_version_succeeds(self) -> None:
        self.service.approve(self.quality, "LOT-1", "hold", "等待复核", 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.second, "LOT-1", "release", "版本已过期", 1)
        self.assertEqual(self._counts("LOT-1"), (1, 2))
        decided = self.service.approve(self.second, "LOT-1", "release", "复核通过", 2)
        self.assertEqual(decided["status"], "released")
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["revision"], 3)

    def test_terminal_state_rejects_further_approval(self) -> None:
        self.service.approve(self.quality, "LOT-1", "release", "放行", 1)
        with self.assertRaises(InvalidState):
            self.service.approve(self.second, "LOT-1", "hold", "终态不可再审", 2)
        self.assertEqual(self._counts("LOT-1"), (1, 2))
        lot = self.service.get_lot(self.quality, "LOT-1")
        self.assertEqual(lot["status"], "released")
        self.assertEqual(lot["revision"], 2)

    def test_permission_denied_leaves_no_residue(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator, "LOT-1", "release", "越权审批", 1)
        self.assertEqual(self._counts("LOT-1"), (0, 1))

    def test_invalid_decision_rejected_before_write(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.approve(self.quality, "LOT-1", "maybe", "非法决定", 1)
        with self.assertRaises(ValidationFailed):
            self.service.approve(self.quality, "LOT-1", "hold", "  ", 1)
        self.assertEqual(self._counts("LOT-1"), (0, 1))

    def test_audit_only_shows_effective_decisions(self) -> None:
        with self.assertRaises(NotFound):
            self.service.audit(self.quality, "LOT-404")
        self.service.approve(self.quality, "LOT-1", "hold", "等待复核", 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.second, "LOT-1", "reject", "过期版本", 1)
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "LOT-GONE", "reject", "不存在", 1)
        events = self.service.audit(self.quality, "LOT-1")
        self.assertEqual([e["event_type"] for e in events], ["created", "approval"])

    def test_missing_lot_errors_are_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_lot(self.quality, "LOT-404")
        with self.assertRaises(NotFound):
            self.service.add_measurement(self.quality, "LOT-404", 450, 0.9, 0.01, "gateway")
        with self.assertRaises(NotFound):
            self.service.analyze(self.quality, "LOT-404")

    def test_duplicate_lot_conflicts_without_touching_history(self) -> None:
        self.service.approve(self.quality, "LOT-1", "hold", "等待复核", 1)
        with self.assertRaises(Conflict):
            self.service.create_lot(self.engineer, "LOT-1", "service-index", "REV-9", 3)
        self.assertEqual(self._counts("LOT-1"), (1, 2))
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "hold")


class MetricQualityMigrationTests(unittest.TestCase):
    def test_legacy_database_gains_version_columns_and_stays_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.executescript(
                """
                CREATE TABLE metric_batches(
                 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
                 sample_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE lot_events(
                 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
                 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE approvals(
                 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
                 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
                INSERT INTO metric_batches VALUES('LOT-OLD','idx','R1',5,'engineering','op','t','t');
                INSERT INTO approvals VALUES('LOT-GHOST','reviewer','release','失败请求残片','t');
                INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at)
                 VALUES('LOT-GHOST','approval','reviewer','{"decision":"release"}','t');
                """
            )
            legacy.commit()
            legacy.close()

            service = MetricQualityService(str(path))
            service.auth.create_user("quality-lead", "quality-pass", "quality")
            service.auth.create_user("engineer", "engineer-pass", "engineer")
            token = service.auth.login("quality-lead", "quality-pass")
            engineer = service.auth.login("engineer", "engineer-pass")

            lot = service.get_lot(token, "LOT-OLD")
            self.assertEqual(lot["revision"], 1)
            decided = service.approve(token, "LOT-OLD", "hold", "迁移后审批", 1)
            self.assertEqual(decided["revision"], 2)

            # 历史失败请求留下的残片不会挂到新建的同名批次上。
            service.create_lot(engineer, "LOT-GHOST", "idx", "R2", 3)
            orphans = service.db.execute(
                "SELECT count(*) FROM approvals WHERE lot_id='LOT-GHOST'"
            ).fetchone()[0]
            self.assertEqual(orphans, 0)
            events = service.audit(token, "LOT-GHOST")
            self.assertEqual([e["event_type"] for e in events], ["created"])


if __name__ == "__main__":
    unittest.main()
