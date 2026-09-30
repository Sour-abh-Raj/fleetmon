"""HTTP transport server.

``fleetmon --serve`` exposes one endpoint, ``/snapshot``, which returns the
machine's current state as JSON. That is the only way to see a machine behind
NAT without a tunnel, so it is built to be harmless:

* **Bound to loopback by default.** Exposing a process inventory on 0.0.0.0 is
  not something a dashboard should do on its own initiative.
* **Read-only.** Two GET routes and nothing else.
* **Self-collecting.** A fresh snapshot per request, because the alternative --
  caching -- is exactly how a fleet view ends up showing stale state that looks
  live.

``ThreadingHTTPServer`` rather than the single-threaded default, because the
whole point is several machines polling at once and a serial server would make
one slow probe delay everyone else's frame.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .model import SCHEMA


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None          # set by serve()
    server_version = "fleetmon"

    def log_message(self, fmt, *args):
        pass                      # a poll per second would flood any log

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send(200, INDEX.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/snapshot":
            from .collectors import collect
            try:
                snap = collect(self.cfg)
            except Exception as exc:
                self._send(500, json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}).encode(),
                    "application/json")
                return
            self._send(200, snap.__json__().encode("utf-8"), "application/json")
            return
        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


INDEX = """<!doctype html><meta charset=utf-8>
<title>fleetmon</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font:14px ui-monospace,monospace;
      margin:2rem auto;max-width:44rem;line-height:1.6}
 h1{color:#d29922;font-size:1.3rem} code{color:#79c0ff}
 a{color:#58a6ff} pre{background:#161b22;padding:1rem;border-radius:6px;
      overflow:auto;border:1px solid #30363d}
 .ok{color:#3fb950}
</style>
<h1>fleetmon</h1>
<p>This machine is publishing its snapshot at
   <a href="/snapshot"><code>/snapshot</code></a>.</p>
<p>Point another fleetmon at it:</p>
<pre>fleetmon --peer http://this-host:%(port)d/</pre>
<p>Schema <code>%(schema)d</code>. Nothing is written to disk and no inbound
   connection is accepted other than these two GET routes.</p>
<p class=ok>OK</p>
"""


def serve(cfg: Config, port: int = 8766, host: str = "127.0.0.1") -> int:
    Handler.cfg = cfg
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"fleetmon: cannot bind {host}:{port} -- {exc}",
              file=sys.stderr)
        return 1
    srv.daemon_threads = True
    print(f"fleetmon serving on http://{host}:{port}/  (snapshot at /snapshot)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
