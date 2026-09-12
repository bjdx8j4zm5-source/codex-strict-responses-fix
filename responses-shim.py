#!/usr/bin/env python3
"""Local passthrough proxy that repairs standalone tool outputs lacking call_id.

Codex (>= rust-v0.151.0-alpha.4) delivers automation triggers as a standalone
`function_call_output` item without `call_id`. Strict Responses providers reject
the whole request with HTTP 400 `missing field call_id`, and the item stays in the
session history, so the conversation breaks permanently.

This shim rewrites such items into the equivalent user message - the format Codex
used before that change - and forwards everything else unchanged.

Usage:
    python responses-shim.py
    python responses-shim.py --port 19100 --upstream api.deepseek.com
"""

import argparse
import http.client
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None


def output_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "body"):
            if isinstance(value.get(key), str):
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def rewrite_payload(payload: dict) -> tuple[dict, int]:
    items = payload.get("input")
    if not isinstance(items, list):
        return payload, 0
    fixed, changed = [], 0
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and not item.get("call_id")
        ):
            fixed.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": output_text(item.get("output"))}],
                }
            )
            changed += 1
        else:
            fixed.append(item)
    if changed:
        payload = dict(payload)
        payload["input"] = fixed
    return payload, changed


class Shim(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "responses-shim"

    def log_message(self, fmt, *args):
        if ARGS.verbose and sys.stdout is not None:
            print(fmt % args, file=sys.stderr)

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if method == "POST" and raw:
            try:
                payload, changed = rewrite_payload(json.loads(raw.decode("utf-8")))
                if changed:
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if ARGS.verbose:
                        print(f"rewrote {changed} orphan tool output(s) -> user message", file=sys.stderr)
            except (ValueError, UnicodeDecodeError):
                pass

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in ("host", "content-length", "connection", "accept-encoding")
        }
        headers["Content-Length"] = str(len(raw))
        headers["Accept-Encoding"] = "identity"

        try:
            conn = http.client.HTTPSConnection(ARGS.upstream, timeout=1800)
            conn.request(method, self.path, body=raw or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": {"message": f"shim: upstream error {exc!r}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return

        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in ("transfer-encoding", "content-length", "connection", "keep-alive"):
                continue
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
            self.close_connection = True

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19100)
    parser.add_argument("--upstream", default="api.deepseek.com")
    parser.add_argument("--verbose", action="store_true")
    ARGS = parser.parse_args()
    server = ThreadingHTTPServer((ARGS.host, ARGS.port), Shim)
    server.daemon_threads = True
    if sys.stdout is not None:
        print(f"shim on http://{ARGS.host}:{ARGS.port}/ -> https://{ARGS.upstream}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
