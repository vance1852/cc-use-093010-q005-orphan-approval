from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from metric_quality.api import Handler
from metric_quality.errors import Conflict, InvalidState, NotFound, ValidationFailed
from metric_quality.service import MetricQualityService
from metric_quality.storage import connect


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService()
        self.service.auth.create_user("engineer", "engineer-pass", "engineer")
        self.service.auth.create_user("quality", "quality-pass", "quality")
        self.service.auth.create_user("quality2", "quality-pass", "quality")
        self.service.auth.create_user("operator", "operator-pass", "operator")
        self.engineer_token = self.service.auth.login("engineer", "engineer-pass")
        self.token = self.service.auth.login("quality", "quality-pass")
        self.token2 = self.service.auth.login("quality2", "quality-pass")
        self.operator_token = self.service.auth.login("operator", "operator-pass")

    def create_lot(self, lot_id: str = "LOT-1") -> dict:
        return self.service.create_lot(self.engineer_token, lot_id, "cross-border-service-index", "POLICY-3.2", 10)

    def fragments(self, lot_id: str) -> tuple[list, list]:
        approvals = self.service.db.execute("SELECT * FROM approvals WHERE lot_id=?", (lot_id,)).fetchall()
        events = self.service.db.execute("SELECT * FROM lot_events WHERE lot_id=?", (lot_id,)).fetchall()
        return list(approvals), list(events)


class ApprovalTransactionTests(ServiceFixture):
    def test_missing_lot_leaves_no_fragments(self):
        """事故场景：批次不存在时报错，且不得留下审批、状态或审计残片。"""
        with self.assertRaises(NotFound) as ctx:
            self.service.approve(self.token, "LOT-TYPO", "release", "night import verified", 0)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(self.fragments("LOT-TYPO"), ([], []))

    def test_new_lot_does_not_inherit_failed_request(self):
        """同名批次建立后，历史里不得出现此前失败请求的痕迹。"""
        with self.assertRaises(NotFound):
            self.service.approve(self.token, "LOT-TYPO", "release", "night import verified", 0)
        lot = self.create_lot("LOT-TYPO")
        self.assertEqual(lot["status"], "engineering")
        self.assertEqual(lot["version"], 0)
        self.assertEqual([e["event_type"] for e in self.service.audit(self.token, "LOT-TYPO")], ["created"])
        approvals, _ = self.fragments("LOT-TYPO")
        self.assertEqual(approvals, [])

    def test_successful_approval_is_atomic_and_audited(self):
        self.create_lot()
        outcome = self.service.approve(self.token, "LOT-1", "release", "night import verified", 0)
        self.assertFalse(outcome["replayed"])
        self.assertEqual(outcome["decision"], "release")
        self.assertEqual(outcome["status"], "released")
        self.assertEqual(outcome["version"], 1)
        lot = self.service.get_lot(self.token, "LOT-1")
        self.assertEqual((lot["status"], lot["version"]), ("released", 1))
        approvals, events = self.fragments("LOT-1")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["lot_version"], 0)
        self.assertEqual([e["event_type"] for e in events], ["created", "approval"])

    def test_identical_replay_returns_original_decision(self):
        self.create_lot()
        first = self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 0)
        replay = self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 0)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["decision"], first["decision"])
        self.assertEqual(replay["decided_at"], first["decided_at"])
        # 重放不产生新状态、新版本或新审计事件
        lot = self.service.get_lot(self.token, "LOT-1")
        self.assertEqual((lot["status"], lot["version"]), ("hold", 1))
        _, events = self.fragments("LOT-1")
        self.assertEqual([e["event_type"] for e in events], ["created", "approval"])

    def test_changed_decision_or_reason_conflicts(self):
        self.create_lot()
        self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 0)
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "LOT-1", "release", "awaiting review", 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "LOT-1", "hold", "different reason", 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 3)
        # 冲突同样不留残片
        approvals, events = self.fragments("LOT-1")
        self.assertEqual(len(approvals), 1)
        self.assertEqual([e["event_type"] for e in events], ["created", "approval"])

    def test_stale_expected_version_conflicts(self):
        self.create_lot()
        self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 0)
        with self.assertRaises(Conflict) as ctx:
            self.service.approve(self.token2, "LOT-1", "release", "second review", 0)
        self.assertIn("版本", str(ctx.exception))
        # 携带当前版本重试则成功
        outcome = self.service.approve(self.token2, "LOT-1", "release", "second review", 1)
        self.assertEqual(outcome["version"], 2)
        self.assertEqual(self.service.get_lot(self.token, "LOT-1")["status"], "released")

    def test_terminal_state_rejects_further_approval(self):
        self.create_lot()
        self.service.approve(self.token, "LOT-1", "release", "night import verified", 0)
        with self.assertRaises(InvalidState) as ctx:
            self.service.approve(self.token2, "LOT-1", "hold", "second thoughts", 1)
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(self.fragments("LOT-1")[0][0]["decision"], "release")

    def test_hold_state_allows_followup_decision(self):
        self.create_lot()
        self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 0)
        outcome = self.service.approve(self.token2, "LOT-1", "release", "review passed", 1)
        self.assertEqual(outcome["status"], "released")

    def test_permission_failure_leaves_no_fragments(self):
        self.create_lot()
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator_token, "LOT-1", "release", "not my job", 0)
        approvals, events = self.fragments("LOT-1")
        self.assertEqual(approvals, [])
        self.assertEqual([e["event_type"] for e in events], ["created"])
        self.assertEqual(self.service.get_lot(self.token, "LOT-1")["status"], "engineering")

    def test_validation_failure_leaves_no_fragments(self):
        self.create_lot()
        for decision, reason, version in (("ship-it", "ok", 0), ("hold", "  ", 0), ("hold", "ok", -1)):
            with self.assertRaises(ValidationFailed):
                self.service.approve(self.token, "LOT-1", decision, reason, version)
        approvals, events = self.fragments("LOT-1")
        self.assertEqual(approvals, [])
        self.assertEqual([e["event_type"] for e in events], ["created"])

    def test_audit_shows_only_effective_decisions(self):
        self.create_lot()
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "LOT-1", "release", "wrong version", 5)
        self.service.approve(self.token, "LOT-1", "hold", "awaiting review", 0)
        audit = self.service.audit(self.token, "LOT-1")
        approval_events = [e for e in audit if e["event_type"] == "approval"]
        self.assertEqual(len(approval_events), 1)
        self.assertEqual(json.loads(approval_events[0]["payload"])["decision"], "hold")

    def test_error_kinds_are_distinguishable(self):
        self.create_lot()
        with self.assertRaises(NotFound):
            self.service.approve(self.token, "LOT-MISSING", "hold", "r", 0)
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "LOT-1", "hold", "r", 9)
        self.service.approve(self.token, "LOT-1", "reject", "failed checks", 0)
        with self.assertRaises(InvalidState):
            self.service.approve(self.token2, "LOT-1", "hold", "r", 1)
        self.assertEqual(NotFound.status, 404)
        self.assertEqual((InvalidState.status, Conflict.status), (409, 409))
        self.assertNotEqual(NotFound.code, Conflict.code)
        self.assertNotEqual(InvalidState.code, Conflict.code)


