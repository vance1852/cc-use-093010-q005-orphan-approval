"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import sqlite3
import threading
import uuid

from .analytics import confidence_interval, summarize_response_profile, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .storage import connect, event, transaction, utcnow

DECISIONS = {"release", "hold", "reject"}
STATUS_BY_DECISION = {"release": "released", "hold": "hold", "reject": "rejected"}
APPROVABLE_STATES = {"engineering", "hold"}


class MetricQualityService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database, check_same_thread=False)
        self.auth = Auth(self.db)
        self._lock = threading.RLock()

    def bootstrap_admin(self, user_id: str = "admin", password: str = "metric-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, sample_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if sample_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValidationFailed("lot fields are invalid")
        now = utcnow()
        with self._lock:
            try:
                with transaction(self.db):
                    self.db.execute(
                        "INSERT INTO metric_batches(lot_id,product,process_rev,sample_count,status,owner,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (lot_id, product, process_rev, sample_count, "engineering", actor.user_id, now, now),
                    )
                    event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"批次已存在: {lot_id}") from exc
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"批次不存在: {lot_id}")
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, test_frequency_hz: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with self._lock:
            with transaction(self.db):
                if not self.db.execute("SELECT 1 FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone():
                    raise NotFound(f"批次不存在: {lot_id}")
                self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(test_frequency_hz), float(response), float(noise), instrument, actor.user_id, utcnow()))
                event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "test_frequency_hz": test_frequency_hz})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT test_frequency_hz,response FROM measurements WHERE lot_id=? ORDER BY test_frequency_hz", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_response_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["sample_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "response_profile": summary.__dict__, "yield": rates, "response_ci": ci}

    @staticmethod
    def _approval_outcome(lot_id: str, reviewer: str, decision: str, reason: str, lot_version: int, decided_at: str, replayed: bool) -> dict:
        return {
            "lot_id": lot_id,
            "reviewer": reviewer,
            "decision": decision,
            "reason": reason,
            "status": STATUS_BY_DECISION[decision],
            "version": lot_version + 1,
            "decided_at": decided_at,
            "replayed": replayed,
        }

    def approve(self, token: str, lot_id: str, decision: str, reason: str, expected_version: int) -> dict:
        """在单个原子边界内完成审批：存在性、权限、状态、预期版本与决定写入同生共死。

        完全相同的重放（同审批人、同决定、同理由、同预期版本）返回原决定；
        决定、理由或版本不一致则以明确冲突拒绝，任何失败都不留残片。
        """
        if decision not in DECISIONS or not reason.strip():
            raise ValidationFailed("decision and reason are required")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValidationFailed("expected_version must be a non-negative integer")
        with self._lock:
            with transaction(self.db):
                actor = self.auth.require(token, "approve")
                lot = self.db.execute("SELECT * FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
                if lot is None:
                    raise NotFound(f"批次不存在: {lot_id}")
                existing = self.db.execute(
                    "SELECT * FROM approvals WHERE lot_id=? AND reviewer=?", (lot_id, actor.user_id)
                ).fetchone()
                if existing is not None:
                    if existing["decision"] == decision and existing["reason"] == reason and existing["lot_version"] == expected_version:
                        return self._approval_outcome(lot_id, actor.user_id, decision, reason, expected_version, existing["created_at"], True)
                    raise Conflict("该审批人对本批次已记录不同决定、理由或版本")
                if lot["status"] not in APPROVABLE_STATES:
                    raise InvalidState(f"批次状态 {lot['status']} 不允许审批")
                if lot["version"] != expected_version:
                    raise Conflict(f"预期版本 {expected_version} 与当前版本 {lot['version']} 不一致")
                now = utcnow()
                self.db.execute(
                    "INSERT INTO approvals(lot_id,reviewer,decision,reason,lot_version,created_at) VALUES(?,?,?,?,?,?)",
                    (lot_id, actor.user_id, decision, reason, expected_version, now),
                )
                cursor = self.db.execute(
                    "UPDATE metric_batches SET status=?,version=version+1,updated_at=? WHERE lot_id=? AND version=?",
                    (STATUS_BY_DECISION[decision], now, lot_id, expected_version),
                )
                if cursor.rowcount != 1:
                    raise Conflict(f"批次 {lot_id} 版本已变化")
                event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason, "lot_version": expected_version})
        return self._approval_outcome(lot_id, actor.user_id, decision, reason, expected_version, now, False)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
