"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ServiceError, ValidationFailed
from .service import MetricQualityService


class Handler(BaseHTTPRequestHandler):
    service = MetricQualityService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ServiceError):
            return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        if isinstance(exc, PermissionError):
            return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
        if isinstance(exc, (KeyError, ValueError)):
            return self._json(422, {"error": {"code": "validation_failed", "message": str(exc)}})
        return self._json(500, {"error": {"code": "internal", "message": "internal error"}})

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _body(self) -> dict:
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(payload, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return payload

    def do_GET(self):
        try:
            parts = [part for part in self.path.split("/") if part]
            if self.path == "/health":
                return self._json(200, {"status": "ok", "service": "metric-quality"})
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(self._token(), parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                return self._json(200, {"events": self.service.audit(self._token(), parts[1])})
            return self._json(404, {"error": {"code": "not_found", "message": "not found"}})
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        try:
            parts = [part for part in self.path.split("/") if part]
            body = self._body()
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], int(body["sample_count"])))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(token, parts[1], body["test_frequency_hz"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approval":
                return self._json(200, self.service.approve(token, parts[1], body["decision"], body["reason"], self._expected_version(body)))
            return self._json(404, {"error": {"code": "not_found", "message": "not found"}})
        except Exception as exc:
            return self._error(exc)

    @staticmethod
    def _expected_version(body: dict) -> int:
        raw = body.get("expected_version")
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValidationFailed("expected_version 必须是非负整数")
        return raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = MetricQualityService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