class MigrationTests(unittest.TestCase):
    def test_existing_database_is_migrated_and_purged(self):
        """旧库补列，并清除事故期间留下的孤儿审批与事件。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.executescript("""
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
            INSERT INTO metric_batches VALUES('LOT-REAL','p','r',5,'engineering','op','2026-10-01','2026-10-01');
            INSERT INTO approvals VALUES('LOT-GHOST','qa','release','phantom','2026-10-02');
            INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at)
             VALUES('LOT-GHOST','approval','qa','{}','2026-10-02');
            """)
            legacy.commit()
            legacy.close()

            migrated = connect(path)
            try:
                batch_columns = {row[1] for row in migrated.execute("PRAGMA table_info(metric_batches)")}
                approval_columns = {row[1] for row in migrated.execute("PRAGMA table_info(approvals)")}
                self.assertIn("version", batch_columns)
                self.assertIn("lot_version", approval_columns)
                self.assertEqual(migrated.execute("SELECT count(*) FROM approvals").fetchone()[0], 0)
                self.assertEqual(migrated.execute("SELECT count(*) FROM lot_events").fetchone()[0], 0)
                self.assertEqual(migrated.execute("SELECT version FROM metric_batches WHERE lot_id='LOT-REAL'").fetchone()[0], 0)
            finally:
                migrated.close()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Handler.service = MetricQualityService()
        Handler.service.auth.create_user("engineer", "engineer-pass", "engineer")
        Handler.service.auth.create_user("quality", "quality-pass", "quality")
        Handler.service.auth.create_user("quality2", "quality-pass", "quality")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_approval_flow_and_error_distinction_over_http(self):
        _, body = self.call("POST", "/login", {"user_id": "engineer", "password": "engineer-pass"})
        engineer_token = body["token"]
        status, body = self.call("POST", "/login", {"user_id": "quality", "password": "quality-pass"})
        self.assertEqual(status, 200)
        token = body["token"]

        status, body = self.call("POST", "/lots", {"lot_id": "LOT-HTTP", "product": "p", "process_rev": "r", "sample_count": 5}, engineer_token)
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 0)

        # 未找到：404 + not_found
        status, body = self.call("POST", "/lots/LOT-GHOST/approval", {"decision": "release", "reason": "x", "expected_version": 0}, token)
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

        # 过期版本：409 + conflict
        status, body = self.call("POST", "/lots/LOT-HTTP/approval", {"decision": "release", "reason": "x", "expected_version": 7}, token)
        self.assertEqual((status, body["error"]["code"]), (409, "conflict"))

        # 成功审批
        status, body = self.call("POST", "/lots/LOT-HTTP/approval", {"decision": "release", "reason": "verified", "expected_version": 0}, token)
        self.assertEqual(status, 200)
        self.assertEqual((body["status"], body["replayed"]), ("released", False))

        # 安全重放：返回原决定
        status, body = self.call("POST", "/lots/LOT-HTTP/approval", {"decision": "release", "reason": "verified", "expected_version": 0}, token)
        self.assertEqual((status, body["replayed"]), (200, True))

        # 状态不允许：409 + invalid_state（另一位审批人在已放行批次上审批）
        _, body = self.call("POST", "/login", {"user_id": "quality2", "password": "quality-pass"})
        token2 = body["token"]
        status, body = self.call("POST", "/lots/LOT-HTTP/approval", {"decision": "hold", "reason": "again", "expected_version": 1}, token2)
        self.assertEqual((status, body["error"]["code"]), (409, "invalid_state"))

        # 审计只展示生效决定
        status, body = self.call("GET", "/lots/LOT-HTTP/audit", token=token)
        self.assertEqual(status, 200)
        self.assertEqual([e["event_type"] for e in body["events"]], ["created", "approval"])


if __name__ == "__main__":
    unittest.main()
