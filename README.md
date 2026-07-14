# sitegrab

Download a full website for offline reading — like your browser's "Save Page As",
but for the whole site.

`sitegrab` crawls a site starting from a URL, saves every page plus the assets
they need (CSS, JavaScript, images, fonts), and rewrites all the links so the
copy works offline in any browser. It's a single file with zero dependencies —
just the Python standard library.

## The easy way: point-and-click UI

```sh
python3 sitegrab_ui.py
```

This opens a page in your browser: paste a website address, hit **Download
site**, watch the progress log, then click **Browse your offline copy**.
Downloads land in a `grabs/` folder next to the script.

## Command line

```sh
python3 sitegrab.py https://example.com
```

That downloads the site into `./example.com/`. Open the printed `index.html`
in your browser and browse it offline.

### Options

```
-o, --output DIR    output directory (default: ./<domain>)
--max-pages N       maximum number of pages to download (default: 100)
--depth N           maximum link depth from the start page (default: 5)
--delay SECONDS     wait between requests, be kind to servers (default: 0.2)
-q, --quiet         only print the final summary
```

```
-r, --render        render pages with headless Chrome first, so
                    JavaScript-built sites (SPAs) are captured
--browser PATH      path to a Chromium-based browser (default: auto-detect
                    Chrome, Chromium, Edge, or Brave)
```

Example — grab up to 500 pages, three links deep:

```sh
python3 sitegrab.py https://example.com -o my-copy --max-pages 500 --depth 3
```

### Numbered chapters: URL ranges

For books or docs published as numbered pages, put a `[N-M]` range in the
URL (quote it so your shell leaves the brackets alone):

```sh
python3 sitegrab.py "https://books.com/epk/[1-200]"
```

That downloads exactly chapters 1 through 200 — no link-following, so
nothing extra comes along — and rewrites the next/previous links between
chapters so you can read straight through offline. Zero-padded numbering
works too (`[001-200]`), and the same syntax works in the web UI's address
box. Big grabs like a 200-chapter book are best done locally rather than on
a hosted instance, which caps grabs at 30 pages.

## JavaScript-heavy sites (web apps)

Sites built with React, Vue, etc. send a nearly-empty HTML shell and build
the page in the browser. For those, use `--render`:

```sh
python3 sitegrab.py https://quotes.toscrape.com/js/ --render
```

With `--render`, each page is loaded in headless Chrome, its JavaScript runs,
and sitegrab saves the *rendered* result — what you'd actually see on screen.
Script tags are stripped from the snapshot (the page is already rendered, and
re-running the app's code offline usually breaks it), so what you get is a
frozen, readable copy of every page. It needs a Chromium-based browser
installed and is slower than a plain grab.

## What it does

- Breadth-first crawl of every same-domain page reachable from the start URL
- Downloads referenced assets: stylesheets, scripts, images, `srcset` variants,
  video posters, favicons
- Follows `url()` and `@import` references *inside* CSS files (fonts,
  background images) and rewrites those too
- Rewrites every link to a relative local path, so the copy works from any
  folder — no web server needed
- Links to pages that weren't downloaded (over the page/depth limit) are left
  pointing at the live site

## What it doesn't do

- Run JavaScript by default — for client-side-rendered sites (SPAs), use
  `--render` (see above); the saved copy is readable but not interactive
- Cross domains — CDN-hosted assets on other domains are left as live links
- Log in — it only sees what an anonymous visitor sees

## Host it as a public service

The UI can run as a website so anyone can use it without installing anything.
The repo ships with a `Dockerfile` and `render.yaml` — on [Render](https://render.com)
(free tier, no card required):

1. Sign up with your GitHub account
2. **New → Web Service**, pick this repo — Render reads the Dockerfile
   automatically
3. Deploy. Your instance lives at `https://<name>.onrender.com`

Hosted mode (`SITEGRAB_HOSTED=1`, set by the Dockerfile) binds to `$PORT`,
returns grabs as ZIP downloads, caps each grab at 30 pages / depth 3, runs at
most two grabs at once, and deletes saved grabs after 30 minutes. The
JavaScript render option works in the container via the bundled Chromium.

Note: free-tier services sleep when idle — the first visit after a quiet
spell takes ~30 seconds to wake up.

## Be a good citizen

Only mirror sites you're allowed to copy. Keep `--delay` reasonable, use
`--max-pages` limits, and respect the site's terms of service.
