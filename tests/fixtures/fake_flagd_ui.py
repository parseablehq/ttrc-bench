"""A stand-in for flagd-ui's flag API, for capture tests without Docker: GET /api/read returns the flag config,
POST /api/write {"data": config} replaces it, as flagd-ui 3.1.0 does. Every write is appended to a log file. With
`persist`, each write also replaces flagd.json, so a restarted stand-in serves the flags it had, as the demo's
mounted flag file does.

  python tests/fixtures/fake_flagd_ui.py <port> <flagd.json> <writes.jsonl> [persist]
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

port, config_path, log_path = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
persist = sys.argv[4:] == ["persist"]
config = json.loads(config_path.read_text())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/api/read":
            return self.reply(404, b"{}")
        self.reply(200, json.dumps(config).encode())

    def do_POST(self) -> None:
        global config
        if self.path != "/api/write":
            return self.reply(404, b"{}")
        config = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["data"]
        if persist:
            config_path.write_text(json.dumps(config))
        variants = {k: v["defaultVariant"] for k, v in config["flags"].items()}
        with log_path.open("a") as f:
            f.write(json.dumps({"at_ns": time.time_ns(), "variants": variants}) + "\n")
        self.reply(200, b'{"ok":true}')


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
