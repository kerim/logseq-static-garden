# Static garden exporter

Builds a reading website from **Logseq 2.0.1 DB graph -> Export public pages**.
It reads the graph embedded in `index.html` and emits one HTML document per page.
There is no Logseq runtime or browser database in the output.

## Build and preview

From the exporter repository:

```sh
./build-static.sh --source examples/minimal --output dist
python3 -m http.server 8000 --directory dist
```

The wrapper installs pinned Python dependencies into `.venv-static/`. Node.js runs
bundled KaTeX and the graph layout script. No npm install is needed, and JavaScript
from the source Logseq export is never executed.

Once dependencies are installed, an offline build can run directly:

```sh
.venv-static/bin/python static-garden/build.py --source /path/to/public-export
```

`--source` defaults to the current directory. `--output` defaults to `SOURCE/dist`.
`--config` defaults to `SOURCE/site.json`, falling back to the generic exporter
configuration. Paths are resolved from the current directory. A supplied `--config`
that does not exist fails the build.

The build uses a temporary directory, checks block coverage and attachment sizes,
then replaces the generated output. It refuses to replace the source, the exporter,
or an unrelated nonempty directory. `dist-report.json` records counts, sizes, and
warnings beside the output, outside the deployed website.

## Publishing

Publish only the generated directory. The raw export is a build input, not the
site. Cloudflare Pages understands the generated `_headers`; other hosts need
matching response-header configuration. No server or database is required.

Keep your site's notes, configuration, logo, hosting setup, and Git hooks in its
own repository. Pin the exporter to a version you have tested. An export should
not overwrite the tool or change which version a deployment uses.

The old `patch-index.py` step is unnecessary. Asset compression is separate from
rendering; a referenced attachment larger than Cloudflare Pages' 25 MiB limit
fails the build.

## Rendering

| Source feature | Published behavior |
| --- | --- |
| Blocks and properties | Nested HTML outlines, native collapse controls, visible property values |
| Page and block references | Links to pages and block anchors |
| Tags, nested pages, backlinks | Collections and related-page lists computed at build time |
| Code and equations | Pygments highlighting and native MathML, rendered during the build |
| Graph | Build-time layout with a Canvas viewer loaded only on `/graph/` |
| Search | Full-text JSON index fetched on the first query |
| Old `#/page/` bookmarks | Small script resolves exported names and UUIDs to static routes |
| Images, audio, video, PDFs | Referenced files copied; lazy images and media with `preload="none"` |
| YouTube `{{video URL}}` blocks | Responsive, lazy-loaded player with a Watch on YouTube link |
| Other attachments | Downloads with original filenames offered by the links |

Page URLs include a UUID suffix to avoid slug collisions. Renaming a page changes
its slug; legacy UUID hash bookmarks still resolve. Reading, all-pages browsing,
and collapsing blocks work without JavaScript. Search and the graph require it.

On phones, a bottom navigation bar provides Home, Pages, Search, Graph, and an
expandable Explore menu. The menu works without JavaScript. Graph touch targets
are larger than mouse targets; tapping selects a page and shows an Open page link
inside the graph. Dragging and pinch-to-zoom remain available.

The source directory’s `site.json` controls the collection links in both the desktop sidebar and the
mobile Explore menu. Links appear only when their targets exist in the public export.
Set `"embed_titles": false` in `site.json` to omit the title link above page embeds (default `true`).

## Security and attachments

Raw HTML is disabled in Markdown. Graph text is not evaluated as code, and link
schemes are restricted to HTTP(S), mail, and telephone links. KaTeX runs at build
time with `trust: false`. Search and graph labels use DOM text nodes.

The generated `_headers` supplies a Content Security Policy with no inline script
or eval allowance, blocks framing and object embeds, and sets `nosniff` and a
referrer policy. Scripts, styles, and fetched data must come from the site itself;
external images and audio/video must use HTTPS. Frames are allowed only from
`https://www.youtube-nocookie.com` for the generated YouTube players. Raw HTML
and arbitrary iframe embeds remain disabled.

