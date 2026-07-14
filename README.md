# sitegrab

Download a full website for offline reading — like your browser's "Save Page As",
but for the whole site.

`sitegrab` crawls a site starting from a URL, saves every page plus the assets
they need (CSS, JavaScript, images, fonts), and rewrites all the links so the
copy works offline in any browser. It's a single file with zero dependencies —
just the Python standard library.

## Usage

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

Example — grab up to 500 pages, three links deep:

```sh
python3 sitegrab.py https://example.com -o my-copy --max-pages 500 --depth 3
```

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

- Run JavaScript — sites that render entirely client-side (SPAs) will save
  their HTML shell, not the rendered content
- Cross domains — CDN-hosted assets on other domains are left as live links
- Log in — it only sees what an anonymous visitor sees

## Be a good citizen

Only mirror sites you're allowed to copy. Keep `--delay` reasonable, use
`--max-pages` limits, and respect the site's terms of service.
