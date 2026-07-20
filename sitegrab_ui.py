#!/usr/bin/env python3
"""sitegrab_ui — a point-and-click web interface for sitegrab.

Run it, and a page opens in your browser: paste a URL, hit Download,
watch the progress log, then browse the offline copy or grab it as a ZIP.

    python3 sitegrab_ui.py
    python3 sitegrab_ui.py --port 9000

Standard library only, like sitegrab itself. Downloads are saved into
./grabs/ next to this script.

Hosted mode (SITEGRAB_HOSTED=1) is for running this as a public service:
binds to 0.0.0.0, honors the PORT env var, caps page counts, and deletes
finished grabs after half an hour.
"""

import argparse
import io
import json
import mimetypes
import os
import secrets
import shutil
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sitegrab

GRABS_DIR = Path(__file__).resolve().parent / "grabs"

HOSTED = os.environ.get("SITEGRAB_HOSTED") == "1"
MAX_PAGES_CAP = 30 if HOSTED else 10000
MAX_DEPTH_CAP = 3 if HOSTED else 50
CONCURRENT_JOBS = 2
JOB_TTL_SECONDS = 30 * 60  # hosted mode: purge grabs after this long

jobs_lock = threading.Lock()
jobs = {}  # job id -> dict(running, done, error, log, entry, out_dir, domain, created)


class UIGrabber(sitegrab.SiteGrabber):
    def __init__(self, job, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job = job

    def log(self, msg):
        with jobs_lock:
            self.job["log"].append(msg)


def run_grab(job, url, max_pages, depth, render):
    browser = None
    if render:
        browser = sitegrab.find_browser()
        if not browser:
            finish(job, error="Render mode needs Chrome, Chromium, Edge or "
                              "Brave installed — none found.")
            return

    url = url if "://" in url else "https://" + url
    try:
        urls = sitegrab.expand_pattern(url)
    except ValueError as e:
        finish(job, error=str(e))
        return
    if len(urls) > 1:
        # a [N-M] range means "get exactly these pages": don't follow links,
        # and let the page budget cover the whole range (hosted cap still wins)
        depth = 0
        max_pages = min(max(max_pages, len(urls)), MAX_PAGES_CAP)
    domain = urllib.parse.urlsplit(url).netloc.replace(":", "_")
    out_dir = GRABS_DIR / job["id"]

    grabber = UIGrabber(job, urls, out_dir, max_pages, depth, delay=0.2,
                        browser=browser)
    try:
        grabber.crawl()
        start = grabber.canonicalize(urls[0])
        entry = grabber.local_paths.get(start)
        with jobs_lock:
            job["domain"] = domain
            job["out_dir"] = str(out_dir)
            if grabber.pages_saved == 0:
                job["error"] = "Nothing downloaded — check the address and the log."
            elif entry:
                job["entry"] = f"grabs/{job['id']}/" + entry.as_posix()
        finish(job)
    except SystemExit as e:
        finish(job, error=str(e))
    except Exception as e:
        finish(job, error=f"{type(e).__name__}: {e}")


def finish(job, error=None):
    with jobs_lock:
        if error:
            job["error"] = error
        job["running"] = False
        job["done"] = True


def purge_old_jobs():
    """Hosted mode: forget grabs after JOB_TTL_SECONDS so disk stays small."""
    while True:
        time.sleep(300)
        cutoff = time.monotonic() - JOB_TTL_SECONDS
        with jobs_lock:
            stale = [j for j in jobs.values()
                     if j["done"] and j["created"] < cutoff]
            for job in stale:
                del jobs[job["id"]]
        for job in stale:
            shutil.rmtree(GRABS_DIR / job["id"], ignore_errors=True)


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
  footer { margin-top: 20px; font-size: 12.5px; color: var(--muted); text-align: center; }
  footer a { color: var(--muted); }
</style>
</head>
<body>
<main>
  <h1>sitegrab<span class="dot">.</span></h1>
  <p class="tag">Download a whole website and read it offline.</p>
  <div class="card">
    <label for="url">Website address</label>
    <input type="text" id="url" autofocus spellcheck="false"
           placeholder="https://example.com &mdash; or a range: books.com/epk/[1-200]">
    <div class="row">
      <div class="field">
        <label for="pages">Max pages</label>
        <input type="number" id="pages" value="__DEFAULT_PAGES__" min="1"
               max="__MAX_PAGES__">
      </div>
      <div class="field">
        <label for="depth">Link depth</label>
        <input type="number" id="depth" value="3" min="0" max="__MAX_DEPTH__">
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
  <footer>Only mirror sites you're allowed to copy. __FOOTER_NOTE__
  &middot; <a href="https://github.com/IAmAnEmpire/sitegrab">source</a></footer>
</main>
<script>
const $ = id => document.getElementById(id);
let timer = null, jobId = null;

$('go').addEventListener('click', start);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') start(); });

async function start() {
  const url = $('url').value.trim();
  if (!url) { $('url').focus(); return; }
  $('go').disabled = true; $('go').textContent = 'Downloading…';
  $('result').style.display = 'none';
  $('log').style.display = 'block'; $('log').textContent = '';
  const resp = await fetch('start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url,
      maxPages: +$('pages').value || 30,
      depth: +$('depth').value || 3,
      render: $('render').checked,
    }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    showResult('bad', data.error || 'Could not start.');
    $('go').disabled = false; $('go').textContent = 'Download site';
    return;
  }
  jobId = data.job;
  timer = setInterval(poll, 700);
}

