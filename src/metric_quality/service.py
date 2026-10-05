"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import sqlite3
import uuid

from .analytics import confidence_interval, summarize_response_profile, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .storage import connect, event, transaction, utcnow

DECISION_STATUSES = {"release": "released", "hold": "hold", "reject": "rejected"}
# 已放行或已驳回的批次是终态，不允许再次审批。
APPROVABLE_STATUSES = {"engineering", "hold"}


class MetricQualityService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

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
        try:
            with transaction(self.db):
                # 失败请求可能曾在同名批次建立前留下审批或事件残片，建批时一并清除，
                # 保证新批次只携带自己的历史。
                self.db.execute("DELETE FROM approvals WHERE lot_id=?", (lot_id,))
                self.db.execute("DELETE FROM lot_events WHERE lot_id=?", (lot_id,))
                self.db.execute(
                    "INSERT INTO metric_batches(lot_id,product,process_rev,sample_count,status,owner,revision,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (lot_id, product, process_rev, sample_count, "engineering", actor.user_id, 1, now, now),
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
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone():
                raise NotFound(f"批次不存在: {lot_id}")
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(test_frequency_hz), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "test_frequency_hz": test_frequency_hz})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        lot = self.get_lot(token, lot_id)
        rows = self.db.execute("SELECT test_frequency_hz,response FROM measurements WHERE lot_id=? ORDER BY test_frequency_hz", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValidationFailed("three measurements are required")
        summary = summarize_response_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(lot["sample_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "response_profile": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str, expected_revision: int) -> dict:
        """在单个事务内完成存在性、状态、版本校验和决定写入。

        完全相同的重放返回已记录的决定；决定、理由或预期版本不一致，
        以及批次版本已经前进，都会以明确冲突拒绝，且不留下任何残片。
        """
        actor = self.auth.require(token, "approve")
        if decision not in DECISION_STATUSES or not reason.strip():
            raise ValidationFailed("decision and reason are required")
        with transaction(self.db):
            row = self.db.execute("SELECT status,revision FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
            if not row:
                raise NotFound(f"批次不存在: {lot_id}")
            existing = self.db.execute(
                "SELECT decision,reason,expected_revision,created_at FROM approvals WHERE lot_id=? AND reviewer=?",
                (lot_id, actor.user_id),
            ).fetchone()
            if existing:
                recorded = (existing["decision"], existing["reason"], existing["expected_revision"])
                if recorded != (decision, reason, expected_revision):
                    raise Conflict("同一审核人对该批次已记录不同的审批请求")
                result = {
                    "lot_id": lot_id,
                    "status": row["status"],
                    "revision": row["revision"],
                    "decision": existing["decision"],
                    "reason": existing["reason"],
                    "reviewer": actor.user_id,
                    "expected_revision": existing["expected_revision"],
                    "decided_at": existing["created_at"],
                    "replayed": True,
                }
            else:
                if row["revision"] != expected_revision:
                    raise Conflict(f"批次版本已前进到 {row['revision']}，请重新读取后再审批")
                if row["status"] not in APPROVABLE_STATUSES:
                    raise InvalidState(f"批次当前状态 {row['status']} 不允许审批")
                now = utcnow()
                cursor = self.db.execute(
                    "UPDATE metric_batches SET status=?,revision=revision+1,updated_at=? WHERE lot_id=? AND revision=?",
                    (DECISION_STATUSES[decision], now, lot_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise Conflict("批次版本已前进，请重新读取后再审批")
                self.db.execute(
                    "INSERT INTO approvals(lot_id,reviewer,decision,reason,expected_revision,created_at) VALUES(?,?,?,?,?,?)",
                    (lot_id, actor.user_id, decision, reason, expected_revision, now),
                )
                event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason, "expected_revision": expected_revision})
                result = {
                    "lot_id": lot_id,
                    "status": DECISION_STATUSES[decision],
                    "revision": expected_revision + 1,
                    "decision": decision,
                    "reason": reason,
                    "reviewer": actor.user_id,
                    "expected_revision": expected_revision,
                    "decided_at": now,
                    "replayed": False,
                }
        return result

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        if not self.db.execute("SELECT 1 FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone():
            raise NotFound(f"批次不存在: {lot_id}")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
