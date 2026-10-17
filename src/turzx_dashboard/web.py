# SPDX-License-Identifier: GPL-3.0-or-later
"""Local browser preview using the same frames as the physical screen."""

import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PAGE = b"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI-Dashboard live preview</title>
<style>
  :root { color-scheme: light; background: #f5efe0; color: #1d2527;
          font-family: ui-monospace, monospace; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
  main { width: 100%; max-width: 1920px; }
  img { display: block; width: 100%; height: auto; aspect-ratio: 4; }
  footer { padding: 20px 24px; display: flex; align-items: center; gap: 16px; }
  button { font: inherit; color: inherit; background: transparent; border: 1px solid;
           border-radius: 4px; min-height: 44px; padding: 8px 16px; cursor: pointer; }
  button:focus-visible { outline: 3px solid #8967bd; outline-offset: 4px; }
  span { font-size: 12px; }
</style>
<main>
  <img id="screen" src="/frame.png" width="1920" height="480"
       alt="AI-Dashboard: account balance, call telemetry, and Codex session status">
  <footer>
    <button id="toggle" type="button" aria-pressed="false">Pause animation</button>
    <span id="status" role="status">Live preview</span>
  </footer>
</main>
<script>
const screen = document.getElementById('screen');
const toggle = document.getElementById('toggle');
const status = document.getElementById('status');
const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
let paused = motion.matches;
let retry;
function update() {
  clearTimeout(retry);
  const running = !paused && !document.hidden;
  screen.src = (running ? '/stream.mjpg?' : '/frame.png?') + Date.now();
  toggle.textContent = paused ? 'Resume animation' : 'Pause animation';
  toggle.setAttribute('aria-pressed', String(paused));
  status.textContent = running ? 'Live preview' : 'Paused';
}
toggle.addEventListener('click', () => { paused = !paused; update(); });
document.addEventListener('visibilitychange', update);
motion.addEventListener('change', () => { paused = motion.matches; update(); });
screen.addEventListener('error', () => {
  status.textContent = 'Reconnecting...';
  retry = setTimeout(update, 1000);
});
update();
</script>
</html>
"""


class BrowserPreview:
    """One latest-frame buffer shared by all viewers; slow viewers skip old frames."""

    def __init__(self, host="127.0.0.1", port=5678):
        self.condition = threading.Condition()
        self.frame = None
        self.jpeg = None
        self.sequence = 0
        self.closed = False
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                try:
                    if path == "/":
                        self.respond(PAGE, "text/html; charset=utf-8")
                    elif path in ("/frame.png", "/screencap.png"):
                        frame = preview.latest_frame()
                        if frame is None:
                            self.respond(b"Waiting for the first frame", "text/plain", 503)
                            return
                        buffer = io.BytesIO()
                        frame.save(buffer, format="PNG")
                        self.respond(buffer.getvalue(), "image/png")
                    elif path == "/stream.mjpg":
                        self.stream()
                    else:
                        self.respond(b"Not found", "text/plain", 404)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass  # browser navigation, pause, or tab closure

            def respond(self, body, mime, status=200):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def stream(self):
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                sequence = -1
                while True:
                    with preview.condition:
                        preview.condition.wait_for(
                            lambda: preview.closed or preview.sequence != sequence, timeout=1)
                        if preview.closed:
                            return
                        jpeg, current = preview.jpeg, preview.sequence
                    if jpeg is None or current == sequence:
                        continue
                    sequence = current
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    self.wfile.flush()

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .1}, daemon=True,
                                       name="browser-preview")

    @property
    def port(self):
        return self.server.server_port

    def publish(self, frame):
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=92, subsampling=0)
        with self.condition:
            self.frame = frame.copy()
            self.jpeg = buffer.getvalue()
            self.sequence += 1
            self.condition.notify_all()

    def latest_frame(self):
        with self.condition:
            return self.frame  # published images are never mutated

    def __enter__(self):
        self.thread.start()
        log.info("Live preview: http://localhost:%d/", self.port)
        return self

    def __exit__(self, *_):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