async function poll() {
  const s = await (await fetch('status?job=' + jobId)).json();
  const log = $('log');
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.textContent = s.log.join('\\n');
  if (stick) log.scrollTop = log.scrollHeight;
  if (!s.done) return;
  clearInterval(timer);
  $('go').disabled = false; $('go').textContent = 'Download site';
  if (s.error) {
    showResult('bad', s.error);
  } else {
    showResult('ok',
      'Done! <a href="' + s.entry + '" target="_blank">Browse the offline ' +
      'copy</a> &nbsp;or&nbsp; <a href="zip?job=' + jobId + '">download it ' +
      'as a ZIP</a>.');
  }
}

function showResult(kind, html) {
  const r = $('result');
  r.style.display = 'block';
  r.className = kind;
  r.innerHTML = html;
}
</script>
</body>
</html>
"""


def render_index():
    return (PAGE
            .replace("__DEFAULT_PAGES__", "30" if HOSTED else "100")
            .replace("__MAX_PAGES__", str(MAX_PAGES_CAP))
            .replace("__MAX_DEPTH__", str(MAX_DEPTH_CAP))
            .replace("__FOOTER_NOTE__",
                     "Grabs are limited in size and deleted after 30 minutes."
                     if HOSTED else
                     "Files land in the <code>grabs/</code> folder."))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def job_from_query(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        job_id = (q.get("job") or [""])[0]
        with jobs_lock:
            return jobs.get(job_id)

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/":
            self.send(200, render_index().encode())
        elif path == "/status":
            job = self.job_from_query()
            if not job:
                self.send(404, b'{"error": "unknown job"}', "application/json")
                return
            with jobs_lock:
                body = json.dumps({
                    "done": job["done"], "error": job["error"],
                    "log": job["log"][-500:], "entry": job["entry"],
                }).encode()
            self.send(200, body, "application/json")
        elif path == "/zip":
            self.serve_zip()
        elif path.startswith("/grabs/"):
            self.serve_grab(path)
        else:
            self.send(404, b"not found", "text/plain")

    def serve_zip(self):
        job = self.job_from_query()
        if not job or not job["done"] or not job["out_dir"]:
            self.send(404, b"unknown job", "text/plain")
            return
        root = Path(job["out_dir"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(root))
        name = (job["domain"] or "site") + ".zip"
        self.send(200, buf.getvalue(), "application/zip",
                  {"Content-Disposition": f'attachment; filename="{name}"'})

    def serve_grab(self, path):
        target = (GRABS_DIR / path[len("/grabs/"):]).resolve()
        if not str(target).startswith(str(GRABS_DIR.resolve()) + os.sep):
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
        with jobs_lock:
            active = sum(1 for j in jobs.values() if j["running"])
        if active >= CONCURRENT_JOBS:
            self.send(429, b'{"error": "Busy right now - try again in a minute."}',
                      "application/json")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
            url = str(req["url"])
            max_pages = max(1, min(int(req.get("maxPages", 30)), MAX_PAGES_CAP))
            depth = max(0, min(int(req.get("depth", 3)), MAX_DEPTH_CAP))
            render = bool(req.get("render", False))
        except (ValueError, KeyError, json.JSONDecodeError):
            self.send(400, b'{"error": "bad request"}', "application/json")
            return
        job = {
            "id": secrets.token_hex(8), "running": True, "done": False,
            "error": None, "log": [], "entry": None, "out_dir": None,
            "domain": None, "created": time.monotonic(),
        }
        with jobs_lock:
            jobs[job["id"]] = job
        threading.Thread(target=run_grab,
                         args=(job, url, max_pages, depth, render),
                         daemon=True).start()
        self.send(200, json.dumps({"job": job["id"]}).encode(),
                  "application/json")


def main():
    parser = argparse.ArgumentParser(prog="sitegrab_ui",
                                     description="Web UI for sitegrab.")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 8737)))
    parser.add_argument("--no-open", action="store_true",
                        help="don't auto-open the browser")
    args = parser.parse_args()

    if HOSTED:
        threading.Thread(target=purge_old_jobs, daemon=True).start()

    host = "0.0.0.0" if HOSTED else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"sitegrab UI running at {url}  (Ctrl-C to stop)")
    if not args.no_open and not HOSTED:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
