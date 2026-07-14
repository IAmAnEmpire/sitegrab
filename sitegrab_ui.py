#!/usr/bin/env python3
"""sitegrab_ui — a point-and-click web interface for sitegrab.

Run it, and a page opens in your browser: paste a URL, hit Download,
watch the progress log, then browse the offline copy right there.

    python3 sitegrab_ui.py
    python3 sitegrab_ui.py --port 9000

Standard library only, like sitegrab itself. Downloads are saved into
./grabs/<domain>/ next to this script.
"""

import argparse
import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sitegrab

GRABS_DIR = Path(__file__).resolve().parent / "grabs"

state_lock = threading.Lock()
state = {
    "running": False,
    "done": False,
    "error": None,
    "log": [],
    "entry": None,   # URL path of the saved start page, e.g. /grabs/x.com/index.html
    "out_dir": None,
}


class UIGrabber(sitegrab.SiteGrabber):
    def log(self, msg):
        with state_lock:
            state["log"].append(msg)


def run_grab(url, max_pages, depth, render):
    browser = None
    if render:
        browser = sitegrab.find_browser()
        if not browser:
            with state_lock:
                state["error"] = ("Render mode needs Chrome, Chromium, Edge or "
                                  "Brave installed — none found.")
                state["running"] = False
                state["done"] = True
            return

    url = url if "://" in url else "https://" + url
    domain = urllib.parse.urlsplit(url).netloc.replace(":", "_")
    out_dir = GRABS_DIR / domain

    grabber = UIGrabber(url, out_dir, max_pages, depth, delay=0.2,
                        browser=browser)
    try:
        grabber.crawl()
        start = grabber.canonicalize(url)
        entry = grabber.local_paths.get(start)
        with state_lock:
            if grabber.pages_saved == 0:
                state["error"] = "Nothing downloaded — check the URL and the log."
            elif entry:
                state["entry"] = "/grabs/" + domain + "/" + entry.as_posix()
            state["out_dir"] = str(out_dir)
    except SystemExit as e:
        with state_lock:
            state["error"] = str(e)
    except Exception as e:
        with state_lock:
            state["error"] = f"{type(e).__name__}: {e}"
    finally:
        with state_lock:
            state["running"] = False
            state["done"] = True


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sitegrab</title>
<style>
  :root {
    --bg: #f4f1ea; --card: #fffdf8; --ink: #1a1815; --muted: #6f6a60;
    --accent: #0f6b4f; --accent-ink: #fffdf8; --line: #e2ddd2;
    --log-bg: #14120f; --log-ink: #d8d3c8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.5 -apple-system, "Segoe UI", sans-serif;
    display: flex; justify-content: center; padding: 48px 20px;
  }
  main { width: 100%; max-width: 640px; }
  h1 { font-size: 28px; margin: 0 0 4px; letter-spacing: -0.02em; }
  h1 .dot { color: var(--accent); }
  p.tag { margin: 0 0 28px; color: var(--muted); }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 24px;
  }
  label { display: block; font-size: 13px; font-weight: 600; margin: 0 0 6px; }
  input[type=text], input[type=number] {
    width: 100%; padding: 10px 12px; font: inherit; color: inherit;
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  }
  input:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: transparent; }
  .row { display: flex; gap: 14px; margin-top: 16px; align-items: end; flex-wrap: wrap; }
  .row .field { flex: 1; min-width: 110px; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 18px;
           font-size: 14px; color: var(--muted); }
  .check input { accent-color: var(--accent); width: 16px; height: 16px; }
  button {
    margin-top: 22px; width: 100%; padding: 12px; font: inherit; font-weight: 650;
    color: var(--accent-ink); background: var(--accent); border: 0;
    border-radius: 8px; cursor: pointer;
  }
  button:hover { filter: brightness(1.08); }
  button:disabled { opacity: .55; cursor: default; }
  #log {
    display: none; margin-top: 20px; padding: 14px 16px; height: 240px;
    overflow-y: auto; background: var(--log-bg); color: var(--log-ink);
    border-radius: 10px; font: 12.5px/1.6 ui-monospace, Menlo, monospace;
    white-space: pre-wrap; word-break: break-all;
  }
  #result { display: none; margin-top: 20px; padding: 16px;
    border: 1px solid var(--line); border-radius: 10px; font-size: 14px; }
  #result.ok { background: #e9f3ee; border-color: #bcd9cc; }
  #result.bad { background: #f7e9e6; border-color: #e3c1ba; }
  #result a { color: var(--accent); font-weight: 650; }
  #result code { background: rgba(0,0,0,.06); padding: 1px 5px; border-radius: 4px; }
  footer { margin-top: 20px; font-size: 12.5px; color: var(--muted); text-align: center; }
