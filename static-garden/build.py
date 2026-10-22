#!/usr/bin/env python3
"""Compile Logseq DB public-page exports to static HTML. Never execute graph text."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from html import escape, unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, quote, unquote, urlsplit

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.util import ClassNotFound
from transit_reader import load_export

HERE = Path(__file__).resolve().parent
UUID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
REF = re.compile(r'\[\[([^\]]+)\]\]|\(\(([^)]+)\)\)')
# Fixed URL map for known license names. A config "license" that isn't listed
# here is rendered as plain text (no link).
LICENSE_URLS = {'CC BY 4.0': 'https://creativecommons.org/licenses/by/4.0/'}
# Only these attachment formats can be opened on the site's origin. Everything
# else gets an inert extension and download-only response headers.
INLINE_ASSETS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.ico',
                 '.mp3', '.ogg', '.wav', '.m4a', '.flac', '.mp4', '.webm', '.mov', '.pdf'}
HEADERS = """/*
  Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; font-src 'self'; connect-src 'self'; media-src 'self' https:; frame-src https://www.youtube-nocookie.com https://player.vimeo.com; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin

/downloads/*
  Content-Type: application/octet-stream
  Content-Disposition: attachment
  Content-Security-Policy: sandbox; default-src 'none'; base-uri 'none'; form-action 'none'

/site/garden-*
  Cache-Control: public, max-age=31536000, immutable

/site/*.json
  Cache-Control: public, max-age=0, must-revalidate
"""


def published_asset_path(path):
    path = Path(path)
    if path.suffix.lower() in INLINE_ASSETS:
        return path
    return Path('downloads') / (str(path.relative_to('assets')) + '.download')


def download_name(url):
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc and parts.path.startswith('/downloads/') and parts.path.endswith('.download'):
        return unquote(parts.path.rsplit('/', 1)[-1])[:-len('.download')]
    return None


def download_attribute(url):
    name = download_name(url)
    return f' download="{escape(name, quote=True)}"' if name is not None else ''


def values(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


VIDEO_FILE = re.compile(r'\.(?:mp4|webm|ogg|mov)', re.I)
VIDEO_MACRO = re.compile(r'(`+).+?\1|\{\{video\s+[^{}]+?\}\}')  # a code span (skipped) or a video macro
FENCE = re.compile(r'[ ]{0,3}(`{3,}|~{3,})(.*)')
# Logseq's built-in #+BEGIN_<NAME> ... #+END_<NAME> blocks, matched as Logseq does:
# in any case, with anything after the name ignored.
CALLOUT_NAMES = 'NOTE|TIP|IMPORTANT|CAUTION|PINNED|WARNING|EXAMPLE|VERSE|CENTER'
CALLOUT_BEGIN = re.compile(r'[ ]{0,3}#\+BEGIN_(' + CALLOUT_NAMES + r')(?:\s.*)?', re.I)
CALLOUT_MARKER = re.compile(
    r'^[ ]{0,3}#\+(?:BEGIN|END)_(?:' + CALLOUT_NAMES + r')\b.*$', re.I | re.M)


def video_argument(raw):
    """Unwrap a video macro argument: a plain URL, <URL>, or a Markdown link."""
    raw = raw.strip()
    markdown_link = re.fullmatch(r'\[[^\]\n]*\]\(([^\s()]+)\)', raw)
    if markdown_link:
        return markdown_link[1]
    if raw.startswith('<') and raw.endswith('>'):
        return raw[1:-1]
    return raw


def video_figure(frame, caption):
    return f'<figure class="video-embed">{frame}<figcaption>{caption}</figcaption></figure>'


def youtube_embed(parts, title):
    hosts = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
             'youtube-nocookie.com', 'www.youtube-nocookie.com', 'youtu.be', 'www.youtu.be'}
    if parts.netloc.lower() not in hosts:
        return None
    query = parse_qs(parts.query)
    if parts.netloc.lower() in ('youtu.be', 'www.youtu.be'):
        video_id = parts.path.lstrip('/')
    elif parts.path == '/watch':
        video_id = query.get('v', [''])[0]
    else:
        path = re.fullmatch(r'/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})/?', parts.path)
        video_id = path[1] if path else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return None
    stamp = query.get('start', query.get('t', parse_qs(parts.fragment).get('t', [''])))[0]
    time = re.fullmatch(r'(?:(\d{1,6})h)?(?:(\d{1,6})m)?(?:(\d{1,6})s)?', stamp)
    start = 0
    if re.fullmatch(r'\d{1,8}', stamp):
        start = int(stamp)
    elif time:
        start = sum(int(n or 0) * multiplier for n, multiplier in zip(time.groups(), (3600, 60, 1)))
    start = min(start, 2147483647)
    src = f'https://www.youtube-nocookie.com/embed/{video_id}?playsinline=1' + (f'&start={start}' if start else '')
    watch = f'https://www.youtube.com/watch?v={video_id}' + (f'&t={start}s' if start else '')
    return video_figure(
        f'<iframe src="{escape(src, quote=True)}" title="{escape(title or "YouTube video player", quote=True)}" '
        'width="560" height="315" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" '
        'allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe>',
        f'<a href="{escape(watch, quote=True)}">Watch on YouTube ↗</a>')


def vimeo_embed(parts, title):
    host, query = parts.netloc.lower(), parse_qs(parts.query)
    if host in ('vimeo.com', 'www.vimeo.com'):
        # vimeo.com/ID, or vimeo.com/ID/HASH for an unlisted video.
        path = re.fullmatch(r'/(\d{1,12})(?:/([0-9a-f]{6,16}))?/?', parts.path)
        video_id, key = (path[1], path[2]) if path else ('', None)
    elif host == 'player.vimeo.com':
        path = re.fullmatch(r'/video/(\d{1,12})/?', parts.path)
        video_id, key = (path[1], query.get('h', [''])[0]) if path else ('', None)
    else:
        return None
    if not video_id or (key and not re.fullmatch(r'[0-9a-f]{6,16}', key)):
        return None
    src = f'https://player.vimeo.com/video/{video_id}?' + (f'h={key}&' if key else '') + 'dnt=1'
    watch = f'https://vimeo.com/{video_id}' + (f'/{key}' if key else '')
    return video_figure(
        f'<iframe src="{escape(src, quote=True)}" title="{escape(title or "Vimeo video player", quote=True)}" '
        'width="560" height="315" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" '
        'allow="picture-in-picture; fullscreen" allowfullscreen></iframe>',
        f'<a href="{escape(watch, quote=True)}">Watch on Vimeo ↗</a>')


def video_embed(url, title='', local=False):
    """Embed a YouTube or Vimeo player, or a direct video file. Iframe sources are built from a fixed
    host plus an ID validated here; the graph's URL itself is never placed in a frame. `local` also
    accepts a site-relative attachment path (an already-resolved `/assets/...` link)."""
    try:
        parts = urlsplit(unescape(url))
    except ValueError:
        return None
    if parts.scheme in ('http', 'https'):
        embed = youtube_embed(parts, title) or vimeo_embed(parts, title)
        if embed:
            return embed
    if VIDEO_FILE.fullmatch(Path(parts.path).suffix) and (
            parts.scheme == 'https' and parts.netloc or (local and not parts.scheme and url.startswith('/'))):
        return (f'<figure class="video-embed"><video controls playsinline preload="none" '
                f'src="{escape(url, quote=True)}"></video></figure>')
    return None


def video_html(text):
    """Render a block that is only a video macro, never arbitrary HTML."""
    macro = re.fullmatch(r'\s*\{\{video\s+([^{}]+?)\}\}\s*', text)
    return video_embed(video_argument(macro[1])) if macro else None


def video_segments(text):
    """Split block text into Markdown chunks and the video macros found outside code: fenced blocks,
    code spans, quotes and indented code stay source. A macro inside a sentence splits its paragraph
    around the player. Returns None when the block has no embeddable video macro."""
    whole = video_html(text)
    if whole is not None:
        return [('video', whole)]
    segments, chunk, fence, callout, found = [], [], None, None, False
    def flush():
        if any(line.strip() for line in chunk):
            segments.append(('markdown', '\n'.join(chunk)))
        chunk.clear()
    for line in text.split('\n'):
        marker = FENCE.fullmatch(line)
        begin = CALLOUT_BEGIN.fullmatch(line)
        if marker and callout is None:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif fence is None and callout is None and begin:
            callout = re.compile(r'[ ]{0,3}#\+END_' + begin[1] + r'\s*', re.I)
        elif callout is not None:
            # A callout stays whole: splitting it around a player would part its ends.
            if callout.fullmatch(line):
                callout = None
        elif fence is None and not line.startswith(('>', '    ', '\t')):
            pos = 0
            for macro in VIDEO_MACRO.finditer(line):
                html = None if macro[1] else video_html(macro[0])
                if html is not None:
                    chunk.append(line[pos:macro.start()])
                    flush()
                    segments.append(('video', html))
                    found, pos = True, macro.end()
            line = line[pos:]
        chunk.append(line)
    flush()
    return segments if found else None


def slugify(title):
    return re.sub(r'[^\w-]+', '-', title.casefold(), flags=re.UNICODE).strip('-')[:90] or 'page'


def strip_markdown(text):
    """Reduce markdown source to plain text for use in a meta description."""
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = text.replace('[[', '').replace(']]', '')
    text = re.sub(r'\^\^(.+?)\^\^', r'\1', text)
    text = CALLOUT_MARKER.sub('', text)
    return re.sub(r'\s+', ' ', text).strip()


# A Logseq numbered list counts 1. 2. 3.; one nested inside a numbered block counts
# a. b. c., then i. ii. iii., then starts over. Each style is (<ol type=...>, label).
def alpha_marker(n):
    out = ''
    while n > 0:
        n, rest = divmod(n - 1, 26)
        out = chr(97 + rest) + out
    return out


ROMAN_VALUES = (('m', 1000), ('cm', 900), ('d', 500), ('cd', 400), ('c', 100),
                ('xc', 90), ('l', 50), ('xl', 40), ('x', 10), ('ix', 9),
                ('v', 5), ('iv', 4), ('i', 1))


def roman_marker(n):
    if not 0 < n < 4000:
        return str(n)
    out = ''
    for symbol, value in ROMAN_VALUES:
        while n >= value:
            out += symbol
            n -= value
    return out


LIST_STYLES = (('1', str), ('a', alpha_marker), ('i', roman_marker))

# Appended after the site's own stylesheet, so numbered lists look right in any theme.
LIST_CSS = '''
ol.outline{list-style:none}
ol.root-outline{padding-left:22px}
.outline.continued{margin-top:0}
.list-number{position:absolute;right:100%;top:1px;margin-right:5px;font-size:14px;line-height:1.7;color:var(--faint,#8b9398);white-space:nowrap}
.list-number:hover{color:var(--accent,#c4433f);text-decoration:none}
.block.numbered>details>summary:before{left:-36px}
'''

# The five block background colors Logseq offers (logseq.property/color.*), as see-through
# tints that suit light and dark themes. Logseq has no text-color property; its inline
# highlight (^^text^^) is shown as <mark> in the same yellow.
BLOCK_COLORS = {'yellow': '234,179,8', 'red': '239,68,68', 'green': '34,197,94',
                'blue': '59,130,246', 'purple': '168,85,247'}
COLOR_CSS = ('\n.block-body.has-bg{padding:6px 12px;border-radius:6px}\n'
             'mark{background:rgba(234,179,8,.3);color:inherit;padding:0 .15em;border-radius:3px}\n') + ''.join(
    f'.block-body.bg-{name}{{background:rgba({rgb},.2)}}\n' for name, rgb in BLOCK_COLORS.items())

RESERVED_TOP_LEVEL_SLUGS = {'pages', 'graph', 'site', 'assets', 'licenses', 'downloads',
                            '404.html', 'robots.txt', 'sitemap.xml', '_headers'}


def read_entities(db):
    entities = defaultdict(dict)
    many = {k for k, v in db['schema'].items() if isinstance(v, dict) and v.get('db/cardinality') == 'db.cardinality/many'}
    for datom in db['datoms']:
        eid, attr, value, *_ = datom.value
        if attr in many:
            entities[eid].setdefault(attr, []).append(value)
        else:
            entities[eid][attr] = value
    return dict(entities)


def drop_comments(entities):
    """Drop Logseq comment threads (blocks tagged Comments/Comment) and everything under them."""
    classes = {i for i, n in entities.items() if n.get('db/ident') in ('logseq.class/Comments', 'logseq.class/Comment')}
    hidden = {i for i, n in entities.items() if classes & set(values(n.get('block/tags')))}
    changed = bool(hidden)
    while changed:
        before = len(hidden)
        hidden |= {i for i, n in entities.items() if n.get('block/parent') in hidden}
        changed = len(hidden) != before
    for i in hidden:
        del entities[i]


def normalize_base_path(raw):
    """Ensure a leading slash and strip any trailing slash; '' stays ''."""
    raw = (raw or '').strip()
    if not raw:
        return ''
    return '/' + raw.strip('/')


def highlight_rule(state, silent):
    """Logseq's inline highlight, ^^text^^, becomes <mark>text</mark>; the text may hold other markup."""
    start = state.pos
    if state.src[start:start + 2] != '^^':
        return False
    end = state.src.find('^^', start + 2, state.posMax)
    if end == -1 or end == start + 2:
        return False
    if not silent:
        outer_end = state.posMax
        state.pos, state.posMax = start + 2, end
        state.push('mark_open', 'mark', 1)
        state.md.inline.tokenize(state)
        state.push('mark_close', 'mark', -1)
        state.posMax = outer_end
    state.pos = end + 2
    return True


# Logseq's callout icons, redrawn as outlines. The color comes from the stylesheet.
CALLOUT_ICONS = {
    'note': '<circle cx="12" cy="12" r="9.5"/><path d="M12 11v6M12 7.5v.01"/>',
    'tip': ('<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.7.5 1.1 1.3 1.1 2.2h5'
            'c0-.9.4-1.7 1.1-2.2A6 6 0 0 0 12 3z"/>'),
    'important': '<circle cx="12" cy="12" r="9.5"/><path d="M12 7v6M12 16.5v.01"/>',
    'caution': ('<path d="M12 21.5c3.9 0 6.5-2.6 6.5-6.2 0-3.4-2.3-5.6-3.8-8.3-.5 2-1.4 '
                '3-2.9 3.5.2-3.2-.9-5.9-3.3-8 0 3.8-3.5 6.2-3.5 11 0 4.4 2.9 8 7 8z"/>'),
    'warning': ('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9'
                'a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17v.01"/>'),
    'pinned': '<path d="M9 3h6M10 3v6l-3 4h10l-3-4V3M12 13v8"/>',
}

# Appended after the site's own stylesheet. Logseq draws a callout as a padded box with
# the icon behind a thin divider and the text a little larger; red marks "important",
# orange "caution" and "warning".
CALLOUT_CSS = '''
.callout{display:flex;align-items:stretch;gap:16px;margin:8px 0;padding:8px}
.callout-icon{display:flex;align-items:center;padding-right:16px;
  border-right:1px solid var(--line,#dcdee0);color:var(--text,#292c2e)}
.callout-icon svg{width:32px;height:32px;fill:none;stroke:currentColor;
  stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.callout-important .callout-icon{color:#ef4444}
.callout-caution .callout-icon,.callout-warning .callout-icon{color:#f97316}
.callout-body{flex:1;min-width:0;font-size:1.1em}
.callout-body>:first-child{margin-top:0}
.callout-body>:last-child{margin-bottom:0}
.callout-center{text-align:center}
pre.callout-example{white-space:pre-wrap;overflow-wrap:anywhere}
'''


def callout_rule(state, start, end, silent):
    """Logseq's #+BEGIN_<NAME> ... #+END_<NAME> blocks. Callouts, centered and verse
    text hold Markdown; an example keeps its text exactly as typed. A block with no end
    line stays as typed, as in Logseq."""
    if state.sCount[start] - state.blkIndent >= 4:
        return False
    def line(n):
        return state.src[state.bMarks[n] + state.tShift[n]:state.eMarks[n]]
    begin = CALLOUT_BEGIN.fullmatch(line(start))
    if not begin:
        return False
    name = begin[1].lower()
    close = re.compile(r'#\+END_' + name + r'\s*', re.I)
    last = start + 1
    while last < end and not close.fullmatch(line(last)):
        last += 1
    if last >= end:
        return False
    if silent:
        return True
    if name == 'example':
        token = state.push('logseq_example', 'pre', 0)
        token.content = state.getLines(start + 1, last, state.blkIndent, False)
    else:
        token = state.push('logseq_callout_open', 'div', 1)
        token.info = name
        outer = state.parentType, state.lineMax
        state.parentType, state.lineMax = 'logseq_callout', last
        state.md.block.tokenize(state, start + 1, last)
        state.parentType, state.lineMax = outer
        state.push('logseq_callout_close', 'div', -1).info = name
    token.map = [start, last + 1]
    state.line = last + 1
    return True


def render_callout_open(tokens, idx, options, env):
    name = tokens[idx].info
    if name not in CALLOUT_ICONS:
        return f'<div class="callout-{name}">'
    label = name.capitalize()
    return (f'<div class="callout callout-{name}"><span class="callout-icon" role="img" '
            f'aria-label="{label}" title="{label}"><svg viewBox="0 0 24 24" '
            f'aria-hidden="true" focusable="false">{CALLOUT_ICONS[name]}</svg></span>'
            '<div class="callout-body">')


def render_callout_close(tokens, idx, options, env):
    return '</div></div>\n' if tokens[idx].info in CALLOUT_ICONS else '</div>\n'


def render_example(tokens, idx, options, env):
    text = escape(tokens[idx].content.rstrip('\n'))
    return '<pre class="callout-example">' + text + '</pre>\n'


class Garden:
    def __init__(self, entities, source, config):
        drop_comments(entities)
        self.entities, self.source, self.config = entities, source, config
        self.branding = source / 'branding' if (source / 'branding').exists() else HERE / 'branding'
        for name in ('logo.svg', 'logo.png'):
            if not (self.branding / name).is_file():
                raise ValueError(f'Branding file missing: {self.branding / name}')
        self.base_path = normalize_base_path(config.get('base_path', ''))
        self.warnings = set()
        self.assets = set()
        self.math = {}
        self.rendered_ids = set()
        self.pages = {i: n for i, n in entities.items() if 'block/name' in n
                      and not n.get('logseq.property/built-in?')
                      and not str(n.get('db/ident', '')).startswith(('logseq.', 'plugin.'))
                      and n.get('logseq.property/publishing-public?') is not False
                      and not n.get('block/closed-value-property')}
        self.uuids = {n['block/uuid']: i for i, n in entities.items() if 'block/uuid' in n}
        self.names = {}
        for i, n in self.pages.items():
            for name in (n['block/name'], n['block/title']):
                key = name.casefold()
                if key in self.names and self.names[key] != i:
                    self.warnings.add(f'Duplicate page name: {name}; UUID URLs remain distinct')
                else:
                    self.names[key] = i
        for excluded_title in config.get('exclude_pages', []):
            excluded_eid = self.resolve(excluded_title)
            if excluded_eid in self.pages:
                for key, val in list(self.names.items()):
                    if val == excluded_eid:
                        del self.names[key]
                del self.pages[excluded_eid]
        self.home = self.resolve(config['home_page'])
        if self.home not in self.pages:
            raise ValueError(f'Home page not found: {config["home_page"]}')
        self.property_entities = {n['db/ident']: n for n in entities.values() if 'db/ident' in n}
        # A block that only holds a property's value (Logseq keeps a block's background
        # color, for one, as a child block named "green") is not content.
        self.property_values = {i for i, n in entities.items() if 'logseq.property/created-from-property' in n}
        self.children = defaultdict(list)
        for i, n in entities.items():
            parent = n.get('block/parent')
            if parent is not None and i not in self.pages and i not in self.property_values:
                self.children[parent].append(i)
        for ids in self.children.values():
            ids.sort(key=lambda i: (entities[i].get('block/order', ''), i))
        self.hidden_properties = set()
        hidden_page_title = config.get('hidden_properties_page')
        if hidden_page_title:
            hidden_page = self.resolve(hidden_page_title)
            if hidden_page in self.pages:
                for node in (n for n in entities.values() if n.get('block/page') == hidden_page):
                    targets = set(values(node.get('block/refs')))
                    targets |= {self.resolve(m[1] or m[2]) for m in REF.finditer(node.get('block/title', ''))}
                    for target in targets:
                        ident = str(entities.get(target, {}).get('db/ident', ''))
                        if ident.startswith('user.property/'):
                            self.hidden_properties.add(ident)
                # The list page and each hidden property's own page are never published.
                hidden_pages = [hidden_page] + [i for i, n in self.pages.items() if n.get('db/ident') in self.hidden_properties]
                for eid in hidden_pages:
                    for key, val in list(self.names.items()):
                        if val == eid:
                            del self.names[key]
                    del self.pages[eid]
            if not self.hidden_properties:
                self.warnings.add(f"hidden_properties_page '{hidden_page_title}' not found or lists no properties; showing all properties")
        self.url_style = config.get('url_style', 'uuid')
        nav_page_title = config.get('navigation_page')
        self.nav_ids = None
        if nav_page_title:
            nav_page_eid = self.resolve(nav_page_title)
            if nav_page_eid in self.pages:
                derived = []
                for b in self.children.get(nav_page_eid, ()):
                    target = self._first_page_ref(b)
                    if target is not None and target != nav_page_eid and target not in derived:
                        derived.append(target)
                # The navigation page itself is never published: it exists only
                # to drive the sidebar/section/breadcrumb navigation order.
                for key, val in list(self.names.items()):
                    if val == nav_page_eid:
                        del self.names[key]
                del self.pages[nav_page_eid]
                if derived:
                    self.nav_ids = derived
                else:
                    self.warnings.add(f"navigation_page '{nav_page_title}' not found or empty; using config navigation")
            else:
                self.warnings.add(f"navigation_page '{nav_page_title}' not found or empty; using config navigation")
        if self.nav_ids is None:
            self.nav_ids = [eid for eid in (self.resolve(label) for label in config.get('navigation', [])) if eid in self.pages]
        self.sections = {}
        if self.url_style == 'sections':
            for nav_eid in self.nav_ids:
                for target in self._section_targets(nav_eid):
                    if target == self.home or target in self.nav_ids or target in self.sections:
                        continue
                    self.sections[target] = nav_eid
        self.urls = self._build_urls()
        self.page_links = set()
        self.current_page = None
        self.backlinks = defaultdict(set)
        self.tagged = defaultdict(set)
        for i, n in entities.items():
            page = self.owner(i)
            if page not in self.pages:
                continue
            for ref in values(n.get('block/refs')):
                target = self.owner(ref)
                if target in self.pages and target != page:
                    self.backlinks[target].add(page)
            for match in REF.finditer(n.get('block/title', '')):
                target = self.owner(self.resolve(match[1] or match[2]))
                if target in self.pages and target != page:
                    self.backlinks[target].add(page)
            for tag in values(n.get('block/tags')):
                if tag in self.pages and tag != page:
                    self.tagged[tag].add(page)
        self.md = MarkdownIt('commonmark', {'html': False, 'breaks': True, 'highlight': self.code})
        self.md.enable('table').enable('strikethrough')
        # allow_digits=False: a `$` next to a digit is a dollar amount, not a formula
        # ("$5/month ... $15/month"), as in Pandoc; formulas like $x^2$ still render.
        self.md.use(dollarmath_plugin, renderer=self.math_html, allow_digits=False)
        self.md.inline.ruler.before('link', 'logseq_link', self.inline_link)
        self.md.inline.ruler.before('emphasis', 'logseq_highlight', highlight_rule)
        self.md.renderer.rules['logseq_ref'] = self.render_ref
        self.md.block.ruler.before('fence', 'logseq_callout', callout_rule,
                                   {'alt': ['paragraph', 'reference', 'blockquote', 'list']})
        self.md.renderer.rules['logseq_callout_open'] = render_callout_open
        self.md.renderer.rules['logseq_callout_close'] = render_callout_close
        self.md.renderer.rules['logseq_example'] = render_example
        self.md.renderer.rules['image'] = self.render_image
        original_link = self.md.renderer.rules.get('link_open')
        def link_open(tokens, idx, options, env):
            href = tokens[idx].attrGet('href') or ''
            href = self.link_url(href)
            tokens[idx].attrSet('href', href)
            if download_name(href) is not None:
                tokens[idx].attrSet('download', download_name(href))
            if self.is_external(href):
                tokens[idx].attrJoin('class', 'external')
                tokens[idx].attrSet('rel', 'noopener')
            if original_link:
                return original_link(tokens, idx, options, env)
            return self.md.renderer.renderToken(tokens, idx, options, env)
        self.md.renderer.rules['link_open'] = link_open

    def href(self, path):
        """Prefix a site-root-relative path (starting with '/') with base_path."""
        return self.base_path + path

    def record_link(self, eid):
        target = self.owner(eid)
        if self.current_page in self.pages and target in self.pages and self.current_page != target:
            self.page_links.add(tuple(sorted((self.current_page, target))))

    def _first_page_ref(self, block_eid):
        """The first page a top-level navigation-page block points at, via a ref, an embed link, or a [[ref]] in its title."""
        node = self.entities[block_eid]
        for ref in values(node.get('block/refs')):
            target = ref if ref in self.pages else self.owner(ref)
            if target in self.pages:
                return target
        link = node.get('block/link')
        if link is not None:
            target = link if link in self.pages else self.owner(link)
            if target in self.pages:
                return target
        for match in REF.finditer(node.get('block/title', '')):
            resolved = self.resolve(match[1] or match[2])
            target = resolved if resolved in self.pages else (self.owner(resolved) if resolved is not None else None)
            if target in self.pages:
                return target
        return None

    def _section_targets(self, nav_eid):
        """Pages a navigation page reaches via refs, embeds, or [[links]] at any depth."""
        targets = set()
        stack = list(self.children.get(nav_eid, ()))
        while stack:
            b = stack.pop()
            n = self.entities[b]
            stack.extend(self.children.get(b, ()))
            for ref in values(n.get('block/refs')):
                target = ref if ref in self.pages else self.owner(ref)
                if target in self.pages:
                    targets.add(target)
            link = n.get('block/link')
            if link is not None:
                target = link if link in self.pages else self.owner(link)
                if target in self.pages:
                    targets.add(target)
            for match in REF.finditer(n.get('block/title', '')):
                resolved = self.resolve(match[1] or match[2])
                target = resolved if resolved in self.pages else (self.owner(resolved) if resolved is not None else None)
                if target in self.pages:
                    targets.add(target)
        targets.discard(nav_eid)
        return targets

    def _build_urls(self):
        if self.url_style != 'sections':
            urls = {}
            for i, n in self.pages.items():
                slug = slugify(n['block/title'])
                urls[i] = '/' if i == self.home else '/page/' + quote(slug + '--' + n['block/uuid'], safe='-') + '/'
            return urls
        nav_index = {eid: idx for idx, eid in enumerate(self.nav_ids)}

        def sort_key(i):
            if i == self.home:
                return (0, 0, '')
            if i in nav_index:
                return (1, nav_index[i], '')
            if i in self.sections:
                return (2, nav_index[self.sections[i]], self.pages[i]['block/title'].casefold())
            return (3, 0, self.pages[i]['block/title'].casefold())

        urls = {}
        taken = set()
        for i in sorted(self.pages, key=sort_key):
            n = self.pages[i]
            if i == self.home:
                urls[i] = '/'
                taken.add('/')
                continue
            slug = slugify(n['block/title'])
            if i in nav_index:
                path = '/' + quote(slug, safe='-') + '/'
                collided = path in taken or slug in RESERVED_TOP_LEVEL_SLUGS
                nested = None
            elif i in self.sections:
                section_slug = slugify(self.pages[self.sections[i]]['block/title'])
                path = '/' + quote(section_slug, safe='-') + '/' + quote(slug, safe='-') + '/'
                collided = path in taken
                nested = section_slug
            else:
                path = '/' + quote(slug, safe='-') + '/'
                collided = path in taken or slug in RESERVED_TOP_LEVEL_SLUGS
                nested = None
            if collided:
                fallback_slug = quote(slug + '--' + n['block/uuid'], safe='-')
                path = '/' + fallback_slug + '/' if nested is None else '/' + quote(nested, safe='-') + '/' + fallback_slug + '/'
                self.warnings.add(f'URL collision resolved with UUID suffix: {path}')
            taken.add(path)
            urls[i] = path
        return urls

    def graph_data(self):
        ids = sorted(self.pages, key=lambda i: (self.label(i).casefold(), i))
        indices = {eid: idx for idx, eid in enumerate(ids)}
        edges = set(self.page_links)
        for target, sources in self.backlinks.items():
            edges.update(tuple(sorted((source, target))) for source in sources)
        for target, sources in self.tagged.items():
            edges.update(tuple(sorted((source, target))) for source in sources)
        for eid, page in self.pages.items():
            parent = page.get('block/parent')
            if parent in self.pages and parent != eid:
                edges.add(tuple(sorted((eid, parent))))
        links = sorted((min(indices[a], indices[b]), max(indices[a], indices[b]))
                       for a, b in edges if a in indices and b in indices and a != b)
        return {'nodes': [{'id': self.pages[i]['block/uuid'], 'title': self.label(i), 'url': self.href(self.urls[i]),
                           'kind': 'home' if i == self.home else 'tag' if self.tagged[i] else 'page'}
                          for i in ids], 'links': links}

    def resolve(self, name):
        name = unquote(str(name)).strip()
        return self.uuids.get(name, self.names.get(name.casefold()))

    def owner(self, eid):
        if eid in self.pages:
            return eid
        return self.entities.get(eid, {}).get('block/page')

    def url(self, eid):
        node = self.entities.get(eid, {})
        if 'logseq.property.asset/type' in node:
            return self.link_url(node.get('logseq.property.asset/external-url') or f'assets/{node["block/uuid"]}.{node["logseq.property.asset/type"]}')
        if 'logseq.property/asset' in node:
            asset = self.url(node['logseq.property/asset'])
            return asset + '#page=' + str(node.get('logseq.property.pdf/hl-page', 1)) if asset else None
        owner = self.owner(eid)
        if owner not in self.urls:
            return None
        return self.href(self.urls[owner]) + ('' if eid == owner else '#block-' + self.entities[eid]['block/uuid'])

    def label(self, eid, seen=()):
        if eid in seen:
            return 'Circular reference'
        title = self.entities.get(eid, {}).get('block/title', 'Unavailable reference')
        return REF.sub(lambda m: self.label(self.resolve(m[1] or m[2]), (*seen, eid)), title)

    def inline_link(self, state, silent):
        rest = state.src[state.pos:]
        match = re.match(r'(?:#)?(?:\[\[([^\]]+)\]\]|\(\(([^)]+)\)\))', rest)
        label = None
        if match:
            target = match[1] or match[2]
        else:
            # [alias](Page), [alias]([[Page]]) and [alias](((block))). Logseq permits spaces in
            # page destinations, unlike CommonMark. A bracketed target that is not published
            # still becomes a reference (shown as plain text) rather than a broken link.
            match = re.match(r'\[([^\]\n]+)\]\((?:\[\[([^\]\n]+)\]\]|\(\(([^)\n]+)\)\)|([^)\n]+))\)', rest)
            if not match:
                return False
            label, target = match[1], match[2] or match[3]
            if target is None:
                if self.resolve(match[4]) is None:
                    return False
                target = match[4]
        if not silent:
            token = state.push('logseq_ref', '', 0)
            token.meta = {'target': target, 'label': label}
        state.pos += len(match[0])
        return True

    def render_ref(self, tokens, idx, options, env):
        meta = tokens[idx].meta
        eid = self.resolve(meta['target'])
        if eid is not None and 'logseq.property.asset/type' in self.entities[eid] and meta['label'] is None:
            return self.asset_html(eid).replace('<figure>', '').replace('</figure>', '')
        label = meta['label'] or (self.label(eid) if eid is not None else meta['target'])
        if eid is None and UUID.fullmatch(label):
            label = 'Unavailable reference'
        href = self.url(eid)
        self.record_link(eid)
        if href:
            return f'<a class="page-ref" href="{escape(href, quote=True)}"{download_attribute(href)}>{escape(label)}</a>'
        self.warnings.add(f'Unresolved reference: {meta["target"]}')
        return f'<span class="unavailable">{escape(label)}</span>'

    def asset_url(self, raw):
        path = unquote(urlsplit(raw).path).replace('\\', '/')
        if path.startswith('../assets/'):
            path = path[3:]
        path = path.lstrip('/')
        if not path.startswith('assets/'):
            return None
        candidate = (self.source / path).resolve()
        if not candidate.is_relative_to(self.source.resolve() / 'assets'):
            raise ValueError(f'Asset path escapes assets directory: {raw}')
        path = candidate.relative_to(self.source.resolve()).as_posix()
        if candidate.is_file():
            self.assets.add(path)
        else:
            self.warnings.add(f'Missing asset: {path}')
        return self.href('/' + quote(published_asset_path(path).as_posix(), safe='/')) + (('#' + urlsplit(raw).fragment) if urlsplit(raw).fragment else '')

    def is_external(self, href):
        parts = urlsplit(href)
        if parts.scheme.lower() not in ('http', 'https'):
            return False
        own_host = urlsplit(self.config.get('url') or '').netloc.lower()
        if own_host.startswith('www.'):
            own_host = own_host[4:]
        if own_host in ('', 'example.com'):
            own_host = None
        host = parts.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        return own_host is None or host != own_host

    def external_attrs(self, href):
        return ' class="external" rel="noopener"' if self.is_external(href) else ''

    def link_url(self, raw):
        parts = urlsplit(raw)
        if parts.scheme or raw.startswith('//'):
            if parts.scheme and parts.scheme.lower() not in ('http', 'https', 'mailto', 'tel'):
                self.warnings.add(f'Unsupported link scheme: {parts.scheme}')
                return '#'
            return raw
        if raw.startswith('#/page/'):
            eid = self.resolve(raw[len('#/page/'):])
            return self.url(eid) or self.href('/pages/')
        asset = self.asset_url(raw)
        if asset:
            return asset
        eid = self.resolve(raw)
        if eid is not None:
            self.record_link(eid)
            return self.url(eid) or self.href('/pages/')
        if raw.startswith('#'):
            return raw
        self.warnings.add(f'Page link absent from export: {raw}')
        return self.href('/pages/?q=' + quote(raw))

    def render_image(self, tokens, idx, options, env):
        token = tokens[idx]
        src = self.link_url(token.attrGet('src') or '')
        alt = token.content
        video = video_embed(src, alt, local=True)
        if video is not None:
            return video
        if download_name(src) is not None:
            return f'<a class="attachment" href="{escape(src, quote=True)}"{download_attribute(src)}>{escape(alt or download_name(src))}</a>'
        return f'<img src="{escape(src, quote=True)}" alt="{escape(alt, quote=True)}" loading="lazy" decoding="async">'

    def code(self, text, lang='', attrs=''):
        try:
            lexer = get_lexer_by_name(lang or 'text')
        except ClassNotFound:
            lexer = TextLexer()
        return highlight(text, lexer, HtmlFormatter(nowrap=True))

    def math_html(self, text, options):
        key = (text, options.get('display_mode', False))
        if key not in self.math:
            self.math[key] = len(self.math)
        return f'<!--GARDEN_MATH_{self.math[key]}-->'

    def asset_html(self, eid):
        n = self.entities[eid]
        ext = n['logseq.property.asset/type'].lower()
        raw = n.get('logseq.property.asset/external-url') or f'assets/{n["block/uuid"]}.{ext}'
        src = self.link_url(raw)
        title = escape(n.get('block/title', 'Attachment'))
        href = escape(src, quote=True)
        if ext in ('png','jpg','jpeg','webp','gif','avif') and download_name(src) is None:
            dims = ''.join(f' {a}="{int(n[k])}"' for a, k in [('width','logseq.property.asset/width'),('height','logseq.property.asset/height')] if n.get(k))
            return f'<figure><a href="{href}"><img src="{href}" alt="{title}"{dims} loading="lazy" decoding="async"></a></figure>'
        if ext in ('mp3', 'ogg', 'wav', 'm4a', 'flac'):
            return f'<audio controls preload="none" src="{href}"></audio><p><a href="{href}">{title}</a></p>'
        if ext in ('mp4', 'webm', 'mov'):
            return f'<video controls preload="none" src="{href}"></video><p><a href="{href}">{title}</a></p>'
        return f'<p class="attachment"><a href="{href}"{download_attribute(src)}>↗ {title} <small>{escape(ext.upper())}</small></a></p>'

    def properties(self, n):
        props = []
        for key, value in n.items():
            if key in self.hidden_properties:
                continue
            if not key.startswith('user.property/') and key not in ('logseq.property/status', 'logseq.property/priority', 'logseq.property/deadline', 'logseq.property/scheduled'):
                continue
            prop = self.property_entities.get(key, {})
            label = escape(prop.get('block/title', key.split('/')[-1]))
            items = []
            for v in values(value):
                is_ref = prop.get('db/valueType') == 'db.type/ref'
                if is_ref and isinstance(v, int) and v in self.entities:
                    label_text = self.label(v)
                    href = self.url(v)
                    if not href and re.fullmatch(r'https?://\S+', label_text):
                        href = self.link_url(label_text)
                    items.append(f'<a href="{escape(href)}"{download_attribute(href)}{self.external_attrs(href)}>{escape(label_text)}</a>' if href else self.md.renderInline(label_text))
                elif isinstance(v, str):
                    if re.fullmatch(r'https?://\S+', v):
                        url = self.link_url(v)
                        items.append(f'<a href="{escape(url)}"{self.external_attrs(url)}>{escape(v)}</a>')
                    else:
                        items.append(self.md.renderInline(v))
                else:
                    items.append(escape(str(v)))
            props.append(f'<dt>{label}</dt><dd>{", ".join(items)}</dd>')
        return '<dl class="properties">' + ''.join(props) + '</dl>' if props else ''

    def block_color(self, n):
        """Name of the block's background color, when it is one Logseq offers."""
        for value in values(n.get('logseq.property/background-color')):
            name = str(self.entities.get(value, {}).get('block/title', '')).strip().casefold()
            if name in BLOCK_COLORS:
                return name
        return None

    def is_numbered(self, eid):
        """True for a block that Logseq shows as an item of a numbered list."""
        value = self.entities.get(eid, {}).get('logseq.property/order-list-type')
        # The value is a block titled "number". Should it not be in the export,
        # "number" is still the only list type Logseq offers.
        return value is not None and any(
            str(self.entities.get(v, {}).get('block/title', 'number')).casefold() == 'number'
            for v in values(value))

    def list_style(self, eid):
        """Index into LIST_STYLES: 0 for an outermost numbered list, then one more
        for each numbered block the list is nested inside."""
        depth, seen = 0, {eid}
        parent = self.entities[eid].get('block/parent')
        while parent not in seen and self.is_numbered(parent):
            seen.add(parent)
            depth += 1
            parent = self.entities[parent].get('block/parent')
        return depth % len(LIST_STYLES)

    def outline(self, items, classes='outline'):
        """Lay blocks out as lists: a <ul> for plain blocks and an <ol> for each run
        of numbered ones. A run is neighbouring numbered blocks of one list style;
        it counts from 1 and starts over after any plain block, as in Logseq.
        `items` come from `blocks()`: ('block', eid, ancestors) or ('html', li)."""
        groups = []  # (list style or None, [rendered <li>, ...])
        for item in items:
            numbered = item[0] == 'block' and self.is_numbered(item[1])
            style = self.list_style(item[1]) if numbered else None
            if not groups or groups[-1][0] != style:
                groups.append((style, []))
            parts = groups[-1][1]
            if item[0] != 'block':
                parts.append(item[1])
            else:
                marker = LIST_STYLES[style][1](len(parts) + 1) if numbered else None
                parts.append(self.block(item[1], item[2], marker))
        if not groups:
            return f'<ul class="{classes}"></ul>'
        lists = []
        for position, (style, parts) in enumerate(groups):
            cls = classes if position == 0 else classes + ' continued'
            if style is None:
                lists.append(f'<ul class="{cls}">{"".join(parts)}</ul>')
            else:
                lists.append(f'<ol class="{cls}" type="{LIST_STYLES[style][0]}">{"".join(parts)}</ol>')
        return ''.join(lists)

    def block(self, eid, ancestors=(), marker=None):
        """One block as an <li>. `marker` is its number in a numbered list, as shown
        ('1', 'b', 'iii'), or None for a plain bullet."""
        if eid in ancestors:
            raise ValueError(f'Cycle in block tree: {eid}')
        n = self.entities[eid]
        self.rendered_ids.add(eid)
        text = n.get('block/title', '')
        display = n.get('logseq.property.node/display-type')
        segments = video_segments(text) if display not in ('code', 'math') and 'logseq.property.asset/type' not in n else None
        remaining = '\n'.join(part for kind, part in segments if kind == 'markdown') if segments else text
        if '{{' in remaining or re.search(r'^#\+BEGIN_(?:QUERY|SRC)', remaining, re.I | re.M):
            self.warnings.add(f'Macro/query retained as source: {n["block/uuid"]}')
        if 'logseq.property.asset/type' in n:
            body = self.asset_html(eid)
        elif display == 'code':
            body = '<pre><code>' + self.code(text, n.get('logseq.property.code/lang', '')) + '</code></pre>'
        elif display == 'math':
            body = '<div class="math-block">' + self.math_html(text, {'display_mode': True}) + '</div>'
        else:
            body = ''.join(part if kind == 'video' else self.md.render(part) for kind, part in segments) if segments else self.md.render(text)
            heading = n.get('logseq.property/heading')
            if heading and segments is None:
                level = max(2, min(6, int(heading)))
                body = f'<h{level}>' + self.md.renderInline(text) + f'</h{level}>'
            if display == 'quote':
                body = '<blockquote>' + body + '</blockquote>'
        link = n.get('block/link')
        if link is not None and not text.strip():
            body = self.embed(link, (*ancestors, eid))
        if n.get('logseq.property/query'):
            self.warnings.add(f'Dynamic query preserved as source: {n["block/uuid"]} ({text})')
            body += '<p class="muted">Query source</p>'
        body += self.properties(n)
        children = self.children[eid]
        nested = self.outline(self.blocks(children, (*ancestors, eid))) if children else ''
        color = self.block_color(n)
        body_class = 'block-body' + (f' has-bg bg-{color}' if color else '')
        anchor = 'block-' + n['block/uuid']
        if marker is None:
            permalink = f'<a class="bullet" href="#{anchor}" aria-label="Link to this block">•</a>'
        else:
            permalink = (f'<a class="list-number" href="#{anchor}" '
                         f'aria-label="Link to this block, item {marker}">{marker}.</a>')
        if children:
            opened = '' if n.get('block/collapsed?') else ' open'
            content = f'<details{opened}><summary><div class="{body_class}">{body}</div></summary>{nested}</details>'
        else:
            content = f'<div class="{body_class}">' + body + '</div>'
        classes = 'block' if marker is None else 'block numbered'
        return f'<li class="{classes}" id="{anchor}">{permalink}{content}</li>'

    def blocks(self, ids, ancestors):
        """What to show for these block ids, as items for `outline`. With
        embed_style 'inline', an embed is replaced by what it points at."""
        if self.config.get('embed_style', 'boxed') != 'inline':
            return [('block', c, ancestors) for c in ids]
        items = []
        for c in ids:
            n = self.entities[c]
            link = n.get('block/link')
            text = n.get('block/title', '')
            if link is None or text.strip():
                items.append(('block', c, ancestors))
                continue
            target = link
            if target in ancestors:
                self.rendered_ids.add(c)
                self.warnings.add(f'Recursive embed skipped: {n.get("block/uuid")}')
                items.append(('html', '<li class="block muted">Recursive embed</li>'))
            elif target in self.pages:
                self.rendered_ids.add(c)
                items.extend(self.blocks(self.children[target], (*ancestors, c, target)))
            elif target in self.entities and 'block/uuid' in self.entities[target] and 'block/name' not in self.entities[target]:
                self.rendered_ids.add(c)
                items.append(('block', target, ancestors))
            else:
                items.append(('block', c, ancestors))
        return items

    def embed(self, target, ancestors):
        # DB graphs store {{embed}} as an empty block whose block/link points at a page or block.
        if target in ancestors:
            self.warnings.add(f'Recursive embed skipped: {self.entities[target].get("block/uuid")}')
            return '<p class="muted">Recursive embed</p>'
        if target in self.pages:
            inner = self.outline([('block', c, ancestors) for c in self.children[target]])
            if not self.config.get('embed_titles', True):
                return f'<div class="embed page-embed">{inner}</div>'
            title = f'<a class="page-ref" href="{escape(self.href(self.urls[target]))}">{escape(self.label(target))}</a>'
            return f'<div class="embed page-embed"><div class="embed-title">{title}</div>{inner}</div>'
        if target in self.entities and 'block/uuid' in self.entities[target] and 'block/name' not in self.entities[target]:
            return '<div class="embed block-embed">' + self.outline([('block', target, ancestors)]) + '</div>'
        self.warnings.add(f'Embed target not exported: {target}')
        return '<p class="muted">Embedded content is not public</p>'

    def crumbs(self, eid):
        """Home > Parent > ... > Current trail, walking block/parent page ancestry."""
        if eid == self.home:
            return [(self.config['title'], None)]
        if eid in self.nav_ids:
            return [('Home', self.href('/')), (self.label(eid), None)]
        if self.url_style == 'sections':
            section = self.sections.get(eid)
            if section is not None:
                return [('Home', self.href('/')), (self.label(section), self.href(self.urls[section])), (self.label(eid), None)]
            return [('Home', self.href('/')), (self.label(eid), None)]
        trail = []
        seen = set()
        cur = eid
        while cur in self.pages and cur not in seen and cur != self.home:
            seen.add(cur)
            trail.append(cur)
            parent = self.pages[cur].get('block/parent')
            cur = parent if parent in self.pages else None
        trail.reverse()
        crumbs = [('Home', self.href('/'))]
        for i in trail[:-1]:
            crumbs.append((self.label(i), self.href(self.urls[i])))
        crumbs.append((self.label(trail[-1]), None))
        return crumbs

    def page_list(self, ids):
        return '<ul class="page-list">' + ''.join(f'<li><a href="{escape(self.href(self.urls[i]))}">{escape(self.label(i))}</a></li>' for i in sorted(ids, key=lambda i: self.label(i).casefold())) + '</ul>'

    def page_content(self, eid):
        self.current_page = eid
        n = self.pages[eid]
        children = self.children[eid]
        # Include exported orphan roots, so missing parents cannot silently lose content.
        orphans = [i for i, b in self.entities.items() if b.get('block/page') == eid and b.get('block/parent') not in self.entities and i not in children]
        body = self.properties(n) + self.outline(self.blocks(children + orphans, (eid,)), 'outline root-outline')
        child_pages = {i for i, page in self.pages.items() if page.get('block/parent') == eid}
        if child_pages:
            body += f'<section class="connections"><h2>Pages within</h2>{self.page_list(child_pages)}</section>'
        tagged = self.tagged[eid]
        if tagged:
            body += f'<section class="connections"><h2>In this collection <span>{len(tagged)}</span></h2>{self.page_list(tagged)}</section>'
        backlinks = self.backlinks[eid] - tagged
        if backlinks:
            body += f'<section class="connections"><details><summary><h2>Linked references <span>{len(backlinks)}</span></h2></summary>{self.page_list(backlinks)}</details></section>'
        if not children and not tagged and not backlinks:
            body += '<p class="muted">A seed in the garden. More notes to come.</p>'
        body += f'<p><a class="graph-local" href="{self.href("/graph/?page=" + n["block/uuid"])}">◌ Explore connections in Graph view</a></p>'
        if eid == self.home:
            body = body.replace('loading="lazy"', 'loading="eager" fetchpriority="high"', 1)
        return body

    def shell(self, title, body, url, css, js, *, home=False, description='', date='',
              extra_head='', theme_js='', crumbs=None):
        if crumbs is None:
            crumbs = [('Home', self.href('/')), (title, None)]
        crumb_items = []
        for label, href in crumbs:
            if href is not None:
                crumb_items.append(f'<li><a href="{escape(href)}">{escape(label)}</a></li>')
            else:
                crumb_items.append(f'<li aria-current="page">{escape(label)}</li>')
        breadcrumb = f'<nav class="crumbs" aria-label="Breadcrumb"><ol>{"".join(crumb_items)}</ol></nav>'
        nav = []
        for eid in self.nav_ids:
            if eid in self.urls:
                label = self.label(eid)
                current = ' aria-current="page"' if url == self.urls[eid] else ''
                nav.append(f'<a href="{escape(self.href(self.urls[eid]))}"{current}><span class="nav-dot">◦</span>{escape(label)}</a>')
        site = escape(self.config['title'])
        canonical = self.config.get('url', '').rstrip('/') + self.href(url)
        desc = escape(description or self.config['description'], quote=True)
        meta = f'<p class="page-meta">Updated {escape(date)}</p>' if date else ''
        logo_hash = sha256((self.branding / 'logo.svg').read_bytes()).hexdigest()[:12]
        document_title = site if home else escape(title) + ' · ' + site
        author = self.config.get('author')
        license_config = self.config.get('license')
        license_name, license_url = None, None
        if isinstance(license_config, dict):
            license_name, license_url = license_config.get('name'), license_config.get('url')
        elif license_config:
            license_name = license_config
            license_url = LICENSE_URLS.get(license_name)
        note_parts = []
        if license_name:
            license_html = escape(license_name)
            if license_url:
                license_html = f'<a href="{escape(license_url, quote=True)}" rel="license">{license_html}</a>'
            note_parts.append(license_html)
        if author:
            note_parts.append(escape(author))
        sidebar_note = f'<div class="sidebar-note">{" · ".join(note_parts)}</div>' if note_parts else ''
        icons = {
            'Home': '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
            'Pages': '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
            'Search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
            'Graph': '<path d="m7 7 9 3M6 8l4 9m6-5-4 5"/><circle cx="5" cy="5" r="3"/><circle cx="19" cy="11" r="3"/><circle cx="11" cy="20" r="2"/>',
            'Explore': '<path d="M4 7h16M4 12h16M4 17h16"/>',
        }
        def icon(label):
            return f'<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icons[label]}</svg>'
        mobile_links = ''
        for label, raw_href in [('Home', '/'), ('Pages', '/pages/'), ('Search', '/pages/#search'), ('Graph', '/graph/')]:
            current = ' aria-current="page"' if url == raw_href else ''
            mobile_links += f'<a href="{escape(self.href(raw_href))}"{current}>{icon(label)}<span>{label}</span></a>'
        mobile_nav = f'''<nav class="mobile-nav" aria-label="Mobile navigation">{mobile_links}
<details class="mobile-explore"><summary>{icon('Explore')}<span>Explore</span></summary>
<div class="mobile-explore-panel"><p>Paths through the garden</p>{''.join(nav)}<a href="{escape(self.href("/licenses/"))}">Licenses</a></div></details></nav>'''
        return f'''<!doctype html>
<html lang="{escape(self.config.get('language', 'en'))}" data-base="{escape(self.base_path)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{document_title}</title><meta name="description" content="{desc}">
<link rel="canonical" href="{escape(canonical, quote=True)}"><meta property="og:title" content="{escape(title, quote=True)}"><meta property="og:description" content="{desc}"><meta property="og:type" content="website"><meta property="og:url" content="{escape(canonical, quote=True)}">
<link rel="icon" type="image/svg+xml" href="{escape(self.href(f'/site/logo-{logo_hash}.svg'), quote=True)}"><link rel="icon" type="image/png" sizes="512x512" href="{escape(self.href(f'/static/img/logo.png?v={logo_hash}'), quote=True)}"><link rel="apple-touch-icon" href="{escape(self.href(f'/static/img/logo.png?v={logo_hash}'), quote=True)}"><meta property="og:image" content="{escape(self.config.get('url', '').rstrip('/') + self.href('/static/img/logo.png'), quote=True)}"><script src="{escape(self.href(theme_js), quote=True)}"></script><link rel="stylesheet" href="{escape(self.href(css), quote=True)}"><script src="{escape(self.href(js), quote=True)}" defer></script>{extra_head}</head>
<body><a class="skip" href="#content">Skip to content</a>
<aside class="sidebar"><a class="brand" href="{escape(self.href('/'))}"><span>{site}</span></a>
<details class="site-nav" open><summary>Explore</summary><nav aria-label="Main navigation"><a href="{escape(self.href('/'))}">⌂ &nbsp; Home</a><a href="{escape(self.href('/pages/'))}">▤ &nbsp; All pages</a><a href="{escape(self.href('/pages/#search'))}">⌕ &nbsp; Search</a><a href="{escape(self.href('/graph/'))}">◌ &nbsp; Graph view</a><p class="nav-label">{escape(self.config.get('navigation_label', 'Topics'))}</p>{''.join(nav)}</nav></details>
{sidebar_note}<button type="button" class="theme-toggle" id="theme-toggle" aria-pressed="false"><span class="theme-toggle-icon" aria-hidden="true">☾</span><span class="theme-toggle-label">Dark</span></button></aside>
<div class="workspace"><header class="topbar">{breadcrumb}<a href="{escape(self.href('/pages/#search'))}" aria-label="Search the garden">⌕ <span>Find a note</span></a></header>
<main id="content" class="{'graph-page' if url == '/graph/' else 'home' if home else 'note'}"><h1>{escape(title)}</h1>{meta}{body}</main>
</div>{mobile_nav}</body></html>'''


def load_base_css(config):
    theme_css_path = config.get('theme_css')
    if theme_css_path:
        theme_css_path = Path(theme_css_path)
        if not theme_css_path.is_file():
            raise FileNotFoundError(f'theme_css not found: {theme_css_path}')
        return theme_css_path.read_text()
    return (HERE / 'garden.css').read_text()


def render_math(garden):
    if not garden.math:
        return []
    katex = HERE.parent / 'vendor/katex/katex.min.js'
    if not katex.is_file():
        raise ValueError(f'KaTeX build renderer missing: {katex}')
    script = """const fs = require('fs'); const k = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(input.map(([s, display]) => {
 try { return {html:k.renderToString(s,{output:'mathml',displayMode:display,throwOnError:true,trust:false,maxExpand:1000})}; }
 catch(e) { return {error:e.message}; }
})));"""
    result = subprocess.run(['node', '-e', script, str(katex.resolve())], input=json.dumps(list(garden.math)), text=True, capture_output=True, check=True)
    rendered = json.loads(result.stdout)
    output = []
    for (source, _), value in zip(garden.math, rendered):
        if 'error' in value:
            garden.warnings.add('Math retained as source: ' + value['error'][:180])
            output.append('<code class="math-source">' + escape(source) + '</code>')
        else:
            output.append(value['html'])
    return output


def build(source, output, config):
    source, output = source.resolve(), output.resolve()
    if output == source or source.is_relative_to(output) or HERE.parent.is_relative_to(output):
        raise ValueError('Output must not replace or contain the original export')
    if output.exists() and any(output.iterdir()) and not (output / '.static-garden-build').is_file():
        raise ValueError('Refusing to replace a non-generated output directory')
    project = HERE.parent
    required_notices = ['LICENSE.md', 'THIRD_PARTY_NOTICES.md', 'licenses/MIT.txt',
                        'licenses/pygments/LICENSE.txt',
                        'licenses/katex/LICENSE.txt', 'licenses/sources.json']
    for notice in required_notices:
        if not (project / notice).is_file():
            raise ValueError(f'Required license notice missing: {notice}')
    license_files = {p.relative_to(project).as_posix(): p.read_bytes()
                     for p in (project / 'licenses').rglob('*') if p.is_file()}
    license_files['licenses/PROJECT-LICENSE.md'] = (project / 'LICENSE.md').read_bytes()
    license_files['licenses/THIRD_PARTY_NOTICES.md'] = (project / 'THIRD_PARTY_NOTICES.md').read_bytes()
    # Preserve the publisher's notices alongside the exporter notices.
    if source != project:
        for path in (source / 'licenses').rglob('*'):
            if path.is_file():
                name = path.relative_to(source).as_posix()
                if name not in license_files:
                    license_files[name] = path.read_bytes()
                elif name == 'licenses/sources.json':
                    records = json.loads(license_files[name])
                    for record in json.loads(path.read_text()):
                        if record not in records:
                            records.append(record)
                    license_files[name] = (json.dumps(records, indent=2) + '\n').encode()
        for name, target in [('LICENSE.md', 'SITE-LICENSE.md'), ('THIRD_PARTY_NOTICES.md', 'SITE-NOTICES.md')]:
            if (source / name).is_file():
                license_files['licenses/' + target] = (source / name).read_bytes()
    entities = read_entities(load_export(source / 'index.html'))
    garden = Garden(entities, source, config)
    css = load_base_css(config) + '\n' + HtmlFormatter(style='native').get_style_defs('pre') + LIST_CSS + COLOR_CSS + CALLOUT_CSS
    js = (HERE / 'garden.js').read_text()
    css_url = '/site/garden-' + sha256(css.encode()).hexdigest()[:12] + '.css'
    js_url = '/site/garden-' + sha256(js.encode()).hexdigest()[:12] + '.js'
    theme_js = (HERE / 'theme.js').read_text()
    theme_js_url = '/site/garden-' + sha256(theme_js.encode()).hexdigest()[:12] + '.theme.js'
    documents, search = {}, []
    for eid, n in garden.pages.items():
        title = garden.label(eid)
        body = garden.page_content(eid)
        date = datetime.fromtimestamp(n['block/updated-at'] / 1000, timezone.utc).strftime('%d %b %Y') if n.get('block/updated-at') else ''
        text = ' '.join(garden.label(i) for i, b in entities.items()
                        if b.get('block/page') == eid and i not in garden.property_values)
        description = config['description'] if eid == garden.home else strip_markdown(text)[:170]
        documents[garden.urls[eid]] = garden.shell(title, body, garden.urls[eid], css_url, js_url, home=eid == garden.home, description=description, date=date, theme_js=theme_js_url, crumbs=garden.crumbs(eid))
        search.append({'title':title, 'url':garden.href(garden.urls[eid]), 'text':text})
    expected = {i for i, b in entities.items() if b.get('block/page') in garden.pages and i not in garden.pages}
    missed = expected - garden.rendered_ids - garden.property_values
    if missed:
        raise ValueError(f'{len(missed)} exported blocks have not been rendered: {sorted(missed)[:20]}')
    search_body = f'''<p class="intro">{len(garden.pages)} notes, connected by curiosity.</p><label class="search-label" for="search">Find a page</label><input type="search" id="search" placeholder="Search titles and notes…" autocomplete="off"><p id="search-status" class="muted" role="status">Browse all pages below. Type to search.</p><div id="search-results">{garden.page_list(garden.pages)}</div>'''
    documents['/pages/'] = garden.shell('All pages', search_body, '/pages/', css_url, js_url, theme_js=theme_js_url, crumbs=[('Home', garden.href('/')), ('All pages', None)])
    graph = garden.graph_data()
    positioned = subprocess.run(['node', str(HERE / 'graph-layout.cjs')], input=json.dumps(graph),
                                text=True, capture_output=True, check=True).stdout
    graph_json = json.dumps(json.loads(positioned), ensure_ascii=False, separators=(',', ':'))
    graph_js = (HERE / 'graph.js').read_text()
    graph_css = (HERE / 'graph.css').read_text()
    graph_files = {}
    for extension, content in [('json', graph_json), ('js', graph_js), ('css', graph_css)]:
        name = '/site/graph-' + sha256(content.encode()).hexdigest()[:12] + '.' + extension
        graph_files[extension] = (name, content)
    graph_body = f'''<p class="graph-intro">Follow the threads between notes, projects, and ideas.</p>
<div class="graph-toolbar"><label class="graph-search-label" for="graph-search"><input id="graph-search" type="search" placeholder="Find a page in the graph…" aria-label="Find a page in the graph" autocomplete="off"></label>
<label><input type="checkbox" id="graph-local"> Selected + neighbors</label><label><input type="checkbox" id="graph-isolated" checked> Unlinked pages</label><label><input type="checkbox" id="graph-labels" checked> Labels</label>
</div><ul class="graph-results" id="graph-results" aria-label="Matching pages"></ul>
<div class="graph-actions"><button id="graph-zoom-in" type="button" aria-label="Zoom in">+</button><button id="graph-zoom-out" type="button" aria-label="Zoom out">−</button><button id="graph-fit" type="button">Fit graph</button><button id="graph-clear" type="button">Clear selection</button></div>
<p id="graph-status" class="graph-status" role="status">{len(graph['nodes'])} pages · {len(graph['links'])} connections · Loading graph…</p>
<div id="graph" class="graph-shell" data-source="{garden.href(graph_files['json'][0])}"><div class="graph-viewport"><canvas tabindex="0" role="img" aria-label="Graph of connected garden pages. Search for an accessible list of nodes. Arrow keys pan; plus and minus zoom; zero fits the graph."></canvas>
<div class="graph-legend"><span><i class="home-dot"></i>Home</span><span><i class="tag-dot"></i>Collections</span><span><i class="note-dot"></i>Notes</span></div><p class="graph-help">Drag to pan · Scroll or pinch to zoom · Select a dot to explore</p></div>
<aside id="graph-detail" class="graph-detail" aria-label="Selected page"><h2>Follow a connection</h2></aside></div>
<noscript><p>Enable JavaScript to explore the interactive graph. You can still <a href="{escape(garden.href('/pages/'))}">browse all pages</a> and follow linked references in each note.</p></noscript>'''
    documents['/graph/'] = garden.shell('Graph view', graph_body, '/graph/', css_url, js_url,
                                      extra_head=f'<link rel="stylesheet" href="{escape(garden.href(graph_files["css"][0]), quote=True)}"><script defer src="{escape(garden.href(graph_files["js"][0]), quote=True)}"></script>', theme_js=theme_js_url,
                                      crumbs=[('Home', garden.href('/')), ('Graph', None)])
    credits = f'''<p>The static exporter and its original browser code are licensed under the
<a href="{escape(garden.href('/licenses/MIT.txt'), quote=True)}">MIT License</a>, copyright 2026 Arney Nova.
Code highlighting includes Pygments stylesheet output under the
<a href="{escape(garden.href('/licenses/pygments/LICENSE.txt'), quote=True)}">BSD 2-Clause license</a>.</p>
<p>Notes, attachments, screenshots, and branding retain their own rights.
This is an unofficial project built for Logseq.</p>
<p>See the repository's <a href="{escape(garden.href('/licenses/PROJECT-LICENSE.md'), quote=True)}">license scope</a>
and <a href="{escape(garden.href('/licenses/THIRD_PARTY_NOTICES.md'), quote=True)}">third-party notices and source links</a>.
Publisher notices, when supplied, are listed separately below.</p>'''
    credits += '<ul>' + ''.join(f'<li><a href="{escape(garden.href("/" + quote(name, safe="/")), quote=True)}">{escape(name.removeprefix("licenses/"))}</a></li>'
                               for name in sorted(license_files)) + '</ul>'
    documents['/licenses/'] = garden.shell('Licenses', credits, '/licenses/', css_url, js_url, theme_js=theme_js_url, crumbs=[('Home', garden.href('/')), ('Licenses', None)])
    error_page = garden.shell('This path hasn’t grown yet.', f'<p>This page may have moved. <a href="{escape(garden.href("/pages/"), quote=True)}">Find it in the garden</a>.</p>', '/404.html', css_url, js_url, theme_js=theme_js_url, crumbs=[('Home', garden.href('/')), ('Not found', None)])
    equations = render_math(garden)
    pattern = re.compile(r'<!--GARDEN_MATH_(\d+)-->')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.garden-build-', dir=output.parent) as temporary:
        dest = Path(temporary)
        for url, html in documents.items():
            path = dest / unquote(url.lstrip('/')) / 'index.html'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pattern.sub(lambda m: equations[int(m[1])], html), encoding='utf-8')
        (dest / '404.html').write_text(error_page, encoding='utf-8')
        (dest / 'site').mkdir()
        (dest / css_url.lstrip('/')).write_text(css, encoding='utf-8')
        (dest / js_url.lstrip('/')).write_text(js, encoding='utf-8')
        (dest / theme_js_url.lstrip('/')).write_text(theme_js, encoding='utf-8')
        for name, content in graph_files.values():
            (dest / name.lstrip('/')).write_text(content, encoding='utf-8')
        for name, content in license_files.items():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (dest / 'site/search.json').write_text(json.dumps(search, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        routes = {n['block/uuid']: garden.href(garden.urls[i]) for i,n in garden.pages.items()}
        routes.update({name: garden.href(garden.urls[i]) for name,i in garden.names.items()})
        # Legacy Logseq /page/<block UUID> links also work.
        routes.update({entities[i]['block/uuid']: garden.url(i) for i in garden.rendered_ids if garden.url(i)})
        (dest / 'site/routes.json').write_text(json.dumps(routes, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        for asset in sorted(garden.assets):
            origin = source / asset
            if origin.is_file():
                if origin.stat().st_size > 25 * 1024 * 1024:
                    raise ValueError(f'Asset exceeds Cloudflare Pages 25 MiB limit: {asset}')
                target = dest / published_asset_path(asset)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, target)
        icon = garden.branding / 'logo.png'
        logo = garden.branding / 'logo.svg'
        logo_hash = sha256(logo.read_bytes()).hexdigest()[:12]
        (dest / 'static/img').mkdir(parents=True)
        shutil.copy2(icon, dest / 'static/img/logo.png')
        shutil.copy2(icon, dest / 'favicon.png')
        shutil.copy2(logo, dest / f'site/logo-{logo_hash}.svg')
        (dest / '_headers').write_text(HEADERS, encoding='utf-8')
        base = config.get('url', '').rstrip('/')
        sitemap = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join('<url><loc>' + escape(base + garden.href(url)) + '</loc></url>' for url in documents) + '</urlset>'
        (dest / 'sitemap.xml').write_text(sitemap, encoding='utf-8')
        (dest / 'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: ' + base + garden.base_path + '/sitemap.xml\n')
        (dest / '.static-garden-build').write_text('Generated by static-garden/build.py\n')
        report = {'pages':len(garden.pages), 'graph_nodes':len(graph['nodes']), 'graph_edges':len(graph['links']), 'graph_json_bytes':len(graph_json.encode()), 'graph_javascript_bytes':len(graph_js.encode()), 'blocks':len(garden.rendered_ids), 'assets':len(garden.assets), 'equations':len(equations), 'input_html_bytes':(source/'index.html').stat().st_size, 'homepage_html_bytes':(dest/'index.html').stat().st_size, 'css_bytes':len(css.encode()), 'javascript_bytes':len(js.encode()), 'warnings':sorted(garden.warnings)}
        # Build report lives next to the output, not inside the deployed website.
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(dest, output)
    (output.parent / (output.name + '-report.json')).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'warnings'}, indent=2))
    print(f'Warnings: {len(garden.warnings)} (see {output.name}-report.json)')
    print(f'Static site: {output}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    config_path = args.config or args.source / 'site.json'
    if not config_path.is_file() and args.config is None:
        config_path = HERE / 'site.json'
    config = json.loads(config_path.read_text())
    if config.get('theme_css'):
        config['theme_css'] = str((config_path.parent / config['theme_css']).resolve())
    build(args.source, args.output or args.source / 'dist', config)


if __name__ == '__main__':
    main()
