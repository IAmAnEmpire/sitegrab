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


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sitegrab</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders:opsz,wght@10..72,500;10..72,700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #0e3059; --line: #d8e6f4; --faint: rgba(216,230,244,0.28);
    --pencil: #ff8a4a; --stamp: #ff4438;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: 'IBM Plex Mono', monospace; color: var(--line); overflow-x: hidden;
    background:
      linear-gradient(rgba(216,230,244,0.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(216,230,244,0.045) 1px, transparent 1px),
      linear-gradient(rgba(216,230,244,0.09) 1px, transparent 1px),
      linear-gradient(90deg, rgba(216,230,244,0.09) 1px, transparent 1px),
      radial-gradient(ellipse at 50% 40%, #124075 0%, var(--paper) 75%);
    background-size: 14px 14px, 14px 14px, 70px 70px, 70px 70px, 100% 100%;
    background-attachment: fixed;
  }
  .frame { position: fixed; inset: 16px; border: 1.5px solid var(--faint); pointer-events: none; z-index: 1; }
  .frame::after { content: ""; position: absolute; inset: 5px; border: 0.5px solid rgba(216,230,244,0.18); }
  canvas { position: fixed; inset: 0; z-index: 0; }

  .titleblock {
    position: fixed; right: 28px; bottom: 28px; z-index: 3;
    border: 1.5px solid var(--faint); background: rgba(14,48,89,0.88);
    font-size: 0.6rem; letter-spacing: 0.12em; text-transform: uppercase;
    display: grid; grid-template-columns: auto auto;
  }
  .titleblock div { padding: 7px 14px; border-top: 1px solid var(--faint); }
  .titleblock div:nth-child(-n+2) { border-top: 0; }
  .titleblock div:nth-child(odd) { border-right: 1px solid var(--faint); color: #8fa9c6; }
  .titleblock b { font-weight: 600; color: var(--line); }
  .titleblock a { color: var(--pencil); text-decoration: none; }
  @media (max-width: 760px) { .titleblock { display: none; } }

  .head { position: fixed; top: 34px; left: 44px; z-index: 3; }
  .head h1 { font-family: 'Big Shoulders', sans-serif; font-weight: 700; font-size: 1.7rem;
    letter-spacing: 0.24em; text-transform: uppercase; }
  .head .sub { font-size: 0.62rem; letter-spacing: 0.22em; text-transform: uppercase; color: #8fa9c6; margin-top: 4px; }

  .spec {
    position: fixed; z-index: 3; left: 50%; top: 50%; transform: translate(-50%, -50%);
    width: min(560px, 92vw); max-height: 86vh; overflow-y: auto;
    border: 1.5px solid var(--line); background: rgba(14,48,89,0.94);
    transition: opacity .4s, transform .5s;
  }
  body.drafting .spec, body.certified .spec { opacity: 0; pointer-events: none; transform: translate(-50%, -54%); }
  .spec .caption { border-bottom: 1.5px solid var(--line); padding: 10px 18px;
    font-size: 0.62rem; letter-spacing: 0.26em; text-transform: uppercase;
    display: flex; justify-content: space-between; color: #8fa9c6; }
  .spec .caption b { color: var(--line); font-weight: 600; }
  .spec .body { padding: 22px 22px 24px; }
  .fld label { display: block; font-size: 0.6rem; letter-spacing: 0.22em; text-transform: uppercase; color: #8fa9c6; margin-bottom: 6px; }
  .fld input[type=text], .fld input[type=number] {
    width: 100%; background: transparent; border: 0; outline: none;
    border-bottom: 1.5px dashed var(--faint); color: var(--line);
    font-family: 'IBM Plex Mono', monospace; font-size: 1rem; padding: 4px 2px 8px;
  }
  .fld input:focus { border-bottom: 1.5px solid var(--pencil); }
  .fld input::placeholder { color: #5f7ba0; }
  .grid2 { display: flex; gap: 22px; margin-top: 20px; flex-wrap: wrap; align-items: end; }
  .grid2 .fld { flex: 1; min-width: 100px; }
  .grid2 .fld input { text-align: center; color: var(--pencil); font-weight: 600; }
  .tickbox { flex: 2; min-width: 200px; display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none;
    font-size: 0.7rem; letter-spacing: 0.1em; text-transform: uppercase; color: #8fa9c6; }
  .tickbox .sq { width: 16px; height: 16px; border: 1.5px solid var(--line); position: relative; flex: none; }
  .tickbox .tb-txt { line-height: 1.7; }
  .tickbox .tb-txt b { color: var(--line); font-weight: 600; letter-spacing: 0.08em; font-size: 0.8rem; }
  .jsrow { width: 100%; margin-top: 20px; padding: 12px 14px;
    border: 1px dashed var(--faint); border-left: 3px solid var(--pencil); }
  .tickbox input { display: none; }
  .tickbox input:checked + .sq::after { content: "X"; position: absolute; inset: 0; color: var(--pencil);
    font-size: 12px; line-height: 15px; text-align: center; font-weight: 600; }
  .rangenote { margin-top: 20px; border: 1px dashed var(--faint); padding: 12px 14px; font-size: 0.7rem; line-height: 1.7; }
  .rangenote .rn-title { display: block; color: var(--line); font-weight: 600; letter-spacing: 0.06em; }
  .rangenote .rn-body { display: block; color: #8fa9c6; margin-top: 4px; }
  .rangenote b { color: var(--pencil); font-weight: 600; }
  .ftr { margin-top: 16px; font-size: 0.62rem; letter-spacing: 0.08em; color: #5f7ba0; line-height: 1.8; }
  .ftr code { color: #8fa9c6; }
  .commence {
    margin-top: 22px; width: 100%; cursor: pointer;
    font-family: 'Big Shoulders', sans-serif; font-weight: 700; font-size: 1.25rem;
    letter-spacing: 0.34em; text-transform: uppercase;
    background: transparent; color: var(--line); border: 1.5px solid var(--line);
    padding: 15px 10px 13px; transition: all .15s;
  }
  .commence:hover { background: var(--line); color: var(--paper); }
  .commence:disabled { opacity: 0.5; cursor: default; }
  .specerr { display: none; margin-top: 16px; border: 1.5px solid var(--stamp); color: #ffb3ad;
    padding: 10px 14px; font-size: 0.72rem; line-height: 1.6; }

  .readout { position: fixed; z-index: 3; left: 44px; bottom: 34px; display: none;
    font-size: 0.66rem; letter-spacing: 0.14em; text-transform: uppercase; line-height: 2.1; color: #8fa9c6; max-width: 46vw; }
  body.drafting .readout, body.certified .readout { display: block; }
  .readout b { color: var(--line); font-weight: 600; font-variant-numeric: tabular-nums; }
  .readout .now { color: var(--pencil); display: inline-block; max-width: 100%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
  .readout details { margin-top: 6px; }
  .readout summary { cursor: pointer; color: #5f7ba0; text-transform: uppercase; font-size: 0.6rem; letter-spacing: 0.18em; }
  .readout pre { margin-top: 8px; max-height: 26vh; width: min(520px, 80vw); overflow: auto;
    background: rgba(10,26,48,0.92); border: 1px solid var(--faint); padding: 10px 12px;
    font-size: 0.64rem; line-height: 1.7; text-transform: none; letter-spacing: 0; color: #b8cbe2;
    white-space: pre-wrap; word-break: break-all; }

  .cert {
    position: fixed; z-index: 4; left: 50%; top: 42%;
    transform: translate(-50%, -50%) rotate(-8deg) scale(3); opacity: 0;
    border: 4px solid var(--stamp); color: var(--stamp); border-radius: 6px;
    padding: 14px 30px 12px; text-align: center; pointer-events: none;
    font-family: 'Big Shoulders', sans-serif; font-weight: 700;
    font-size: 1.9rem; letter-spacing: 0.28em; text-transform: uppercase;
    box-shadow: inset 0 0 14px rgba(255,68,56,0.35);
    transition: transform .18s cubic-bezier(.7,0,.9,1), opacity .12s;
  }
  .cert small { display: block; font-family: 'IBM Plex Mono', monospace; font-size: 0.58rem; letter-spacing: 0.3em; margin-top: 4px; }
  body.certified .cert { opacity: 0.92; transform: translate(-50%, -50%) rotate(-8deg) scale(1); }

  .exports { position: fixed; z-index: 4; left: 50%; bottom: 90px; transform: translateX(-50%);
    display: none; gap: 14px; align-items: center; flex-wrap: wrap; justify-content: center; }
  body.certified .exports { display: flex; }
  .exports a { text-decoration: none; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.2em;
    text-transform: uppercase; padding: 14px 24px; border: 1.5px solid var(--line); color: var(--line); }
  .exports a.main { background: var(--line); color: var(--paper); }
  .exports a.main:hover { background: var(--pencil); border-color: var(--pencil); color: var(--paper); }
  .exports a:hover { border-color: var(--pencil); color: var(--pencil); }
  .exports button { background: none; border: 0; color: #8fa9c6; cursor: pointer;
    font-family: 'IBM Plex Mono', monospace; font-size: 0.62rem; letter-spacing: 0.18em; text-transform: uppercase; }
  .exports button:hover { color: var(--line); }

  .donenote { position: fixed; z-index: 4; left: 50%; bottom: 150px; transform: translateX(-50%);
    display: none; max-width: min(560px, 90vw); text-align: center;
    font-size: 0.7rem; letter-spacing: 0.08em; color: #b8cbe2; line-height: 1.8;
    background: rgba(14,48,89,0.92); border: 1px dashed var(--faint); padding: 10px 16px; }
  body.certified .donenote { display: block; }

  .mobilist { display: none; }

  @media (max-width: 700px) {
    .frame { inset: 8px; }
    .head { top: 22px; left: 22px; }
    .head h1 { font-size: 1.25rem; letter-spacing: 0.18em; }
    .head .sub { font-size: 0.55rem; }
    canvas { display: none; }
    .spec { top: 80px; transform: translate(-50%, 0); max-height: calc(100vh - 100px); }
    body.drafting .spec, body.certified .spec { transform: translate(-50%, -4%); }
    .grid2 { gap: 14px; }
    .mobilist { display: block; position: fixed; z-index: 2; top: 84px; left: 6vw; right: 6vw;
      bottom: 175px; overflow-y: auto; font-size: 0.72rem; line-height: 2.1;
      color: #b8cbe2; opacity: 0; pointer-events: none; transition: opacity .4s; }
    body.drafting .mobilist, body.certified .mobilist { opacity: 1; }
    .mobilist .pg { color: var(--line); font-weight: 600; }
    .mobilist .as { color: var(--pencil); padding-left: 16px; }
    .readout { left: 6vw; right: 6vw; bottom: 92px; max-width: none; }
    .readout pre { width: 88vw; max-height: 20vh; }
    .cert { top: auto; bottom: 215px; font-size: 1.15rem; letter-spacing: 0.2em;
      padding: 10px 18px 8px; border-width: 3px; }
    .exports { bottom: 20px; width: 92vw; gap: 8px; }
    .exports a { flex: 1 1 40%; text-align: center; padding: 13px 8px; font-size: 0.62rem; }
    .exports button { flex: 1 1 100%; padding-top: 6px; }
    .donenote { bottom: 86px; }
    .titleblock { display: none; }
  }

  /* progress panel (overrides earlier .readout) */
  .readout { left: 50%; right: auto; transform: translateX(-50%); bottom: 96px;
    width: min(600px, 92vw); max-width: none; z-index: 4;
    background: rgba(10,26,48,0.94); border: 1px solid var(--faint); padding: 16px 18px 14px; }
  .pphead { display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
    font-size: 0.72rem; letter-spacing: 0.12em; color: var(--line); }
  .pphead .now { max-width: 70%; }
  .pphead b.pct { color: var(--pencil); font-size: 1rem; }
  .ppbar { margin-top: 10px; height: 8px; border: 1px solid var(--faint); background: rgba(216,230,244,0.07); }
  .ppbar i { display: block; height: 100%; width: 0%; background: var(--pencil); transition: width .6s; }
  .ppmeta { display: flex; justify-content: space-between; margin-top: 8px;
    font-size: 0.64rem; letter-spacing: 0.12em; color: #8fa9c6; }
  .ppmeta b { color: var(--line); font-weight: 600; }
  .pptip { margin-top: 10px; font-size: 0.66rem; letter-spacing: 0.04em; text-transform: none;
    color: #8fa9c6; line-height: 1.8; }
  .ppnext { margin-top: 12px; border-top: 1px dashed var(--faint); padding-top: 10px;
    font-size: 0.66rem; letter-spacing: 0.04em; text-transform: none; color: var(--line); line-height: 1.9; }
  .ppnext b { color: var(--pencil); }
  .howto { position: fixed; z-index: 6; left: 50%; bottom: 150px; transform: translateX(-50%);
    display: none; width: min(520px, 90vw); background: rgba(14,48,89,0.97);
    border: 1.5px solid var(--line); padding: 16px 20px;
    font-size: 0.72rem; line-height: 2.1; letter-spacing: 0.04em; text-transform: none; color: var(--line); }
  .howto.show { display: block; }
  .howto b { color: var(--pencil); }
  @media (max-width: 700px) {
    .readout { bottom: 108px; }
    .howto { bottom: 170px; }
  }

  /* done state: one card, nothing stacked behind it */
  body.certified .readout, body.certified .mobilist { display: none; }
  .howto, #howOpen { display: none !important; }
  .donenote { position: fixed; z-index: 5; left: 50%; transform: translateX(-50%); bottom: 100px;
    width: min(600px, 92vw); max-width: none; text-align: left;
    background: rgba(10,26,48,0.97); border: 1.5px solid var(--line);
    padding: 18px 20px; font-size: 0.74rem; line-height: 2; letter-spacing: 0.04em; color: var(--line); }
  .donenote b { color: var(--pencil); }
  .donenote .sum { color: #8fa9c6; display: block; margin-bottom: 8px; }
  .exports { bottom: 34px; }
  @media (max-width: 700px) {
    .donenote { bottom: 96px; }
    .exports { bottom: 14px; }
    .cert { bottom: auto; top: 20%; }
  }
</style>
</head>
<body>

<div class="frame"></div>
<canvas id="draft"></canvas>

<div class="head">
  <h1>SITEGRAB</h1>
  <div class="sub">Archival copies of living websites</div>
</div>

<div class="titleblock">
  <div>Project</div><div><b id="tbProj">&mdash;</b></div>
  <div>Drawing</div><div><b>SITE PLAN &middot; SHEET 1 OF 1</b></div>
  <div>Scale</div><div><b>1 : 1 &middot; TRUE COPY</b></div>
  <div>Office</div><div><b><a href="https://douvenne.com">DOUVENNE</a> &middot; <a href="https://github.com/IAmAnEmpire/sitegrab">SOURCE</a></b></div>
</div>

<div class="spec" id="spec">
  <div class="caption"><span>Specification <b>&#8470; 001</b></span><span>works offline</span></div>
  <div class="body">
    <div class="fld">
      <label for="url">Subject website</label>
      <input type="text" id="url" spellcheck="false" autocomplete="off" autofocus placeholder="https://example.com">
    </div>
    <div class="grid2">
      <div class="fld"><label>Max pages</label><input type="number" id="pages" value="__DEFAULT_PAGES__" min="1" max="__MAX_PAGES__"></div>
      <div class="fld"><label>Link depth</label><input type="number" id="depth" value="3" min="0" max="__MAX_DEPTH__"></div>
    </div>
    <label class="tickbox jsrow"><input type="checkbox" id="render"><span class="sq"></span>
      <span class="tb-txt"><b>Render JS first</b><br>(best for web apps / complicated sites)</span></label>
    <div class="rangenote">
      <span class="rn-title">Saving numbered pages? (chapters, episodes, issues)</span>
      <span class="rn-body">Put the numbers in brackets and we'll download every one.<br>
      Example: <b>books.com/chapter/[1-200]</b> grabs chapters 1 to 200.</span>
    </div>
    <button class="commence" id="go">Download</button>
    <div class="specerr" id="specerr"></div>
    <p class="ftr">__FOOTER_NOTE__ &middot; Only mirror sites you're allowed to copy.</p>
  </div>
</div>

<div class="readout">
  <div class="pphead"><span class="now" id="roNow">preparing&hellip;</span><b class="pct" id="ppPct">0%</b></div>
  <div class="ppbar"><i id="ppBar"></i></div>
  <div class="ppmeta">
    <span>pages <b id="roP">0</b> &middot; files <b id="roF">0</b></span>
    <span>elapsed <b id="roT">0s</b></span>
  </div>
  <div class="pptip" id="ppTip">Starting up. Bigger sites can take a minute or two; every page gets saved properly.</div>
  <div class="ppnext"><b>When this finishes:</b> a ZIP downloads. Unzip it and double-click
  <b>Open-website.html</b> to view your downloaded site.</div>
  <details><summary>raw log</summary><pre id="rawlog"></pre></details>
</div>

<div class="cert" id="cert">True copy<small>certified &middot; works offline</small></div>

<div class="mobilist" id="mobilist"></div>
<div class="donenote" id="doneNote"></div>

<div class="exports">
  <a class="main" id="zipLink" href="#">Download the ZIP again</a>
  <button id="howOpen">hide / show instructions</button>
  <button id="again">&#8635; download another</button>
</div>

<div class="howto" id="howto">
  Unzip the file in your Downloads, then double-click <b>Open-website.html</b> to view your downloaded site.
</div>

<script>
const ZIPBASE = '__ZIP_BASE__';
const $ = id => document.getElementById(id);
var cv = $('draft'), cx = cv.getContext('2d');
var W, H, DPR = Math.min(devicePixelRatio || 1, 2);
function sizeCv() { W = innerWidth; H = innerHeight; cv.width = W*DPR; cv.height = H*DPR; cx.setTransform(DPR,0,0,DPR,0,0); }
sizeCv(); addEventListener('resize', sizeCv);

var LINE = '#d8e6f4', PENCIL = '#ff8a4a';
var rooms = [], pipes = [], pagesArr = [];
var MAX_ROOMS = 26, MAX_ASSETS_PER = 3;

function lastSeg(url) {
  try {
    var p = url.replace(/https?:\/\//, '').split('?')[0].split('#')[0];
    var parts = p.split('/').filter(Boolean);
    var s = parts.length > 1 ? parts[parts.length - 1] : (parts[0] || 'INDEX');
    return s.toUpperCase().slice(0, 12) || 'INDEX';
  } catch (e) { return 'PAGE'; }
}

function addPage(label) {
  var n = pagesArr.length;
  if (rooms.length > MAX_ROOMS) { pagesArr.push(pagesArr[pagesArr.length-1]); return; }
  var r;
  if (n === 0) {
    r = { x: W/2-70, y: 110, w: 140, h: 50, label: 'INDEX', kind: 'page', born: performance.now(), assets: 0 };
  } else {
    var row = 1 + Math.floor((n - 1) / 4), col = (n - 1) % 4;
    var w = Math.min(130, (W - 180) / 4);
    var x = W/2 + (col - 1.5) * (w + 42) - w/2;
    var y = 110 + row * 105;
    if (y > H - 230) { y = H - 230; x += (row % 3) * 20; }
    r = { x: x, y: y, w: w, h: 44, label: label, kind: 'page', born: performance.now(), assets: 0 };
    var parent = pagesArr[Math.floor((n - 1) / 4)] || pagesArr[0];
    pipes.push({ from: parent, to: r });
  }
  rooms.push(r); pagesArr.push(r);
}

function addAsset(label) {
  var host = pagesArr[pagesArr.length - 1];
  if (!host || host.assets >= MAX_ASSETS_PER || rooms.length > MAX_ROOMS + 30) return;
  var r = { x: Math.min(host.x + host.w + 12, W - 66), y: host.y + host.assets * 21,
            w: 44, h: 16, label: label.toUpperCase().slice(0, 5), kind: 'asset', born: performance.now() };
  host.assets++;
  rooms.push(r); pipes.push({ from: host, to: r });
}

function roomPath(r, prog) {
  var per = 2*(r.w+r.h), d = per*prog;
  cx.beginPath(); cx.moveTo(r.x, r.y);
  var seg = Math.min(d, r.w); cx.lineTo(r.x+seg, r.y); d-=seg; if (d<=0) return cx.stroke();
  seg = Math.min(d, r.h); cx.lineTo(r.x+r.w, r.y+seg); d-=seg; if (d<=0) return cx.stroke();
  seg = Math.min(d, r.w); cx.lineTo(r.x+r.w-seg, r.y+r.h); d-=seg; if (d<=0) return cx.stroke();
  seg = Math.min(d, r.h); cx.lineTo(r.x, r.y+r.h-seg); cx.stroke();
}

function draw(t) {
  cx.clearRect(0,0,W,H);
  cx.setLineDash([5,4]); cx.lineWidth = 1;
  pipes.forEach(function (p) {
    var prog = Math.min(1, (t - p.to.born) / 700);
    if (prog <= 0) return;
    cx.strokeStyle = 'rgba(216,230,244,0.4)';
    var ax, ay, bx, by;
    if (p.to.kind === 'asset') { ax = p.from.x + p.from.w; ay = p.from.y + p.from.h/2; bx = p.to.x; by = p.to.y + p.to.h/2; }
    else { ax = p.from.x + p.from.w/2; ay = p.from.y + p.from.h; bx = p.to.x + p.to.w/2; by = p.to.y; }
    var my = ay + (by-ay)/2;
    cx.beginPath(); cx.moveTo(ax, ay);
    cx.lineTo(ax, ay + (my-ay)*Math.min(1,prog*3));
    if (prog > 0.33) cx.lineTo(ax + (bx-ax)*Math.min(1,(prog-0.33)*3), my);
    if (prog > 0.66) cx.lineTo(bx, my + (by-my)*Math.min(1,(prog-0.66)*3));
    cx.stroke();
  });
  cx.setLineDash([]);
  rooms.forEach(function (r) {
    var prog = Math.min(1, (t - r.born) / 600);
    if (prog <= 0) return;
    cx.strokeStyle = r.kind === 'asset' ? PENCIL : LINE;
    cx.lineWidth = r.kind === 'asset' ? 1 : 1.6;
    roomPath(r, prog);
    if (prog >= 1) {
      cx.fillStyle = r.kind === 'asset' ? PENCIL : LINE;
      cx.font = (r.kind === 'asset' ? '9px' : '11px') + ' "IBM Plex Mono", monospace';
      cx.textAlign = 'center';
      cx.fillText(r.label, r.x + r.w/2, r.y + r.h/2 + 4);
      if (r.kind === 'page') {
        cx.strokeStyle = 'rgba(255,138,74,0.5)'; cx.lineWidth = 0.8;
        cx.beginPath(); cx.moveTo(r.x, r.y + r.h + 7); cx.lineTo(r.x + r.w, r.y + r.h + 7); cx.stroke();
        cx.beginPath(); cx.moveTo(r.x, r.y + r.h + 3); cx.lineTo(r.x, r.y + r.h + 11); cx.stroke();
        cx.beginPath(); cx.moveTo(r.x + r.w, r.y + r.h + 3); cx.lineTo(r.x + r.w, r.y + r.h + 11); cx.stroke();
      }
    }
  });
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

// ---- real crawl wiring ----
var jobId = null, timer = null, clockTimer = null, seen = 0, nPages = 0, nFiles = 0, t0 = 0;
var maxReq = 30, tipTimer = null, tipIdx = 0;
var TIPS = [
  'Saving each page with its pictures, styles and fonts.',
  'Links are being rewired so the copy works with no internet.',
  'Bigger sites simply take longer. Nothing is stuck.',
  'When this finishes, your ZIP downloads by itself.'
];

function specError(msg) {
  var e = $('specerr'); e.style.display = 'block'; e.textContent = msg;
}

async function start() {
  var url = $('url').value.trim();
  if (!url) { $('url').focus(); return; }
  $('specerr').style.display = 'none';
  $('go').disabled = true; $('go').textContent = 'Starting\u2026';
  try {
    var resp = await fetch('start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: url,
        maxPages: +$('pages').value || 30,
        depth: +$('depth').value || 3,
        render: $('render').checked
      })
    });
    var data = await resp.json();
    if (!resp.ok) { specError(data.error || 'Could not start.'); $('go').disabled = false; $('go').textContent = 'Download'; return; }
  } catch (err) {
    specError('Could not reach the server.'); $('go').disabled = false; $('go').textContent = 'Download'; return;
  }
  jobId = data.job;
  rooms = []; pipes = []; pagesArr = []; seen = 0; nPages = 0; nFiles = 0;
  $('roP').textContent = '0'; $('roF').textContent = '0'; $('rawlog').textContent = '';
  $('tbProj').textContent = url.replace(/https?:\/\//, '').toUpperCase().slice(0, 22);
  document.body.classList.remove('certified');
  document.body.classList.add('drafting');
  $('go').disabled = false; $('go').textContent = 'Download';
  maxReq = Math.max(1, +$('pages').value || 30);
  $('ppPct').textContent = '0%'; $('ppBar').style.width = '0%';
  $('howto').classList.remove('show');
  tipIdx = 0; $('ppTip').textContent = TIPS[0];
  tipTimer = setInterval(function () {
    tipIdx = (tipIdx + 1) % TIPS.length;
    $('ppTip').textContent = TIPS[tipIdx];
  }, 7000);
  t0 = Date.now();
  clockTimer = setInterval(function () { $('roT').textContent = Math.floor((Date.now()-t0)/1000) + 's'; }, 500);
  timer = setInterval(poll, 700);
}

function digest(line) {
  if (line.indexOf('page ') !== -1 && line.charAt(0) === '[') {
    var url = line.split('page ')[1] || '';
    nPages++; nFiles++;
    addPage(nPages === 1 ? 'INDEX' : lastSeg(url));
    $('roNow').textContent = 'downloading ' + url.replace(/https?:\/\//, '').slice(0, 60);
    $('roP').textContent = nPages;
    var pct = Math.min(99, Math.round(nPages / maxReq * 100));
    $('ppPct').textContent = pct + '%';
    $('ppBar').style.width = pct + '%';
  } else if (line.indexOf('+ asset') !== -1) {
    var aurl = line.split('+ asset ')[1] || '';
    nFiles++;
    var seg = lastSeg(aurl);
    var ext = (seg.indexOf('.') !== -1 ? seg.split('.').pop() : 'FILE');
    addAsset(ext);
  }
  $('roF').textContent = nFiles;
  var ml = $('mobilist');
  if (ml.childElementCount > 300) ml.removeChild(ml.firstChild);
  var atBottom = ml.scrollTop + ml.clientHeight >= ml.scrollHeight - 12;
  var d = document.createElement('div');
  if (line.indexOf('+ asset') !== -1) { d.className = 'as'; d.textContent = '+ ' + lastSeg(line.split('+ asset ')[1] || ''); }
  else if (line.charAt(0) === '[') { d.className = 'pg'; d.textContent = '▸ ' + (line.split('page ')[1] || '').replace(/https?:\/\//, '').slice(0, 44); }
  else return;
  ml.appendChild(d);
  if (atBottom) ml.scrollTop = ml.scrollHeight;
}

async function poll() {
  var s;
  try { s = await (await fetch('status?job=' + jobId)).json(); }
  catch (e) { return; }
  var fresh = s.log.slice(seen); seen = s.log.length;
  fresh.forEach(digest);
  var raw = $('rawlog');
  var stick = raw.scrollTop + raw.clientHeight >= raw.scrollHeight - 8;
  raw.textContent = s.log.join('\n');
  if (stick) raw.scrollTop = raw.scrollHeight;
  if (!s.done) return;
  clearInterval(timer); clearInterval(clockTimer);
  if (s.error) {
    document.body.classList.remove('drafting');
    $('specerr').style.display = 'block'; $('specerr').textContent = s.error;
    var sp = document.querySelector('.spec');
    sp.style.opacity = 1; sp.style.pointerEvents = 'auto'; sp.style.transform = 'translate(-50%, -50%)';
    return;
  }
  clearInterval(tipTimer);
  $('roNow').textContent = 'complete';
  $('ppPct').textContent = '100%'; $('ppBar').style.width = '100%';
  $('doneNote').innerHTML = '<span class="sum">'
    + (nPages <= 1
      ? 'Saved 1 page. It had no further links on the same site to follow, so that single page is your whole copy.'
      : 'Saved ' + nPages + ' pages and ' + nFiles + ' files.')
    + ' Your ZIP is downloading.</span>'
    + 'Unzip the file in your Downloads, then double-click <b>Open-website.html</b> to view your downloaded site.';
  $('zipLink').href = ZIPBASE + 'zip?job=' + jobId;
  document.body.classList.remove('drafting');
  document.body.classList.add('certified');
  var auto = document.createElement('a');
  auto.href = ZIPBASE + 'zip?job=' + jobId; auto.download = '';
  document.body.appendChild(auto); auto.click(); auto.remove();
}

$('go').addEventListener('click', start);
$('url').addEventListener('keydown', function (e) { if (e.key === 'Enter') start(); });
$('howOpen').addEventListener('click', function () {
  $('howto').classList.toggle('show');
});
$('again').addEventListener('click', function () {
  document.body.classList.remove('certified');
  $('howto').classList.remove('show');
  rooms = []; pipes = []; pagesArr = [];
  var sp = document.querySelector('.spec');
  sp.style.opacity = ''; sp.style.pointerEvents = ''; sp.style.transform = '';
  $('url').value = ''; $('url').focus();
});
</script>
</body>
</html>
"""


def render_index():
    return (PAGE
            .replace("__ZIP_BASE__",
                     (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") + "/")
                     if HOSTED and os.environ.get("RENDER_EXTERNAL_URL") else "")
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
        entry_rel = None
        if job.get("entry"):
            entry_rel = job["entry"].split(job["id"] + "/", 1)[-1]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, str(Path("website-files") / f.relative_to(root)))
            if entry_rel:
                target = "website-files/" + urllib.parse.quote(entry_rel)
                z.writestr("Open-website.html",
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<meta http-equiv='refresh' content='0; url=" + target + "'>"
                    "<title>Opening your website…</title></head>"
                    "<body style='font-family:sans-serif;padding:40px'>"
                    "<p>Opening your website… If nothing happens, "
                    "<a href='" + target + "'>click here</a>.</p></body></html>")
            z.writestr("READ-ME.txt",
                "Double-click Open-website.html to view your downloaded site.\n"
                "(Keep the website-files folder next to it.)\n"
                "\n"
                "sitegrab - a free tool by Douvenne - douvenne.com/projects/sitegrab\n")
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
