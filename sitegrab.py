#!/usr/bin/env python3
"""sitegrab — download a full website for offline reading.

Crawls a site starting from a URL, saves every page plus the assets it
needs (CSS, JS, images, fonts), and rewrites links so the copy works
offline in any browser. Standard library only — no dependencies.

Usage:
    python3 sitegrab.py https://example.com
    python3 sitegrab.py https://example.com -o my-copy --max-pages 200 --depth 3
"""

import argparse
import os
import posixpath
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = "sitegrab/1.0 (+offline archiver)"


def make_ssl_context():
    """Build an SSL context that works even when Python's default cert
    bundle is missing (common with python.org installs on macOS)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"] == 0 and os.path.exists("/etc/ssl/cert.pem"):
        ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


SSL_CONTEXT = make_ssl_context()

# Chromium-based browsers that can render JavaScript pages headlessly
BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]

SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>|<script\b[^>]*/>",
                           re.IGNORECASE | re.DOTALL)


def find_browser(explicit=None):
    """Path to a Chromium-based browser binary, or None."""
    candidates = [explicit] if explicit else BROWSER_CANDIDATES
    for c in candidates:
        if c and os.path.exists(c):
            return c
        found = shutil.which(c) if c else None
        if found:
            return found
    return None

# HTML attributes that can reference other resources
LINK_ATTRS = {
    ("a", "href"),
    ("area", "href"),
}
ASSET_ATTRS = {
    ("img", "src"),
    ("img", "srcset"),
    ("source", "src"),
    ("source", "srcset"),
    ("script", "src"),
    ("link", "href"),
    ("video", "src"),
    ("video", "poster"),
    ("audio", "src"),
    ("embed", "src"),
    ("iframe", "src"),
    ("input", "src"),
}

CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")
CSS_IMPORT_RE = re.compile(r"""@import\s+['"]([^'"]+)['"]""")


class RefCollector(HTMLParser):
    """Collects every URL referenced by a page, split into pages vs assets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page_links = set()
        self.asset_links = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for (t, a) in LINK_ATTRS:
            if tag == t and attrs.get(a):
                self.page_links.add(attrs[a])
        for (t, a) in ASSET_ATTRS:
            if tag == t and attrs.get(a):
                value = attrs[a]
                if a == "srcset":
                    for part in value.split(","):
                        url = part.strip().split()[0] if part.strip() else ""
                        if url:
                            self.asset_links.add(url)
                else:
                    self.asset_links.add(value)