Local attachments have two destinations:

- `/assets/`: allowlisted raster images, audio, video, and PDFs retain their URLs.
- `/downloads/`: every other format, including HTML, SVG, scripts, and office
  documents, receives a `.download` suffix. Links offer the original filename.
  Cloudflare serves these as `application/octet-stream` with
  `Content-Disposition: attachment` and an additional sandbox CSP. They cannot
  become executable pages on the garden's origin.

The only local SVG served as an image is the trusted logo from the source directory’s `branding/` (or the generic default). Asset paths
and symlinks escaping the source's `assets/` directory fail the build. Attachment
contents are not modified or scanned for malware; downloaded files remain untrusted.
Old direct URLs to files moved into `/downloads/` change; generated note links
point to the new locations.

These response headers depend on the host honoring
[Cloudflare's `_headers` format](https://developers.cloudflare.com/pages/configuration/headers/).
Python's preview server does not apply them. A different host needs equivalent
header configuration. The raw export's root `_headers` is not used for `dist/`.

Only referenced local assets are copied, and the build reads only the public
export. It never opens the private Logseq database. This is not a secret scanner:
exported text, property labels, attachments, search data, and Git history can still
contain information that was published accidentally.

## YouTube videos

A standalone Logseq video block renders as an embedded player:

```text
{{video https://youtu.be/0-zDFLbWK1Q}}
```

YouTube watch, short, Shorts, live, and embed URLs are supported, including `t=`
or `start=` timestamps. A Markdown link inside the macro also works. Players use
YouTube's privacy-enhanced domain, load lazily, and do not autoplay. The player
contacts YouTube when it loads; privacy-enhanced mode does not mean no external
requests. Each embed includes a normal YouTube link for videos with playback or
embedding restrictions. Unknown providers and invalid URLs stay readable source.

## Format limits

The Transit reader supports the types used by this Logseq DB export and rejects
unknown types. It does not support the older Markdown-graph export format.

Plugins, editing, flashcard scheduling, arbitrary Hiccup/HTML, and live Datalog
queries are outside the renderer. Page and block embeds (`{{embed}}`) render
inline; other unsupported query or macro syntax remains readable source and
produces a warning. Missing references and attachments,
unsupported links, and equation errors are also reported. Missing private targets
are never fetched; an exported property value may still expose its label without
a corresponding public page body.

## Project files

| File | Purpose |
| --- | --- |
| `site.json` | Generic defaults; publishers normally supply `SOURCE/site.json` |
| `build.py` / `transit_reader.py` | Decode the graph, render pages, copy attachments, emit headers |
| `garden.css` / `garden.js` | Main layout, search, legacy bookmarks |
| `graph-layout.cjs` / `graph.js` / `graph.css` | Graph layout and browser viewer |
| `branding/logo.svg` / `logo.png` | Generic default logo; overridden by `SOURCE/branding/` |

Branding is copied to the hashed SVG used in the sidebar, `static/img/logo.png`,
and `favicon.png` in the output. Generated pages also include canonical URLs,
a sitemap, and a real `404.html` rather than an SPA fallback.

## Tests

```sh
.venv-static/bin/python -m unittest discover -s static-garden -p 'test_*.py' -v
```

Tests cover Transit decoding, rendering, page filtering, reference handling,
attachment containment, script injection, download routing, generated security
headers, separate source directories, branding, and the fictional example build.

## License

The original exporter, browser code, generic logo, examples, and docs are
[MIT licensed](../licenses/MIT.txt). [Third-party notices](../THIRD_PARTY_NOTICES.md)
cover bundled KaTeX and build dependencies. Keep those files with the exporter.

Each generated site includes the exporter notices at `/licenses/`. If the source
has `LICENSE.md`, `THIRD_PARTY_NOTICES.md`, or a `licenses/` directory, those notices
are also retained. Publisher documents appear as `SITE-LICENSE.md` and
`SITE-NOTICES.md`. Exporting content does not grant a new license to that content.
