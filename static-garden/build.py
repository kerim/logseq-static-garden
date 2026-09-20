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
# Only these attachment formats can be opened on the site's origin. Everything
# else gets an inert extension and download-only response headers.
INLINE_ASSETS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.ico',
                 '.mp3', '.ogg', '.wav', '.m4a', '.flac', '.mp4', '.webm', '.mov', '.pdf'}
HEADERS = """/*
  Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https:; font-src 'self'; connect-src 'self'; media-src 'self' https:; frame-src https://www.youtube-nocookie.com; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
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


def video_html(text):
    """Render standalone YouTube macros from validated IDs, never arbitrary HTML."""
    macro = re.fullmatch(r'\s*\{\{video\s+(.+?)\}\}\s*', text)
    if not macro:
        return None
    raw = macro[1].strip()
    # Logseq accepts plain URLs; also accept a Markdown link as the argument.
    markdown_link = re.fullmatch(r'\[[^\]\n]*\]\(([^\s()]+)\)', raw)
    if markdown_link:
        raw = markdown_link[1]
    elif raw.startswith('<') and raw.endswith('>'):
        raw = raw[1:-1]
    try:
        parts = urlsplit(unescape(raw))
    except ValueError:
        return None
    hosts = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
             'youtube-nocookie.com', 'www.youtube-nocookie.com', 'youtu.be', 'www.youtu.be'}
    if parts.scheme not in ('http', 'https') or parts.netloc.lower() not in hosts:
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
    return (f'<figure class="video-embed"><iframe src="{escape(src, quote=True)}" '
            'title="YouTube video player" width="560" height="315" loading="lazy" '
            'referrerpolicy="strict-origin-when-cross-origin" '
            'allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe>'
            f'<figcaption><a href="{escape(watch, quote=True)}">Watch on YouTube ↗</a></figcaption></figure>')


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


class Garden:
    def __init__(self, entities, source, config):
        self.entities, self.source, self.config = entities, source, config
        self.branding = source / 'branding' if (source / 'branding').exists() else HERE / 'branding'
        for name in ('logo.svg', 'logo.png'):
            if not (self.branding / name).is_file():
                raise ValueError(f'Branding file missing: {self.branding / name}')
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
        self.home = self.resolve(config['home_page'])
        if self.home not in self.pages:
            raise ValueError(f'Home page not found: {config["home_page"]}')
        # UUID suffixes keep routes unambiguous when names or slugs collide.
        self.urls = {}
        for i, n in self.pages.items():
            slug = re.sub(r'[^\w-]+', '-', n['block/title'].casefold(), flags=re.UNICODE).strip('-')[:90] or 'page'
            self.urls[i] = '/' if i == self.home else '/page/' + quote(slug + '--' + n['block/uuid'], safe='-') + '/'
        self.property_entities = {n['db/ident']: n for n in entities.values() if 'db/ident' in n}
        self.children = defaultdict(list)
        for i, n in entities.items():
            parent = n.get('block/parent')
            if parent is not None and i not in self.pages:
                self.children[parent].append(i)
        for ids in self.children.values():
            ids.sort(key=lambda i: (entities[i].get('block/order', ''), i))
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
        self.md.use(dollarmath_plugin, renderer=self.math_html)
        self.md.inline.ruler.before('link', 'logseq_link', self.inline_link)
        self.md.renderer.rules['logseq_ref'] = self.render_ref
        self.md.renderer.rules['image'] = self.render_image
        original_link = self.md.renderer.rules.get('link_open')
        def link_open(tokens, idx, options, env):
            href = tokens[idx].attrGet('href') or ''
            href = self.link_url(href)
            tokens[idx].attrSet('href', href)
            if download_name(href) is not None:
                tokens[idx].attrSet('download', download_name(href))
            if original_link:
                return original_link(tokens, idx, options, env)
            return self.md.renderer.renderToken(tokens, idx, options, env)
        self.md.renderer.rules['link_open'] = link_open

    def record_link(self, eid):
        target = self.owner(eid)
        if self.current_page in self.pages and target in self.pages and self.current_page != target:
            self.page_links.add(tuple(sorted((self.current_page, target))))

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
        return {'nodes': [{'id': self.pages[i]['block/uuid'], 'title': self.label(i), 'url': self.urls[i],
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
        return self.urls[owner] + ('' if eid == owner else '#block-' + self.entities[eid]['block/uuid'])

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
            # Logseq permits spaces in page destinations, unlike CommonMark.
            match = re.match(r'\[([^\]\n]+)\]\(([^)\n]+)\)', rest)
            if not match or self.resolve(match[2]) is None:
                return False
            label, target = match[1], match[2]
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
        return '/' + quote(published_asset_path(path).as_posix(), safe='/') + (('#' + urlsplit(raw).fragment) if urlsplit(raw).fragment else '')

    def link_url(self, raw):
        parts = urlsplit(raw)
        if parts.scheme or raw.startswith('//'):
            if parts.scheme and parts.scheme.lower() not in ('http', 'https', 'mailto', 'tel'):
                self.warnings.add(f'Unsupported link scheme: {parts.scheme}')
                return '#'
            return raw
        if raw.startswith('#/page/'):
            eid = self.resolve(raw[len('#/page/'):])
            return self.url(eid) or '/pages/'
        asset = self.asset_url(raw)
        if asset:
            return asset
        eid = self.resolve(raw)
        if eid is not None:
            self.record_link(eid)
            return self.url(eid) or '/pages/'
        if raw.startswith('#'):
            return raw
        self.warnings.add(f'Page link absent from export: {raw}')
        return '/pages/?q=' + quote(raw)

    def render_image(self, tokens, idx, options, env):
        token = tokens[idx]
        src = self.link_url(token.attrGet('src') or '')
        alt = token.content
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
                    items.append(f'<a href="{escape(href)}"{download_attribute(href)}>{escape(label_text)}</a>' if href else self.md.renderInline(label_text))
                elif isinstance(v, str):
                    if re.fullmatch(r'https?://\S+', v):
                        items.append(f'<a href="{escape(self.link_url(v))}">{escape(v)}</a>')
                    else:
                        items.append(self.md.renderInline(v))
                else:
                    items.append(escape(str(v)))
            props.append(f'<dt>{label}</dt><dd>{", ".join(items)}</dd>')
        return '<dl class="properties">' + ''.join(props) + '</dl>' if props else ''

    def block(self, eid, ancestors=()):
        if eid in ancestors:
            raise ValueError(f'Cycle in block tree: {eid}')
        n = self.entities[eid]
        self.rendered_ids.add(eid)
        text = n.get('block/title', '')
        display = n.get('logseq.property.node/display-type')
        video = video_html(text) if display not in ('code', 'math') and 'logseq.property.asset/type' not in n else None
        if video is None and ('{{' in text or re.search(r'^#\+BEGIN_(?:QUERY|SRC)', text, re.I | re.M)):
            self.warnings.add(f'Macro/query retained as source: {n["block/uuid"]}')
        if 'logseq.property.asset/type' in n:
            body = self.asset_html(eid)
        elif display == 'code':
            body = '<pre><code>' + self.code(text, n.get('logseq.property.code/lang', '')) + '</code></pre>'
        elif display == 'math':
            body = '<div class="math-block">' + self.math_html(text, {'display_mode': True}) + '</div>'
        else:
            body = video if video is not None else self.md.render(text)
            heading = n.get('logseq.property/heading')
            if heading and video is None:
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
        nested = '<ul class="outline">' + self.blocks(children, (*ancestors, eid)) + '</ul>' if children else ''
        anchor = 'block-' + n['block/uuid']
        permalink = f'<a class="bullet" href="#{anchor}" aria-label="Link to this block">•</a>'
        if children:
            opened = '' if n.get('block/collapsed?') else ' open'
            content = f'<details{opened}><summary><div class="block-body">{body}</div></summary>{nested}</details>'
        else:
            content = '<div class="block-body">' + body + '</div>'
        return f'<li class="block" id="{anchor}">{permalink}{content}</li>'

    def blocks(self, ids, ancestors):
        if self.config.get('embed_style', 'boxed') != 'inline':
            return ''.join(self.block(c, ancestors) for c in ids)
        parts = []
        for c in ids:
            n = self.entities[c]
            link = n.get('block/link')
            text = n.get('block/title', '')
            if link is None or text.strip():
                parts.append(self.block(c, ancestors))
                continue
            target = link
            if target in ancestors:
                self.rendered_ids.add(c)
                self.warnings.add(f'Recursive embed skipped: {n.get("block/uuid")}')
                parts.append('<li class="block muted">Recursive embed</li>')
            elif target in self.pages:
                self.rendered_ids.add(c)
                parts.append(self.blocks(self.children[target], (*ancestors, c, target)))
            elif target in self.entities and 'block/uuid' in self.entities[target] and 'block/name' not in self.entities[target]:
                self.rendered_ids.add(c)
                parts.append(self.block(target, ancestors))
            else:
                parts.append(self.block(c, ancestors))
        return ''.join(parts)

    def embed(self, target, ancestors):
        # DB graphs store {{embed}} as an empty block whose block/link points at a page or block.
        if target in ancestors:
            self.warnings.add(f'Recursive embed skipped: {self.entities[target].get("block/uuid")}')
            return '<p class="muted">Recursive embed</p>'
        if target in self.pages:
            inner = '<ul class="outline">' + ''.join(self.block(c, ancestors) for c in self.children[target]) + '</ul>'
            if not self.config.get('embed_titles', True):
                return f'<div class="embed page-embed">{inner}</div>'
            title = f'<a class="page-ref" href="{escape(self.urls[target])}">{escape(self.label(target))}</a>'
            return f'<div class="embed page-embed"><div class="embed-title">{title}</div>{inner}</div>'
        if target in self.entities and 'block/uuid' in self.entities[target] and 'block/name' not in self.entities[target]:
            return '<div class="embed block-embed"><ul class="outline">' + self.block(target, ancestors) + '</ul></div>'
        self.warnings.add(f'Embed target not exported: {target}')
        return '<p class="muted">Embedded content is not public</p>'

    def page_list(self, ids):
        return '<ul class="page-list">' + ''.join(f'<li><a href="{escape(self.urls[i])}">{escape(self.label(i))}</a></li>' for i in sorted(ids, key=lambda i: self.label(i).casefold())) + '</ul>'

    def page_content(self, eid):
        self.current_page = eid
        n = self.pages[eid]
        children = self.children[eid]
        # Include exported orphan roots, so missing parents cannot silently lose content.
        orphans = [i for i, b in self.entities.items() if b.get('block/page') == eid and b.get('block/parent') not in self.entities and i not in children]
        body = self.properties(n) + '<ul class="outline root-outline">' + self.blocks(children + orphans, (eid,)) + '</ul>'
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
        body += f'<p><a class="graph-local" href="/graph/?page={n["block/uuid"]}">◌ Explore connections in Graph view</a></p>'
        if eid == self.home:
            body = body.replace('loading="lazy"', 'loading="eager" fetchpriority="high"', 1)
        return body

    def shell(self, title, body, url, css, js, *, home=False, description='', date='', extra_head='', theme_js=''):
        nav = []
        for label in self.config['navigation']:
            eid = self.resolve(label)
            if eid in self.urls:
                current = ' aria-current="page"' if url == self.urls[eid] else ''
                nav.append(f'<a href="{escape(self.urls[eid])}"{current}><span class="nav-dot">◦</span>{escape(label)}</a>')
        site = escape(self.config['title'])
        canonical = self.config.get('url', '').rstrip('/') + url
        desc = escape(description or self.config['description'], quote=True)
        subtitle = '<p class="eyebrow">A PERSONAL CORNER OF THE INTERNET</p>' if home else '<p class="eyebrow"><a href="/pages/">THE GARDEN</a></p>'
        meta = f'<p class="page-meta">Updated {escape(date)}</p>' if date else ''
        logo_hash = sha256((self.branding / 'logo.svg').read_bytes()).hexdigest()[:12]
        document_title = site + ' · Portfolio & Garden' if home else escape(title) + ' · ' + site
        icons = {
            'Home': '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
            'Pages': '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
            'Search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
            'Graph': '<path d="m7 7 9 3M6 8l4 9m6-5-4 5"/><circle cx="5" cy="5" r="3"/><circle cx="19" cy="11" r="3"/><circle cx="11" cy="20" r="2"/>',
            'Explore': '<path d="M4 7h16M4 12h16M4 17h16"/>',
        }
        def icon(label):
            return f'<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icons[label]}</svg>'
        mobile_links = ''.join(f'<a href="{href}"' + (' aria-current="page"' if url == href else '')
                               + f'>{icon(label)}<span>{label}</span></a>'
                               for label, href in [('Home', '/'), ('Pages', '/pages/'), ('Search', '/pages/#search'), ('Graph', '/graph/')])
        mobile_nav = f'''<nav class="mobile-nav" aria-label="Mobile navigation">{mobile_links}
<details class="mobile-explore"><summary>{icon('Explore')}<span>Explore</span></summary>
<div class="mobile-explore-panel"><p>Paths through the garden</p>{''.join(nav)}<a href="/licenses/">Licenses</a></div></details></nav>'''
        return f'''<!doctype html>
<html lang="{escape(self.config.get('language', 'en'))}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{document_title}</title><meta name="description" content="{desc}">
<link rel="canonical" href="{escape(canonical, quote=True)}"><meta property="og:title" content="{escape(title, quote=True)}"><meta property="og:description" content="{desc}"><meta property="og:type" content="website"><meta property="og:url" content="{escape(canonical, quote=True)}">
<link rel="icon" type="image/svg+xml" href="/site/logo-{logo_hash}.svg"><link rel="icon" type="image/png" sizes="512x512" href="/static/img/logo.png?v={logo_hash}"><link rel="apple-touch-icon" href="/static/img/logo.png?v={logo_hash}"><meta property="og:image" content="{escape(self.config.get('url', '').rstrip('/'), quote=True)}/static/img/logo.png"><script src="{theme_js}"></script><link rel="stylesheet" href="{css}"><script src="{js}" defer></script>{extra_head}</head>
<body><a class="skip" href="#content">Skip to content</a>
<aside class="sidebar"><a class="brand" href="/"><img class="brand-logo" src="/site/logo-{logo_hash}.svg" width="38" height="38" alt=""><span>{site}<small>portfolio & digital garden</small></span></a>
<details class="site-nav" open><summary>Explore</summary><nav aria-label="Main navigation"><a href="/">⌂ &nbsp; Home</a><a href="/pages/">▤ &nbsp; All pages</a><a href="/pages/#search">⌕ &nbsp; Search</a><a href="/graph/">◌ &nbsp; Graph view</a><p class="nav-label">PATHS THROUGH THE GARDEN</p>{''.join(nav)}</nav></details>
<div class="sidebar-note"><span class="status-dot"></span> Always growing.<p>Notes, things I make,<br>and things I’m figuring out.</p></div><button type="button" class="theme-toggle" id="theme-toggle" aria-pressed="false"><span class="theme-toggle-icon" aria-hidden="true">☾</span><span class="theme-toggle-label">Dark</span></button></aside>
<div class="workspace"><header class="topbar"><a href="/">{site}<span> / {('home' if home else 'garden')}</span></a><a href="/pages/#search" aria-label="Search the garden">⌕ <span>Find a note</span></a></header>
<main id="content" class="{'graph-page' if url == '/graph/' else 'home' if home else 'note'}">{subtitle}<h1>{escape(title)}</h1>{meta}{body}</main>
<footer>Made of curiosity. <a href="/licenses/">Licenses</a><a href="/pages/">Wander the garden ↗</a></footer></div>{mobile_nav}</body></html>'''


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
    css = (HERE / 'garden.css').read_text() + '\n' + HtmlFormatter(style='native').get_style_defs('pre')
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
        text = ' '.join(garden.label(i) for i, b in entities.items() if b.get('block/page') == eid)
        description = re.sub(r'\s+', ' ', text).strip()[:170]
        documents[garden.urls[eid]] = garden.shell(title, body, garden.urls[eid], css_url, js_url, home=eid == garden.home, description=description, date=date, theme_js=theme_js_url)
        search.append({'title':title, 'url':garden.urls[eid], 'text':text})
    expected = {i for i, b in entities.items() if b.get('block/page') in garden.pages and i not in garden.pages}
    missed = expected - garden.rendered_ids
    if missed:
        raise ValueError(f'{len(missed)} exported blocks have not been rendered: {sorted(missed)[:20]}')
    search_body = f'''<p class="intro">{len(garden.pages)} notes, connected by curiosity.</p><label class="search-label" for="search">Find a page</label><input type="search" id="search" placeholder="Search titles and notes…" autocomplete="off"><p id="search-status" class="muted" role="status">Browse all pages below. Type to search.</p><div id="search-results">{garden.page_list(garden.pages)}</div>'''
    documents['/pages/'] = garden.shell('All pages', search_body, '/pages/', css_url, js_url, theme_js=theme_js_url)
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
<div id="graph" class="graph-shell" data-source="{graph_files['json'][0]}"><div class="graph-viewport"><canvas tabindex="0" role="img" aria-label="Graph of connected garden pages. Search for an accessible list of nodes. Arrow keys pan; plus and minus zoom; zero fits the graph."></canvas>
<div class="graph-legend"><span><i class="home-dot"></i>Home</span><span><i class="tag-dot"></i>Collections</span><span><i class="note-dot"></i>Notes</span></div><p class="graph-help">Drag to pan · Scroll or pinch to zoom · Select a dot to explore</p></div>
<aside id="graph-detail" class="graph-detail" aria-label="Selected page"><h2>Follow a connection</h2></aside></div>
<noscript><p>Enable JavaScript to explore the interactive graph. You can still <a href="/pages/">browse all pages</a> and follow linked references in each note.</p></noscript>'''
    documents['/graph/'] = garden.shell('Graph view', graph_body, '/graph/', css_url, js_url,
                                      extra_head=f'<link rel="stylesheet" href="{graph_files["css"][0]}"><script defer src="{graph_files["js"][0]}"></script>', theme_js=theme_js_url)
    credits = '''<p>The static exporter and its original browser code are licensed under the
<a href="/licenses/MIT.txt">MIT License</a>, copyright 2026 Arney Nova.
Code highlighting includes Pygments stylesheet output under the
<a href="/licenses/pygments/LICENSE.txt">BSD 2-Clause license</a>.</p>
<p>Notes, attachments, screenshots, and branding retain their own rights.
This is an unofficial project built for Logseq.</p>
<p>See the repository's <a href="/licenses/PROJECT-LICENSE.md">license scope</a>
and <a href="/licenses/THIRD_PARTY_NOTICES.md">third-party notices and source links</a>.
Publisher notices, when supplied, are listed separately below.</p>'''
    credits += '<ul>' + ''.join(f'<li><a href="/{quote(name, safe="/")}">{escape(name.removeprefix("licenses/"))}</a></li>'
                               for name in sorted(license_files)) + '</ul>'
    documents['/licenses/'] = garden.shell('Licenses', credits, '/licenses/', css_url, js_url, theme_js=theme_js_url)
    error_page = garden.shell('This path hasn’t grown yet.', '<p>This page may have moved. <a href="/pages/">Find it in the garden</a>.</p>', '/404.html', css_url, js_url, theme_js=theme_js_url)
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
        routes = {n['block/uuid']: garden.urls[i] for i,n in garden.pages.items()}
        routes.update({name: garden.urls[i] for name,i in garden.names.items()})
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
        sitemap = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join('<url><loc>' + escape(base + url) + '</loc></url>' for url in documents) + '</urlset>'
        (dest / 'sitemap.xml').write_text(sitemap, encoding='utf-8')
        (dest / 'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: ' + base + '/sitemap.xml\n')
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
    build(args.source, args.output or args.source / 'dist', config)


if __name__ == '__main__':
    main()