class SiteGrabber:
    def __init__(self, start_url, out_dir, max_pages, max_depth, delay, quiet=False,
                 browser=None):
        self.start_url = start_url
        self.root = urllib.parse.urlsplit(start_url)
        self.out_dir = Path(out_dir)
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.delay = delay
        self.quiet = quiet
        self.browser = browser  # path to headless Chrome; None = plain fetch
        self.local_paths = {}   # canonical URL -> Path relative to out_dir
        self.failed = set()
        self.pages_saved = 0
        self.assets_saved = 0

    # ---------- URL helpers ----------

    def canonicalize(self, url):
        """Absolute URL with fragment stripped, or None if unusable."""
        url, _ = urllib.parse.urldefrag(url)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        if not parts.path:
            parts = parts._replace(path="/")
        return urllib.parse.urlunsplit(parts)

    def is_internal(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        return host == self.root.netloc.lower()

    def local_path_for(self, url, content_type=""):
        """Map a URL to a file path under the output directory."""
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path) or "/"
        if path.endswith("/"):
            path += "index.html"
        # queries become part of the filename so distinct URLs don't collide
        if parts.query:
            stem, dot, ext = path.rpartition(".")
            safe_q = re.sub(r"[^A-Za-z0-9._-]", "_", parts.query)
            if dot:
                path = f"{stem}__{safe_q}.{ext}"
            else:
                path = f"{path}__{safe_q}"
        # HTML pages without an extension get one, so browsers open them right
        if "html" in content_type and not path.endswith((".html", ".htm")):
            path += ".html"
        rel = Path(parts.netloc.replace(":", "_")) / path.lstrip("/")
        return rel

    def relative_href(self, from_page_url, to_url):
        """Href that links from one saved file to another, or None."""
        target = self.local_paths.get(to_url)
        if target is None:
            return None
        source = self.local_paths[from_page_url]
        rel = posixpath.relpath(target.as_posix(), source.parent.as_posix())
        return urllib.parse.quote(rel)

    # ---------- fetching ----------

    def fetch(self, url):
        """Return (bytes, content_type) or (None, None) on failure."""
        if url in self.failed:
            return None, None
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                return resp.read(), ctype
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            self.log(f"  ! failed {url}: {e}")
            self.failed.add(url)
            return None, None

    def render_page(self, url):
        """Rendered DOM of a page after its JavaScript has run, via headless
        Chrome. Returns HTML text, or None if rendering failed."""
        cmd = [
            self.browser,
            "--headless=new",
            "--dump-dom",
            "--virtual-time-budget=10000",  # let JS/fetches settle, fast-forwarded
            "--timeout=30000",
            "--disable-gpu",
            "--no-first-run",
            "--hide-scrollbars",
            "--mute-audio",
            f"--user-agent={USER_AGENT}",
            url,
        ]
        if os.environ.get("SITEGRAB_CHROME_NO_SANDBOX") == "1":
            cmd.insert(-1, "--no-sandbox")  # required when running in Docker
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            self.log(f"  ! render failed {url}: {e}")
            return None
        html = result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0 or "<" not in html:
            self.log(f"  ! render failed {url} (exit {result.returncode})")
            return None
        # Drop script tags: the snapshot is already rendered, and re-running
        # the app's JS offline usually blanks the page or errors out.
        return SCRIPT_TAG_RE.sub("", html)

    def save(self, rel_path, data):
        dest = self.out_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def log(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    # ---------- assets ----------

    def grab_asset(self, url):
        """Download an asset once; returns True if we have it locally."""
        if url in self.local_paths:
            return True
        if url in self.failed:
            return False
        data, ctype = self.fetch(url)
        if data is None:
            return False
        rel = self.local_path_for(url, ctype)
        self.local_paths[url] = rel
        if ctype == "text/css" or str(rel).endswith(".css"):
            data = self.process_css(data, url)
        self.save(rel, data)
        self.assets_saved += 1
        self.log(f"  + asset {url}")
        return True

    def process_css(self, data, css_url):
        """Download url()/@import references inside CSS and rewrite them."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return data

        def replace(match, wrap):
            raw = match.group(1)
            if raw.startswith("data:"):
                return match.group(0)
            absolute = self.canonicalize(urllib.parse.urljoin(css_url, raw))
            if absolute and self.is_internal(absolute) and self.grab_asset(absolute):
                rel = self.relative_href(css_url, absolute)
                if rel:
                    return wrap(rel)
            return match.group(0)

        # the CSS file's own path must be registered before relative_href works
        text = CSS_URL_RE.sub(lambda m: replace(m, lambda r: f"url('{r}')"), text)
        text = CSS_IMPORT_RE.sub(lambda m: replace(m, lambda r: f"@import '{r}'"), text)
        return text.encode("utf-8")

    # ---------- pages ----------

    def rewrite_html(self, html, page_url):
        """Point every known reference at its local copy."""

        def sub_attr(match):
            prefix, quote, raw = match.group(1), match.group(2), match.group(3)
            if raw.startswith(("data:", "mailto:", "javascript:", "#", "tel:")):
                return match.group(0)
            base, frag = urllib.parse.urldefrag(raw)
            absolute = self.canonicalize(urllib.parse.urljoin(page_url, base))
            if absolute:
                rel = self.relative_href(page_url, absolute)
                if rel:
                    if frag:
                        rel += "#" + frag
                    return f"{prefix}{quote}{rel}{quote}"
            return match.group(0)

        return re.sub(
            r"""((?:href|src|poster)\s*=\s*)(["'])(.*?)\2""",
            sub_attr,
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

    def crawl(self):
        start = self.canonicalize(self.start_url)
        if start is None:
            sys.exit("error: start URL must be http(s)")

        queue = deque([(start, 0)])
        seen = {start}
        raw_pages = {}  # url -> html text, rewritten after crawl completes

        while queue and self.pages_saved < self.max_pages:
            url, depth = queue.popleft()
            data, ctype = self.fetch(url)
            if data is None:
                continue

            if "html" not in ctype:
                # linked directly to a file (PDF, image...) — save as asset
                rel = self.local_path_for(url, ctype)
                self.local_paths[url] = rel
                self.save(rel, data)
                self.assets_saved += 1
                continue

            rel = self.local_path_for(url, ctype)
            self.local_paths[url] = rel
            html = data.decode("utf-8", errors="replace")
            if self.browser:
                rendered = self.render_page(url)
                if rendered:
                    html = rendered
            raw_pages[url] = html
            self.pages_saved += 1
            self.log(f"[{self.pages_saved}/{self.max_pages}] page {url}")

            collector = RefCollector()
            try:
                collector.feed(html)
            except Exception:
                pass

            for raw in collector.asset_links:
                absolute = self.canonicalize(urllib.parse.urljoin(url, raw))
                if absolute and self.is_internal(absolute):
                    self.grab_asset(absolute)

            if depth < self.max_depth:
                for raw in collector.page_links:
                    absolute = self.canonicalize(urllib.parse.urljoin(url, raw))
                    if (
                        absolute
                        and absolute not in seen
                        and self.is_internal(absolute)
                    ):
                        seen.add(absolute)
                        queue.append((absolute, depth + 1))

            if self.delay:
                time.sleep(self.delay)

        # rewrite links only after the crawl, when we know every local path
        for url, html in raw_pages.items():
            self.save(self.local_paths[url], self.rewrite_html(html, url).encode("utf-8"))

        entry = self.local_paths.get(start)
        self.log(
            f"\ndone: {self.pages_saved} pages, {self.assets_saved} assets "
            f"-> {self.out_dir}/"
        )
        if entry:
            self.log(f"open offline: {self.out_dir / entry}")


def main():
    parser = argparse.ArgumentParser(
        prog="sitegrab",
        description="Download a website for offline reading.",
    )
    parser.add_argument("url", help="start URL, e.g. https://example.com")
    parser.add_argument("-o", "--output", default=None,
                        help="output directory (default: ./<domain>)")
    parser.add_argument("--max-pages", type=int, default=100,
                        help="maximum number of pages to download (default: 100)")
    parser.add_argument("--depth", type=int, default=5,
                        help="maximum link depth from the start page (default: 5)")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="seconds to wait between requests (default: 0.2)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print the final summary")
    parser.add_argument("-r", "--render", action="store_true",
                        help="render pages with headless Chrome first, so "
                             "JavaScript-built sites (SPAs) are captured")
    parser.add_argument("--browser", default=None,
                        help="path to a Chromium-based browser binary "
                             "(default: auto-detect)")
    args = parser.parse_args()

    url = args.url if "://" in args.url else "https://" + args.url
    out = args.output or urllib.parse.urlsplit(url).netloc.replace(":", "_")

    browser = None
    if args.render:
        browser = find_browser(args.browser)
        if not browser:
            sys.exit("error: --render needs a Chromium-based browser "
                     "(Chrome, Chromium, Edge, Brave); none found. "
                     "Point at one with --browser /path/to/binary")

    grabber = SiteGrabber(url, out, args.max_pages, args.depth, args.delay,
                          quiet=args.quiet, browser=browser)
    try:
        grabber.crawl()
    except KeyboardInterrupt:
        print("\ninterrupted — partial site saved in", out)


if __name__ == "__main__":
    main()
