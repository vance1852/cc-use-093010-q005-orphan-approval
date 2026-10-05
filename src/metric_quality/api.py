"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import MetricQualityError
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

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "metric-quality"})
        try:
            if self.path.startswith("/lots/") and self.path.endswith("/events"):
                return self._json(200, {"events": self.service.audit(self._token(), self.path.split("/")[2])})
            if self.path.startswith("/lots/"):
                return self._json(200, self.service.get_lot(self._token(), self.path.split("/", 2)[2]))
            return self._error(404, "route_not_found", "接口不存在")
        except MetricQualityError as exc:
            return self._error(exc.status, exc.code, str(exc))
        except PermissionError as exc:
            return self._error(403, "forbidden", str(exc))
        except Exception as exc:
            return self._error(400, "bad_request", str(exc))

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["sample_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["test_frequency_hz"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                return self._json(200, self.service.analyze(token, self.path.split("/")[2]))
            if self.path.startswith("/lots/") and self.path.endswith("/approval"):
                lot_id = self.path.split("/")[2]
                return self._json(200, self.service.approve(token, lot_id, body["decision"], body["reason"], int(body["expected_revision"])))
            return self._error(404, "route_not_found", "接口不存在")
        except MetricQualityError as exc:
            return self._error(exc.status, exc.code, str(exc))
        except PermissionError as exc:
            return self._error(403, "forbidden", str(exc))
        except (KeyError, TypeError) as exc:
            return self._error(422, "invalid_request", str(exc))
        except Exception as exc:
            return self._error(400, "bad_request", str(exc))


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
