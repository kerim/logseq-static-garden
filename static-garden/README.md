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
| `{{video URL}}` macros and `![](URL)` video links | YouTube and Vimeo players with a watch link, or a native video element for a direct video file |
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
Set `"embed_titles": false` in `site.json` to omit the title link above page embeds (default `true`). Set `"embed_style": "inline"` in `site.json` to render embedded pages/blocks as plain sub-blocks with no box, no title, and no bullet for the embed block itself (default `"boxed"`, the current boxed behavior; `embed_titles` only affects `"boxed"`).

## Security and attachments

Raw HTML is disabled in Markdown. Graph text is not evaluated as code, and link
schemes are restricted to HTTP(S), mail, and telephone links. KaTeX runs at build
time with `trust: false`. Search and graph labels use DOM text nodes.

The generated `_headers` supplies a Content Security Policy with no inline script
or eval allowance, blocks framing and object embeds, and sets `nosniff` and a
referrer policy. Scripts, styles, and fetched data must come from the site itself;
external images and audio/video must use HTTPS. Frames are allowed only from
`https://www.youtube-nocookie.com` and `https://player.vimeo.com` for the generated
players. Raw HTML and arbitrary iframe embeds remain disabled.

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

## Videos

A Logseq video macro renders as an embedded player. It can be a block of its own, or
sit on its own line or inside a sentence, in which case the paragraph is split around
the player:

```text
{{video https://youtu.be/0-zDFLbWK1Q}}
{{video https://vimeo.com/76979871}}
{{video https://example.com/talk.mp4}}
```

Markdown image syntax that points at a video, such as `![](https://youtu.be/0-zDFLbWK1Q)`
(common after an Obsidian import), renders the same way. Macros inside code blocks,
code spans, quotes, and indented code stay source.

- **YouTube.** Watch, short, Shorts, live, and embed URLs are supported, including `t=`
  or `start=` timestamps. Players use YouTube's privacy-enhanced domain.
- **Vimeo.** `vimeo.com/ID`, `player.vimeo.com/video/ID`, and unlisted-video hashes are
  supported. Players use Vimeo's `dnt=1` option.
- **Video files.** An `https://` URL ending in `.mp4`, `.webm`, `.ogg`, or `.mov`, or an
  attachment referenced with image syntax, renders a native `<video controls>` element
  with `preload="none"`.

Frame sources are built from a fixed provider host plus an ID validated by the
builder; the URL in the graph is never placed in a frame. Players load lazily and do not
autoplay. A player contacts its provider when it loads; the privacy-enhanced modes do not
mean no external requests. Each embed includes a normal watch link for videos with
playback or embedding restrictions. Unknown providers and invalid URLs stay readable
source and produce a warning.

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

## URL styles

`site.json`'s `"url_style"` controls how page URLs and breadcrumbs are built. It defaults
to `"uuid"`: every page (other than home) gets `/page/<slug>--<uuid>/`, and the breadcrumb
walks `block/parent` ancestry.

Setting `"url_style": "sections"` instead groups pages under the navigation entry that
references them:

- Each navigation page gets a bare top-level URL, e.g. `/technology/`.
- A page is assigned to the first navigation page (in `navigation` order) whose blocks —
  at any depth — reference it via `block/refs`, an embed's `block/link`, or a `[[link]]`
  in a block's title. That page then gets `/<section-slug>/<page-slug>/`, and its
  breadcrumb reads Home › Section › Page, with Section linked.
- A page no navigation entry reaches gets `/<page-slug>/`, with a two-level breadcrumb
  (Home › Page).
- Slugs are lowercased, with runs of punctuation/whitespace collapsed to a single hyphen.
- If two pages would resolve to the same URL, the first one (by navigation order, then
  title) keeps it; the rest fall back to `/<slug>--<uuid>/`, with a build warning. The
  same fallback applies if a top-level slug collides with a reserved path (`pages`,
  `graph`, `site`, `assets`, `licenses`, `downloads`, `404.html`, `robots.txt`,
  `sitemap.xml`, `_headers`).

## Deriving navigation from a page

A page titled `Topics` (or the title set in `"navigation_page"`) replaces `"navigation"`
with the list of pages that page's top-level blocks reference, in block order (first
`block/refs` entry, embed `block/link`, or `[[ref]]` in the title; duplicates and
blocks with no page reference are skipped). The navigation page itself is never
published. If it's missing from the export or yields no pages, the build falls back to
`"navigation"`; it logs a warning only when `"navigation_page"` was set.