</style>
</head>
<body>
<main>
  <h1>sitegrab<span class="dot">.</span></h1>
  <p class="tag">Download a whole website and read it offline.</p>
  <div class="card">
    <label for="url">Website address</label>
    <input type="text" id="url" placeholder="https://example.com" autofocus
           spellcheck="false">
    <div class="row">
      <div class="field">
        <label for="pages">Max pages</label>
        <input type="number" id="pages" value="100" min="1" max="10000">
      </div>
      <div class="field">
        <label for="depth">Link depth</label>
        <input type="number" id="depth" value="5" min="0" max="50">
      </div>
    </div>
    <label class="check">
      <input type="checkbox" id="render">
      Render JavaScript first (for app-style sites; slower)
    </label>
    <button id="go">Download site</button>
    <div id="log"></div>
    <div id="result"></div>
  </div>
  <footer>Only mirror sites you're allowed to copy. Files land in the
  <code style="font-size:12px">grabs/</code> folder.</footer>
</main>
<script>
const $ = id => document.getElementById(id);
let timer = null;

$('go').addEventListener('click', start);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') start(); });

async function start() {
  const url = $('url').value.trim();
  if (!url) { $('url').focus(); return; }
  $('go').disabled = true; $('go').textContent = 'Downloading…';
  $('result').style.display = 'none';
  $('log').style.display = 'block'; $('log').textContent = '';
  await fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url,
      maxPages: +$('pages').value || 100,
      depth: +$('depth').value || 5,
      render: $('render').checked,
    }),
  });
  timer = setInterval(poll, 700);
}

async function poll() {
  const s = await (await fetch('/status')).json();
  const log = $('log');
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.textContent = s.log.join('\\n');
  if (stick) log.scrollTop = log.scrollHeight;
  if (!s.done) return;
  clearInterval(timer);
  $('go').disabled = false; $('go').textContent = 'Download site';
  const r = $('result');
  r.style.display = 'block';
  if (s.error) {
    r.className = 'bad';
    r.textContent = s.error;
  } else {
    r.className = 'ok';
    r.innerHTML = 'Done! <a href="' + s.entry + '" target="_blank">Browse your ' +
      'offline copy</a> — saved in <code>' + s.outDir + '</code>';
  }
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/":
            self.send(200, PAGE.encode())
        elif path == "/status":
            with state_lock:
                body = json.dumps({
                    "running": state["running"],
                    "done": state["done"],
                    "error": state["error"],
                    "log": state["log"][-500:],
                    "entry": state["entry"],
                    "outDir": state["out_dir"],
                }).encode()
            self.send(200, body, "application/json")
        elif path.startswith("/grabs/"):
            self.serve_grab(path)
        else:
            self.send(404, b"not found", "text/plain")

    def serve_grab(self, path):
        target = (GRABS_DIR / path[len("/grabs/"):]).resolve()
        if not str(target).startswith(str(GRABS_DIR.resolve())):
            self.send(403, b"forbidden", "text/plain")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send(200, target.read_bytes(), ctype)

    def do_POST(self):
        if self.path != "/start":
            self.send(404, b"not found", "text/plain")
            return
        with state_lock:
            if state["running"]:
                self.send(409, b'{"error": "already running"}', "application/json")
                return
            state.update(running=True, done=False, error=None, log=[],
                         entry=None, out_dir=None)
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
            url = str(req["url"])
            max_pages = max(1, min(int(req.get("maxPages", 100)), 10000))
            depth = max(0, min(int(req.get("depth", 5)), 50))
            render = bool(req.get("render", False))
        except (ValueError, KeyError, json.JSONDecodeError):
            with state_lock:
                state.update(running=False, done=True, error="bad request")
            self.send(400, b'{"error": "bad request"}', "application/json")
            return
        threading.Thread(target=run_grab, args=(url, max_pages, depth, render),
                         daemon=True).start()
        self.send(200, b'{"ok": true}', "application/json")


def main():
    parser = argparse.ArgumentParser(prog="sitegrab_ui",
                                     description="Web UI for sitegrab.")
    parser.add_argument("--port", type=int, default=8737)
    parser.add_argument("--no-open", action="store_true",
                        help="don't auto-open the browser")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"sitegrab UI running at {url}  (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
