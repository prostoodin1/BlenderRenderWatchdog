"""Responsive, token-protected render dashboard for phones on the local network."""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from network_render import lan_address


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070814"><link rel="manifest" href="/manifest.json"><title>Render Watchdog</title>
<style>
:root{color-scheme:dark;--bg:#070814;--panel:#11192a;--line:#2b3a59;--text:#f8fbff;--muted:#98a7c2;--accent:#7c5cff;--green:#55f7b0;--red:#ff5f7e}*{box-sizing:border-box}body{margin:0;overflow-x:hidden;background:radial-gradient(circle at 80% 0,#20204b 0,var(--bg) 38%);font:15px system-ui;color:var(--text)}main{width:100%;max-width:760px;margin:auto;padding:22px}.hero{display:flex;justify-content:space-between;gap:16px;align-items:start}.hero>*{min-width:0}.badge{padding:8px 12px;border:1px solid var(--line);border-radius:99px;color:var(--green);background:#101827}h1{font-size:28px;margin:10px 0 4px;overflow-wrap:anywhere}.muted{color:var(--muted)}button{border:0;border-radius:10px;padding:10px;background:#1b2942;color:var(--text);font-weight:700}.card{width:100%;margin-top:18px;padding:18px;background:rgba(17,25,42,.92);border:1px solid var(--line);border-radius:18px;box-shadow:0 18px 50px #0006}.progress{height:14px;background:#080d19;border-radius:99px;overflow:hidden;margin:15px 0}.bar{height:100%;width:0;background:linear-gradient(90deg,var(--accent),#6ae6ff);transition:width .35s}.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.actions button{border-radius:13px;padding:14px 10px;font-weight:750}.actions button.primary{background:var(--accent)}.actions button.danger{background:#481b2a}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.metric{padding:12px;border-radius:13px;background:#0b1220}.metric b{font-size:20px;display:block;margin-top:4px}img{display:block;width:100%;max-height:420px;object-fit:contain;border-radius:13px;background:#080d19}img:not([src]){display:none}@media(max-width:540px){main{padding:16px}.hero{display:block}.badge{display:inline-block}.actions{grid-template-columns:1fr}.grid{grid-template-columns:1fr 1fr}}
</style></head><body><main><div class="hero"><div><div class="muted">BLENDER RENDER WATCHDOG 2.0</div><h1 id="project">Waiting for render</h1><div id="detail" class="muted">Connecting…</div></div><div id="status" class="badge">OFFLINE</div></div>
<section class="card"><div class="grid"><div class="metric"><span class="muted">Progress</span><b id="percent">0%</b></div><div class="metric"><span class="muted">Workers</span><b id="workers">0</b></div></div><div class="progress"><div id="bar" class="bar"></div></div><div class="actions"><button class="primary" onclick="action('pause')">Pause / Resume</button><button class="danger" onclick="action('stop')">Stop</button><button onclick="action('shutdown')">Shutdown after finish</button></div></section>
<section class="card"><div class="hero"><h2 style="margin:0 0 12px">Latest frame</h2><button onclick="notifications()">Enable notifications</button></div><img id="preview" alt="Latest rendered frame"></section>
<section class="card"><h2 style="margin-top:0">Queue</h2><div id="queue" class="muted">No queued projects</div></section></main>
<script>
const queryToken=new URLSearchParams(location.search).get('token')||'';if(queryToken)localStorage.setItem('watchdog-token',queryToken);const token=queryToken||localStorage.getItem('watchdog-token')||'';let lastStatus='';
async function action(name){const r=await fetch('/api/action?token='+encodeURIComponent(token),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:name})});const d=await r.json();if(!d.ok)alert(d.error||'Command failed')}
async function refresh(){try{const r=await fetch('/api/state?token='+encodeURIComponent(token),{cache:'no-store'});const d=await r.json();if(!d.ok)throw Error(d.error);document.querySelector('#project').textContent=d.project||'Waiting for render';document.querySelector('#detail').textContent=d.detail||'';document.querySelector('#status').textContent=(d.status||'ready').toUpperCase();const p=Number(d.progress||0);document.querySelector('#percent').textContent=Math.round(p)+'%';document.querySelector('#bar').style.width=p+'%';document.querySelector('#workers').textContent=d.workers||0;document.querySelector('#queue').textContent=d.queue||'No queued projects';if(d.preview){document.querySelector('#preview').src='/preview?token='+encodeURIComponent(token)+'&v='+Date.now()}if(lastStatus&&lastStatus!==d.status&&Notification.permission==='granted')new Notification('Blender Render Watchdog',{body:(d.project||'Render')+': '+d.status});lastStatus=d.status}catch(e){document.querySelector('#status').textContent='OFFLINE'}}
function notifications(){Notification.requestPermission()}setInterval(refresh,2000);refresh();
</script></body></html>'''


class MobileDashboardServer:
    def __init__(
        self,
        state_provider: Callable[[], dict[str, object]],
        action_handler: Callable[[str], tuple[bool, str]],
        preview_provider: Callable[[], Path | None] | None = None,
        bind_host: str = "0.0.0.0",
        port: int = 0,
        advertised_host: str | None = None,
        token: str | None = None,
    ) -> None:
        self.state_provider = state_provider
        self.action_handler = action_handler
        self.preview_provider = preview_provider
        self.bind_host = bind_host
        self.port = port
        self.advertised_host = advertised_host or lan_address()
        self.token = token or secrets.token_urlsafe(16)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def public_url(self) -> str:
        if not self.port:
            raise RuntimeError("Dashboard has not started")
        return f"http://{self.advertised_host}:{self.port}/?token={urllib.parse.quote(self.token)}"

    def start(self) -> str:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BlenderRenderWatchdogMobile/2.0"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _token_ok(self) -> bool:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return query.get("token", [""])[0] == dashboard.token

            def _send(self, payload: bytes, content_type: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, data: dict[str, object], status: int = 200) -> None:
                self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

            def do_GET(self) -> None:  # noqa: N802
                route = urllib.parse.urlparse(self.path).path
                if route == "/":
                    self._send(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if route == "/manifest.json":
                    self._json({"name": "Blender Render Watchdog", "short_name": "Watchdog", "display": "standalone", "start_url": "/", "theme_color": "#070814", "background_color": "#070814"})
                    return
                if not self._token_ok():
                    self._json({"ok": False, "error": "Unauthorized"}, 401)
                    return
                if route == "/api/state":
                    state = dict(dashboard.state_provider())
                    state["ok"] = True
                    self._json(state)
                    return
                if route == "/preview":
                    preview = dashboard.preview_provider() if dashboard.preview_provider else None
                    if preview is None or not preview.exists() or not preview.is_file():
                        self._send(b"", "image/png", 404)
                        return
                    self._send(preview.read_bytes(), mimetypes.guess_type(preview.name)[0] or "application/octet-stream")
                    return
                self._json({"ok": False, "error": "Not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                if not self._token_ok():
                    self._json({"ok": False, "error": "Unauthorized"}, 401)
                    return
                route = urllib.parse.urlparse(self.path).path
                if route != "/api/action":
                    self._json({"ok": False, "error": "Not found"}, 404)
                    return
                length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    action = str(data.get("action") or "") if isinstance(data, dict) else ""
                    ok, message = dashboard.action_handler(action)
                    self._json({"ok": ok, "message": message}, 200 if ok else 400)
                except (ValueError, json.JSONDecodeError) as error:
                    self._json({"ok": False, "error": str(error)}, 400)

        self._server = ThreadingHTTPServer((self.bind_host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="mobile-dashboard", daemon=True)
        self._thread.start()
        return self.public_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None