## Hiding properties

A page titled `Hidden Properties` (or the title set in `"hidden_properties_page"`)
hides every property that page links to, via `[[Property name]]` in any of its
blocks. A hidden property's values are left off every page and block, and the
property's own page is not published. The list page itself is never published.
If the page is missing from the export or links to no properties, every property
is shown; the build logs a warning only when `"hidden_properties_page"` was set.

## Link preview image

The first image on a page titled `Link Preview` (or the title set in
`"link_preview_page"`) becomes the picture social sites and chat apps show when
any page of the site is shared (`og:image`, with a large `twitter:card`). It can
be a pasted image or a Markdown image link, anywhere in the page's outline;
changing the picture means replacing that image and exporting again. The page
itself is never published. Without such an image the site logo is used, and a
warning is logged only when `"link_preview_page"` was set.

## Admin pages

Every page tagged `#garden-admin` (or the tag set in `"admin_tag"`) is left off
the site, and so is the tag's own page. Tag Topics, Hidden Properties, Link
Preview and any other behind-the-scenes page with it; a page listing them all,
tagged the same way, makes them easy to find in Logseq. Links to these pages
from published pages show as plain text.

## Serving several sites from one builder

Three `site.json` options let one exporter build several distinct sites:

- `"theme_css"`: a path to a CSS file, resolved relative to the config file's own
  directory. When set, this file replaces `garden.css` as the main stylesheet
  (the Pygments code-highlighting styles are still appended after it, and
  `graph.css` is still loaded separately for the graph page since it only
  consumes CSS variables). Leaving it unset keeps the default `garden.css`. A
  configured file that doesn't exist fails the build with a clear error before
  anything is written.
- `"base_path"`: a URL prefix for a site served from a sub-folder, e.g.
  `"/logseq-faq"`. It's normalized to have a leading slash and no trailing
  slash (`"logseq-faq/"` becomes `"/logseq-faq"`; an empty value stays `""`).
  Every absolute URL the build emits — page links, breadcrumbs, the sidebar
  and mobile navigation, `/pages/`, `/graph/`, `/assets/`, `/downloads/`,
  hashed CSS/JS/JSON asset paths, favicons, canonical and `og:url` meta tags,
  `sitemap.xml`, `robots.txt`, `routes.json`, and `search.json` — is prefixed
  with it. Generated pages carry the prefix as `<html data-base="/logseq-faq">`,
  and `garden.js`'s `fetch()` calls read that attribute so client-side search
  and legacy-link redirects still resolve correctly. Files are still written
  to disk at their normal relative paths under the output directory; only the
  URLs embedded in the generated content change. This fits how GitHub Pages
  already serves a repository's Pages output prefixed at `/<repo>/`. The
  site's `"url"` may be given with or without the prefix
  (`https://user.github.io/logseq-faq` or `https://user.github.io`); full URLs
  never repeat it.
- `"exclude_pages"`: a list of page titles to drop from publishing even though
  they'd otherwise be public. They're removed the same way the navigation
  page itself is removed: dropped from the page set and from the name index,
  so any link to them degrades to the existing "unresolved reference"
  behavior (a plain, unlinked span) instead of rendering as a page link.

## Project files

| File | Purpose |
| --- | --- |
| `site.json` | Generic defaults; publishers normally supply `SOURCE/site.json`; optional `author`/`license` render as the sidebar license note (a known `license` name such as `"CC BY 4.0"` is linked), optional `url_style` (see below), optional `theme_css`/`base_path`/`exclude_pages` (see "Serving several sites from one builder"), optional `hidden_properties_page` (see "Hiding properties"), `link_preview_page` (see "Link preview image") and `admin_tag` (see "Admin pages") |
| `build.py` / `transit_reader.py` | Decode the graph, render pages, copy attachments, emit headers |
| `garden.css` / `garden.js` | Main layout, search, legacy bookmarks |
| `graph-layout.cjs` / `graph.js` / `graph.css` | Graph layout and browser viewer |
| `branding/logo.svg` / `logo.png` | Generic default logo; overridden by `SOURCE/branding/` |

Branding is copied to the hashed SVG used for the favicon, `static/img/logo.png`,
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
