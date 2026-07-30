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
:root{color-scheme:dark;--bg:#070a12;--panel:#111a2a;--line:#2d3c58;--text:#f8fbff;--muted:#9aa9c2;--accent:#8b7cff;--hot:#aa9cff;--blue:#6ee7ff;--green:#55f7b0;--red:#ff6b8b}*{box-sizing:border-box}body{margin:0;overflow-x:hidden;background:radial-gradient(circle at 82% -8%,#313067 0,transparent 38%),radial-gradient(circle at -8% 58%,#123247 0,transparent 34%),var(--bg);font:15px system-ui;color:var(--text);min-height:100vh}main{width:100%;max-width:760px;margin:auto;padding:24px}.hero{display:flex;justify-content:space-between;gap:16px;align-items:start}.hero>*{min-width:0}.badge{padding:9px 14px;border:1px solid #4b607e;border-radius:99px;color:var(--green);background:rgba(17,26,42,.72);box-shadow:inset 0 1px #ffffff20,0 12px 32px #0005;backdrop-filter:blur(20px) saturate(125%);animation:breathe 2.8s ease-in-out infinite}h1{font-size:30px;letter-spacing:-.03em;margin:10px 0 4px;overflow-wrap:anywhere}.muted{color:var(--muted)}button{border:1px solid #40516f;border-radius:18px;padding:11px 14px;background:linear-gradient(180deg,#23314a,#172238);box-shadow:inset 0 1px #ffffff1f,0 10px 24px #0004;color:var(--text);font-weight:750;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease,background .18s ease}button:hover{transform:translateY(-2px);border-color:#7797c7;box-shadow:inset 0 1px #ffffff2b,0 16px 30px #0006}button:active{transform:translateY(0) scale(.97)}.card{width:100%;margin-top:20px;padding:20px;background:linear-gradient(145deg,rgba(24,36,58,.82),rgba(13,21,35,.88));border:1px solid #3b4d6c;border-radius:28px;box-shadow:inset 0 1px #ffffff22,0 22px 60px #0007;backdrop-filter:blur(28px) saturate(120%);animation:rise .55s cubic-bezier(.2,.8,.2,1) both}.card:nth-of-type(2){animation-delay:.08s}.card:nth-of-type(3){animation-delay:.16s}.progress{height:15px;background:#0b1220;border-radius:99px;overflow:hidden;margin:16px 0;box-shadow:inset 0 2px 8px #0008}.bar{height:100%;width:0;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--blue),var(--hot),var(--accent));background-size:220% 100%;animation:flow 2s linear infinite;transition:width .45s cubic-bezier(.2,.8,.2,1)}.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:11px}.actions button{padding:15px 10px}.actions button.primary{color:#080b13;background:linear-gradient(135deg,var(--accent),var(--hot))}.actions button.danger{background:linear-gradient(180deg,#56273a,#3a1c2a)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.metric{padding:14px;border:1px solid #293953;border-radius:19px;background:rgba(9,16,29,.72);box-shadow:inset 0 1px #ffffff12}.metric b{font-size:21px;display:block;margin-top:4px}img{display:block;width:100%;max-height:420px;object-fit:contain;border-radius:20px;background:#0b1220;box-shadow:inset 0 1px #ffffff12}img:not([src]){display:none}@keyframes rise{from{opacity:0;transform:translateY(18px) scale(.985)}to{opacity:1;transform:none}}@keyframes flow{to{background-position:-220% 0}}@keyframes breathe{50%{box-shadow:inset 0 1px #ffffff28,0 12px 38px #55f7b025}}@media(max-width:540px){main{padding:17px}.hero{display:block}.badge{display:inline-block;margin-top:13px}.actions{grid-template-columns:1fr}.grid{grid-template-columns:1fr 1fr}.card{border-radius:24px}}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
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
