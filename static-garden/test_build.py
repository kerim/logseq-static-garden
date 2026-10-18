"""Regression tests for export decoding and publishing behavior."""
import re
import tempfile
import unittest
from pathlib import Path

from build import (HERE, LIST_CSS, Garden, alpha_marker, build, load_base_css, read_entities,
                   roman_marker, strip_markdown)
from transit_reader import Reader


def node(title, uuid, **extra):
    return {'block/title': title, 'block/uuid': uuid, **extra}


class TransitTests(unittest.TestCase):
    def test_cache_is_shared_across_map_keys_and_tagged_arrays(self):
        raw = ['^ ', '~:schema', ['^ ', '~:block/refs', ['^ ', '~:db/cardinality', '~:db.cardinality/many']],
               '~:datoms', [['~#datascript/Datom', [1, '^1', 2, 9]], ['^5', [1, '^1', 3, 9]]]]
        decoded = Reader().read(raw)
        self.assertEqual(read_entities(decoded), {1: {'block/refs': [2, 3]}})

    def test_cache_rolls_over_and_uses_base_44_indices(self):
        reader = Reader()
        for i in range(1936):
            reader.read('~:key-' + str(i))
        self.assertEqual(reader.read('^[['), 'key-1935')
        reader.read('~:replacement')
        self.assertEqual(reader.read('^0'), 'replacement')
        self.assertEqual(reader.read('^10'), 'key-44')

    def test_escaped_values_and_large_integer(self):
        self.assertEqual(Reader().read(['~~hello', '~^0', '~i9007199254740993']), ['~hello', '^0', 9007199254740993])

    def test_unknown_tags_fail(self):
        with self.assertRaises(ValueError):
            Reader().read(['~#unexpected/type', []])


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = ['11111111-1111-4111-8111-' + f'{i:012d}' for i in range(1, 8)]
        self.nodes = {
            1: node('Home', self.ids[0], **{'block/name':'home'}),
            2: node('Security', self.ids[1], **{'block/name':'security'}),
            3: node('Child', self.ids[2], **{'block/page':1,'block/parent':4,'block/order':'a1'}),
            4: node('Parent', self.ids[3], **{'block/page':1,'block/parent':1,'block/order':'a0','block/collapsed?':True}),
            5: node('Notes', self.ids[4], **{'block/name':'notes','block/tags':[2],'block/refs':[1]}),
            6: node('Hidden', self.ids[5], **{'block/name':'hidden','logseq.property/publishing-public?':False}),
            7: node('Internal', self.ids[6], **{'block/name':'internal','logseq.property/built-in?':True})}
        self.config = {'home_page':'Home', 'title':'My garden', 'navigation':['Security'], 'description':'Notes'}
        self.g = Garden(self.nodes, self.root, self.config)

    def test_public_page_filter_and_collections(self):
        self.assertEqual(set(self.g.pages), {1,2,5})
        self.assertEqual(self.g.tagged[2], {5})
        self.assertEqual(self.g.backlinks[1], {5})

    def test_outline_order_and_collapsed_state_are_native_html(self):
        content = self.g.page_content(1)
        self.assertIn('<details>', content)
        self.assertLess(content.index('Parent'), content.index('Child'))
        self.assertIn('id="block-' + self.ids[2], content)

    def test_uuid_and_space_containing_page_links(self):
        text = self.g.md.render('[[%s]] [my notes](Notes)' % self.ids[1])
        self.assertIn(self.g.urls[2], text)
        self.assertIn(self.g.urls[5], text)
        self.assertNotIn(self.ids[1] + ']]', text)

    def test_code_is_not_interpreted_as_page_links_or_html(self):
        content = self.g.md.render('`[[Home]]` <script>alert(1)</script>')
        self.assertIn('<code>[[Home]]</code>', content)
        self.assertNotIn('<script>', content)
        self.assertNotIn('page-ref', content)

    def test_asset_reference_and_pdf_annotation(self):
        asset = self.ids[5] + '.pdf'
        (self.root/'assets').mkdir()
        (self.root/'assets'/asset).write_bytes(b'example')
        self.nodes[6] = node('Paper', self.ids[5], **{'logseq.property.asset/type':'pdf'})
        self.nodes[7] = node('Highlight', self.ids[6], **{'logseq.property/asset':6,'logseq.property.pdf/hl-page':8})
        self.assertEqual(self.g.url(7), '/assets/' + asset + '#page=8')
        self.assertIn('assets/' + asset, self.g.assets)

    def test_comment_threads_are_not_published(self):
        # Logseq stores a comment thread as a block tagged Comments under the commented block,
        # holding one block tagged Comment per comment.
        uid = lambda i: '11111111-1111-4111-8111-%012d' % i
        self.nodes[20] = {'db/ident': 'logseq.class/Comments', 'block/uuid': uid(20), 'block/title': 'Comments'}
        self.nodes[21] = {'db/ident': 'logseq.class/Comment', 'block/uuid': uid(21), 'block/title': 'Comment'}
        self.nodes[22] = node('Comments', uid(22), **{'block/page':1,'block/parent':4,'block/order':'a0V','block/tags':[20],'logseq.property.comments/blocks':[4]})
        self.nodes[23] = node('Private remark about [[Notes]]', uid(23), **{'block/page':1,'block/parent':22,'block/order':'a0','block/tags':[21],'block/refs':[5]})
        self.nodes[24] = node('Reply', uid(24), **{'block/page':1,'block/parent':23,'block/order':'a0'})
        g = Garden(self.nodes, self.root, self.config)
        content = g.page_content(1)
        self.assertIn('Child', content)
        self.assertNotIn('Private remark', content)
        self.assertNotIn('Reply', content)
        self.assertNotIn(1, g.backlinks[5])
        self.assertFalse({22, 23, 24} & set(g.entities))

    def _with_properties(self):
        uid = lambda i: '11111111-1111-4111-8111-%012d' % i
        self.nodes[30] = node('FAQ Status', uid(30), **{'block/name':'faq status','db/ident':'user.property/faq-status','db/valueType':'db.type/string'})
        self.nodes[31] = node('Source', uid(31), **{'block/name':'source','db/ident':'user.property/source','db/valueType':'db.type/string'})
        self.nodes[5]['user.property/faq-status'] = 'Draft'
        self.nodes[5]['user.property/source'] = 'Manual'
        self.nodes[32] = node('Hidden Properties', uid(32), **{'block/name':'hidden properties'})
        self.nodes[33] = node('[[FAQ Status]]', uid(33), **{'block/page':32,'block/parent':32,'block/order':'a0','block/refs':[30]})

    def test_hidden_properties_page_hides_listed_properties(self):
        self._with_properties()
        g = Garden(self.nodes, self.root, {**self.config, 'hidden_properties_page':'Hidden Properties'})
        content = g.page_content(5)
        self.assertNotIn('Draft', content)
        self.assertNotIn('FAQ Status', content)
        self.assertIn('Manual', content)
        self.assertNotIn(30, g.pages)
        self.assertNotIn(32, g.pages)
        self.assertIn(31, g.pages)
        self.assertFalse(g.warnings)

    def test_properties_all_shown_without_hidden_properties_page(self):
        self._with_properties()
        g = Garden(self.nodes, self.root, self.config)
        content = g.page_content(5)
        self.assertIn('Draft', content)
        self.assertIn('Manual', content)
        self.assertIn(32, g.pages)

    def test_missing_hidden_properties_page_warns_and_shows_all(self):
        self._with_properties()
        g = Garden(self.nodes, self.root, {**self.config, 'hidden_properties_page':'Nope'})
        self.assertIn('Draft', g.page_content(5))
        self.assertTrue(any('hidden_properties_page' in w for w in g.warnings))

    def test_shell_has_no_footer(self):
        html = self.g.shell('Home', '', '/', '/a.css', '/a.js', theme_js='/t.js')
        self.assertNotIn('<footer', html)
        self.assertNotIn('Made of curiosity', html)

    def test_page_and_block_embeds_render_linked_content(self):
        # DB graphs store {{embed}} as an empty block whose block/link points at a page or block.
        self.nodes[8] = node('', '11111111-1111-4111-8111-000000000008', **{'block/page':1,'block/parent':1,'block/order':'a2','block/link':2,'block/refs':[2]})
        self.nodes[9] = node('Security detail', '11111111-1111-4111-8111-000000000009', **{'block/page':2,'block/parent':2,'block/order':'a0'})
        self.nodes[10] = node('', '11111111-1111-4111-8111-000000000010', **{'block/page':1,'block/parent':1,'block/order':'a3','block/link':9,'block/refs':[9]})
        self.nodes[11] = node('', '11111111-1111-4111-8111-000000000011', **{'block/page':1,'block/parent':1,'block/order':'a4','block/link':6,'block/refs':[6]})
        g = Garden(self.nodes, self.root, self.config)
        content = g.page_content(1)
        self.assertIn('class="embed page-embed"', content)
        self.assertIn(g.urls[2], content)
        self.assertEqual(content.count('Security detail'), 2)
        self.assertIn('class="embed block-embed"', content)
        self.assertIn('not public', content)

    def test_embed_titles_false_omits_embed_title(self):
        self.nodes[8] = node('', '11111111-1111-4111-8111-000000000008', **{'block/page':1,'block/parent':1,'block/order':'a2','block/link':2,'block/refs':[2]})
        config = {**self.config, 'embed_titles': False}
        g = Garden(self.nodes, self.root, config)
        content = g.page_content(1)
        self.assertIn('page-embed', content)
        self.assertNotIn('embed-title', content)

    def test_embed_titles_default_includes_embed_title(self):
        self.nodes[8] = node('', '11111111-1111-4111-8111-000000000008', **{'block/page':1,'block/parent':1,'block/order':'a2','block/link':2,'block/refs':[2]})
        g = Garden(self.nodes, self.root, self.config)
        content = g.page_content(1)
        self.assertIn('page-embed', content)
        self.assertIn('embed-title', content)

    def test_recursive_embed_does_not_loop(self):
        self.nodes[8] = node('', '11111111-1111-4111-8111-000000000008', **{'block/page':1,'block/parent':1,'block/order':'a2','block/link':1,'block/refs':[1]})
        g = Garden(self.nodes, self.root, self.config)
        content = g.page_content(1)
        self.assertIn('Recursive embed', content)
        self.assertTrue(any('Recursive' in w for w in g.warnings))

    def test_embed_style_inline_splices_page_embed_as_plain_siblings(self):
        embed_uuid = '11111111-1111-4111-8111-000000000008'
        self.nodes[8] = node('', embed_uuid, **{'block/page':1,'block/parent':1,'block/order':'a2','block/link':2,'block/refs':[2]})
        self.nodes[9] = node('Security detail', '11111111-1111-4111-8111-000000000009', **{'block/page':2,'block/parent':2,'block/order':'a0'})
        config = {**self.config, 'embed_style': 'inline'}
        g = Garden(self.nodes, self.root, config)
        content = g.page_content(1)
        self.assertNotIn('page-embed', content)
        self.assertNotIn('embed-title', content)
        self.assertIn('Security detail', content)
        self.assertNotIn('id="block-' + embed_uuid + '"', content)
        ordinary_blocks = 2  # Parent, Child
        embedded_page_blocks = 1  # Security detail
        self.assertEqual(content.count('<li class="block'), ordinary_blocks + embedded_page_blocks)

    def test_missing_references_do_not_invent_titles(self):
        result = self.g.md.render('[[99999999-9999-4999-8999-999999999999]]')
        self.assertIn('Unavailable reference', result)
        self.assertTrue(self.g.warnings)

    def test_cycles_and_asset_path_traversal_fail(self):
        self.g.children[3].append(4)
        with self.assertRaises(ValueError):
            self.g.page_content(1)
        with self.assertRaises(ValueError):
            self.g.asset_url('assets/../../outside.txt')

    def test_numerical_property_is_not_confused_with_entity_id(self):
        self.g.property_entities['user.property/grade'] = {'block/title':'Grade'}
        result = self.g.properties({'user.property/grade':2})
        self.assertIn('<dd>2</dd>', result)
        self.assertNotIn('<a', result)

    def test_ref_property_displays_value_even_without_public_page(self):
        self.g.property_entities['user.property/status'] = {'block/title':'Status','db/valueType':'db.type/ref'}
        result = self.g.properties({'user.property/status':6})
        self.assertIn('Hidden', result)

    def test_external_link_gets_external_class_and_rel(self):
        text = self.g.md.render('[x](https://example.org/a)')
        self.assertIn('class="external"', text)
        self.assertIn('rel="noopener"', text)

    def test_internal_links_do_not_get_external_class(self):
        text = self.g.md.render('[[%s]] [t](/page/foo/)' % self.ids[0])
        self.assertNotIn('external', text)

    def test_url_property_value_gets_external_class(self):
        self.g.property_entities['user.property/link'] = {'block/title':'Link'}
        result = self.g.properties({'user.property/link':'https://example.org/a'})
        self.assertIn('class="external"', result)

    def test_graph_contains_only_public_pages_and_deduplicated_edges(self):
        self.g.page_content(1)
        self.g.md.render('[[Security]] [security notes](Security)')
        data = self.g.graph_data()
        self.assertEqual({n['title'] for n in data['nodes']}, {'Home', 'Security', 'Notes'})
        links = {frozenset((data['nodes'][a]['title'], data['nodes'][b]['title'])) for a,b in data['links']}
        self.assertEqual(links, {frozenset(('Home', 'Security')), frozenset(('Home', 'Notes')), frozenset(('Security', 'Notes'))})
        self.assertEqual(len(links), len(data['links']))
        self.assertEqual(next(n for n in data['nodes'] if n['title'] == 'Home')['kind'], 'home')

    def test_sidebar_license_note_renders_when_configured(self):
        config = {**self.config, 'author': 'P. Kerim Friedman', 'license': 'CC BY 4.0'}
        g = Garden(self.nodes, self.root, config)
        html = g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js')
        self.assertIn('href="https://creativecommons.org/licenses/by/4.0/" rel="license"', html)
        self.assertIn('CC BY 4.0', html)
        self.assertIn('P. Kerim Friedman', html)
        self.assertNotIn('Always growing', html)
        self.assertNotIn('portfolio & digital garden', html)
        self.assertNotIn('brand-mark', html)
        self.assertNotIn('brand-logo', html)

    def test_sidebar_note_empty_without_author_or_license(self):
        html = self.g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js')
        self.assertNotIn('sidebar-note', html)

    def test_home_page_shell_has_a_single_unlinked_crumb(self):
        html = self.g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js',
                             home=True, crumbs=self.g.crumbs(1))
        self.assertEqual(len(re.findall(r'<li[ >]', html)), 1)
        self.assertIn('<li aria-current="page">My garden</li>', html)
        self.assertRegex(html, r'<header class="topbar"><nav class="crumbs"[^>]*>.*?</nav>')
        self.assertNotIn('/ garden</span>', html)

    def test_home_page_title_has_no_suffix(self):
        html = self.g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js', home=True)
        self.assertIn('<title>My garden</title>', html)

    def test_other_page_title_keeps_site_suffix(self):
        html = self.g.shell('Security', '<p>body</p>', '/security/', '/site/x.css', '/site/x.js')
        self.assertIn('<title>Security · My garden</title>', html)

    def test_no_page_has_a_garden_label_above_its_title(self):
        # The breadcrumb and the site title already say where you are.
        for home in (True, False):
            with self.subTest(home=home):
                html = self.g.shell('Security', '<p>body</p>', '/security/', '/site/x.css', '/site/x.js', home=home)
                self.assertNotIn('eyebrow', html)
                self.assertNotIn('THE GARDEN', html)
                self.assertIn('<h1>Security</h1>', html)

    def test_nav_label_defaults_to_topics(self):
        html = self.g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js', home=True)
        self.assertIn('<p class="nav-label">Topics</p>', html)

    def test_nav_label_is_config_driven(self):
        config = {**self.config, 'navigation_label': 'Paths'}
        g = Garden(self.nodes, self.root, config)
        html = g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js', home=True)
        self.assertIn('<p class="nav-label">Paths</p>', html)
        self.assertNotIn('Topics', html)

    def test_child_page_crumb_links_parent_and_leaves_child_unlinked(self):
        ids = self.ids + ['11111111-1111-4111-8111-' + f'{i:012d}' for i in range(8, 9)]
        nodes = {**self.nodes,
                 8: node('Child Page', ids[7], **{'block/name': 'child page',
                                                   'block/parent': 2})}
        g = Garden(nodes, self.root, self.config)
        crumbs = g.crumbs(8)
        self.assertEqual([label for label, _ in crumbs], ['Home', 'Security', 'Child Page'])
        html = g.shell('Child Page', '<p>body</p>', g.urls[8], '/site/x.css', '/site/x.js',
                        crumbs=crumbs)
        self.assertIn(f'<li><a href="{g.urls[2]}">Security</a></li>', html)
        self.assertIn('<li aria-current="page">Child Page</li>', html)
        self.assertRegex(html, r'<header class="topbar"><nav class="crumbs"[^>]*>.*?</nav>')
        self.assertNotIn('/ garden</span>', html)

    def test_strip_markdown_converts_links_and_removes_double_brackets(self):
        text = '[Triptych Newsletter](https://example.com/x) - [[3 fabulous]] links'
        self.assertEqual(strip_markdown(text), 'Triptych Newsletter - 3 fabulous links')

    def test_strip_markdown_collapses_whitespace(self):
        self.assertEqual(strip_markdown('a\n\n  b   c'), 'a b c')

    def test_home_page_description_uses_config_not_block_text(self):
        # build() passes config['description'] for the home page regardless of block text.
        html = self.g.shell('Home', '<p>body</p>', '/', '/site/x.css', '/site/x.js',
                             home=True, description=self.config['description'])
        self.assertIn('<meta name="description" content="Notes">', html)

    def test_other_page_description_strips_markdown_links(self):
        self.nodes[9] = node('[Triptych](https://example.com) - great',
                              '11111111-1111-4111-8111-000000000009',
                              **{'block/page': 2, 'block/parent': 2, 'block/order': 'a0'})
        g = Garden(self.nodes, self.root, self.config)
        text = ' '.join(g.label(i) for i, b in self.nodes.items() if b.get('block/page') == 2)
        description = strip_markdown(text)[:170]
        html = g.shell('Security', '<p>body</p>', '/security/', '/site/x.css', '/site/x.js',
                        description=description)
        self.assertIn('Triptych', html)
        self.assertNotIn('](https://example.com)', html)

    def test_output_cannot_replace_source_or_unrelated_files(self):
        with self.assertRaises(ValueError):
            build(self.root, self.root, self.config)
        out = self.root/'existing'
        out.mkdir(); (out/'precious.txt').write_text('keep')
        with self.assertRaises(ValueError):
            build(self.root, out, self.config)
        self.assertEqual((out/'precious.txt').read_text(), 'keep')

    def test_default_url_style_still_uuid(self):
        self.assertEqual(self.g.url_style, 'uuid')
        self.assertEqual(self.g.urls[2], '/page/security--' + self.ids[1] + '/')

    def test_theme_css_replaces_garden_css(self):
        theme_css_path = self.root / 'my-theme.css'
        theme_css_path.write_text('body { color: hotpink; }')
        config = {**self.config, 'theme_css': str(theme_css_path)}
        css = load_base_css(config)
        self.assertIn('hotpink', css)
        self.assertNotIn('hotpink', (HERE / 'garden.css').read_text())

    def test_theme_css_missing_file_raises_clear_error(self):
        config = {**self.config, 'theme_css': str(self.root / 'does-not-exist.css')}
        with self.assertRaises(FileNotFoundError):
            load_base_css(config)

    def test_exclude_pages_removes_page_and_degrades_links(self):
        config = {**self.config, 'exclude_pages': ['Notes']}
        g = Garden(self.nodes, self.root, config)
        self.assertNotIn(5, g.pages)
        self.assertNotIn('notes', g.names)
        result = g.md.render('[[Notes]]')
        self.assertIn('class="unavailable"', result)
        self.assertIn('Notes', result)
        self.assertNotIn('page-ref', result)

    def test_base_path_normalizes_leading_and_trailing_slash(self):
        config = {**self.config, 'base_path': 'logseq-faq/'}
        g = Garden(self.nodes, self.root, config)
        self.assertEqual(g.base_path, '/logseq-faq')
        self.assertEqual(g.href('/'), '/logseq-faq/')

    def test_base_path_defaults_to_empty(self):
        self.assertEqual(self.g.base_path, '')
        self.assertEqual(self.g.href('/pages/'), '/pages/')

    def test_base_path_prefixes_all_absolute_urls_in_home_page(self):
        config = {**self.config, 'base_path': '/x'}
        g = Garden(self.nodes, self.root, config)
        body = g.page_content(g.home)
        html = g.shell(self.config['title'], body, g.urls[g.home], '/site/garden-abc.css',
                        '/site/garden-abc.js', home=True, theme_js='/site/garden-abc.theme.js',
                        crumbs=g.crumbs(g.home))
        for attr in ('href', 'src'):
            for match in re.finditer(attr + r'="([^"]*)"', html):
                value = match.group(1)
                if value.startswith('/'):
                    self.assertTrue(value.startswith('/x/') or value == '/x',
                                     f'{attr}="{value}" is absolute but not under base_path /x')
        self.assertIn('data-base="/x"', html)

    def test_base_path_prefixes_attachment_links(self):
        (self.root / 'assets').mkdir()
        for name in ('photo.png', 'clip.mp4'):
            (self.root / 'assets' / name).write_bytes(b'x')
        g = Garden(self.nodes, self.root, {**self.config, 'base_path': '/x'})
        self.assertIn('src="/x/assets/photo.png"', g.md.render('![](../assets/photo.png)'))
        self.assertIn('src="/x/assets/clip.mp4"', g.md.render('![](../assets/clip.mp4)'))
        self.assertEqual(self.g.link_url('../assets/photo.png'), '/assets/photo.png')

    def test_base_path_prefixes_route_and_search_json_values(self):
        config = {**self.config, 'base_path': '/x'}
        g = Garden(self.nodes, self.root, config)
        routes = {n['block/uuid']: g.href(g.urls[i]) for i, n in g.pages.items()}
        for url in routes.values():
            self.assertTrue(url.startswith('/x/') or url == '/x')
        search_entry_url = g.href(g.urls[g.home])
        self.assertTrue(search_entry_url.startswith('/x/') or search_entry_url == '/x')


class NumberedListTests(unittest.TestCase):
    """Logseq numbered lists (`order-list-type` = "number") must come out numbered, not as bullets."""

    NUMBER = 90  # the "number" value block that numbered blocks point at

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'home_page': 'Home', 'title': 'My garden', 'navigation': [], 'description': 'Notes'}
        self.nodes = {1: node('Home', self.uuid(1), **{'block/name': 'home'}),
                      self.NUMBER: node('number', self.uuid(self.NUMBER), **{'block/page': 80, 'block/parent': 80})}

    @staticmethod
    def uuid(i):
        return '33333333-3333-4333-8333-' + f'{i:012d}'

    def add(self, eid, title, parent=1, order='a0', numbered=False):
        extra = {'block/page': 1, 'block/parent': parent, 'block/order': order}
        if numbered:
            extra['logseq.property/order-list-type'] = self.NUMBER
        self.nodes[eid] = node(title, self.uuid(eid), **extra)

    def page(self):
        return Garden(self.nodes, self.root, self.config).page_content(1)

    @staticmethod
    def labels(content):
        return re.findall(r'class="list-number"[^>]*>([^<]*)<', content)

    def test_numbered_siblings_become_an_ordered_list_numbered_from_one(self):
        for i, order in enumerate(('a0', 'a1', 'a2')):
            self.add(10 + i, f'Step {i}', order=order, numbered=True)
        content = self.page()
        self.assertEqual(content.count('<ol class="outline root-outline" type="1">'), 1)
        self.assertEqual(self.labels(content), ['1.', '2.', '3.'])
        self.assertEqual(content.count('<li class="block numbered"'), 3)
        self.assertNotIn('class="bullet"', content)

    def test_blocks_without_the_property_stay_plain_bullets(self):
        self.add(10, 'Just a note')
        content = self.page()
        self.assertIn('<ul class="outline root-outline">', content)
        self.assertIn('class="bullet"', content)
        self.assertNotIn('<ol', content)
        self.assertNotIn('list-number', content)

    def test_a_plain_block_restarts_the_numbering(self):
        self.add(10, 'One', order='a0', numbered=True)
        self.add(11, 'Two', order='a1', numbered=True)
        self.add(12, 'Aside', order='a2')
        self.add(13, 'Again', order='a3', numbered=True)
        content = self.page()
        self.assertEqual(self.labels(content), ['1.', '2.', '1.'])
        self.assertEqual(content.count('<ol '), 2)
        self.assertEqual(content.count('<ul '), 1)
        self.assertLess(content.index('Two'), content.index('Aside'))
        self.assertLess(content.index('Aside'), content.index('Again'))

    def test_nested_numbered_lists_count_in_letters_then_roman_numerals(self):
        self.add(10, 'Method')
        self.add(11, 'First', parent=10, order='a0', numbered=True)
        self.add(12, 'Second', parent=10, order='a1', numbered=True)
        self.add(13, 'Sub one', parent=11, order='a0', numbered=True)
        self.add(14, 'Sub two', parent=11, order='a1', numbered=True)
        self.add(15, 'Deep one', parent=13, order='a0', numbered=True)
        self.add(16, 'Deep two', parent=13, order='a1', numbered=True)
        content = self.page()
        # The list under a plain block counts 1. 2.; inside "First" it is a. b.; inside that, i. ii.
        self.assertEqual(re.findall(r'<ol class="outline[^"]*" type="(.)">', content), ['1', 'a', 'i'])
        self.assertEqual(self.labels(content), ['1.', 'a.', 'i.', 'ii.', 'b.', '2.'])

    def test_the_number_is_also_the_block_permalink(self):
        self.add(10, 'Only step', numbered=True)
        content = self.page()
        self.assertIn(f'<a class="list-number" href="#block-{self.uuid(10)}"', content)
        self.assertIn(f'id="block-{self.uuid(10)}"', content)

    def test_collapsible_numbered_block_keeps_its_number_and_children(self):
        self.add(10, 'Parent step', numbered=True)
        self.add(11, 'Child note', parent=10)
        content = self.page()
        self.assertIn('<details open>', content)
        self.assertEqual(self.labels(content), ['1.'])
        self.assertLess(content.index('Parent step'), content.index('Child note'))

    def test_only_the_number_list_type_makes_a_numbered_list(self):
        self.nodes[91] = node('bullet', self.uuid(91), **{'block/page': 80, 'block/parent': 80})
        self.add(10, 'Odd one', numbered=True)
        self.nodes[10]['logseq.property/order-list-type'] = 91
        self.assertNotIn('<ol', self.page())

    def test_a_missing_value_block_still_counts_as_a_numbered_list(self):
        self.add(10, 'Step', numbered=True)
        del self.nodes[self.NUMBER]
        self.assertIn('<ol', self.page())

    def test_embedded_page_keeps_its_numbered_list(self):
        self.nodes[2] = node('Steps', self.uuid(2), **{'block/name': 'steps'})
        for eid, title, order in ((20, 'Do this', 'a0'), (21, 'Then this', 'a1')):
            self.nodes[eid] = node(title, self.uuid(eid), **{
                'block/page': 2, 'block/parent': 2, 'block/order': order,
                'logseq.property/order-list-type': self.NUMBER})
        self.nodes[10] = node('', self.uuid(10), **{'block/page': 1, 'block/parent': 1, 'block/order': 'a0',
                                                    'block/link': 2, 'block/refs': [2]})
        for style in ('boxed', 'inline'):
            with self.subTest(embed_style=style):
                self.config['embed_style'] = style
                self.assertEqual(self.labels(self.page()), ['1.', '2.'])

    def test_marker_helpers_match_how_browsers_count(self):
        self.assertEqual([alpha_marker(n) for n in (1, 2, 26, 27, 28, 52, 53)],
                         ['a', 'b', 'z', 'aa', 'ab', 'az', 'ba'])
        self.assertEqual([roman_marker(n) for n in (1, 2, 4, 9, 14, 40, 1994)],
                         ['i', 'ii', 'iv', 'ix', 'xiv', 'xl', 'mcmxciv'])
        self.assertEqual(roman_marker(4000), '4000')

    def test_the_list_stylesheet_does_not_depend_on_the_theme(self):
        self.assertIn('.list-number', LIST_CSS)
        self.assertIn('ol.outline{list-style:none}', LIST_CSS)


class SectionUrlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = ['22222222-2222-4222-8222-' + f'{i:012d}' for i in range(1, 12)]
        self.nodes = {
            1: node('Home', self.ids[0], **{'block/name': 'home'}),
            2: node('Security', self.ids[1], **{'block/name': 'security'}),
            3: node('Taiwan', self.ids[2], **{'block/name': 'taiwan'}),
            4: node('My Obsidian Setup', self.ids[3], **{'block/name': 'my obsidian setup'}),
            5: node('', self.ids[4], **{'block/page': 2, 'block/parent': 2, 'block/order': 'a0', 'block/refs': [4]}),
            6: node('Shared Page', self.ids[5], **{'block/name': 'shared page'}),
            7: node('', self.ids[6], **{'block/page': 2, 'block/parent': 2, 'block/order': 'a1', 'block/refs': [6]}),
            8: node('', self.ids[7], **{'block/page': 3, 'block/parent': 3, 'block/order': 'a0', 'block/refs': [6]}),
            9: node('Solo', self.ids[8], **{'block/name': 'solo'}),
            10: node('Same Title', self.ids[9], **{'block/name': 'same title a'}),
            11: node('Same Title', self.ids[10], **{'block/name': 'same title b'})}
        self.config = {'home_page': 'Home', 'title': 'My garden', 'navigation': ['Security', 'Taiwan'],
                       'description': 'Notes', 'url_style': 'sections'}
        self.g = Garden(self.nodes, self.root, self.config)

    def test_navigation_page_gets_top_level_url(self):
        self.assertEqual(self.g.urls[2], '/security/')
        self.assertEqual(self.g.crumbs(2), [('Home', '/'), ('Security', None)])

    def test_referenced_page_gets_nested_url_and_crumb(self):
        self.assertEqual(self.g.urls[4], '/security/my-obsidian-setup/')
        self.assertEqual(self.g.crumbs(4),
                         [('Home', '/'), ('Security', '/security/'), ('My Obsidian Setup', None)])

    def test_unreferenced_page_gets_top_level_url(self):
        self.assertEqual(self.g.urls[9], '/solo/')
        self.assertEqual(self.g.crumbs(9), [('Home', '/'), ('Solo', None)])

    def test_precedence_first_navigation_entry_wins(self):
        self.assertEqual(self.g.urls[6], '/security/shared-page/')
        self.assertNotIn(6, [i for i in self.g.pages if self.g.sections.get(i) == 3])

    def test_collision_falls_back_to_uuid_suffix_with_warning(self):
        urls = {self.g.urls[10], self.g.urls[11]}
        self.assertIn('/same-title/', urls)
        fallback = next(u for u in urls if u != '/same-title/')
        self.assertTrue(fallback.startswith('/same-title--'))
        self.assertTrue(any('URL collision' in w for w in self.g.warnings))


class NavigationPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = ['33333333-3333-4333-8333-' + f'{i:012d}' for i in range(1, 10)]
        self.nodes = {
            1: node('Home', self.ids[0], **{'block/name': 'home'}),
            2: node('Security', self.ids[1], **{'block/name': 'security'}),
            3: node('Taiwan', self.ids[2], **{'block/name': 'taiwan'}),
            4: node('Topics', self.ids[3], **{'block/name': 'topics'}),
            # Block referencing Security via a [[Security]] link in its title.
            5: node('[[Security]]', self.ids[4], **{'block/page': 4, 'block/parent': 4, 'block/order': 'a0', 'block/refs': [2]}),
            # Embed block pointing at Taiwan (block/link, no title text).
            6: node('', self.ids[5], **{'block/page': 4, 'block/parent': 4, 'block/order': 'a1', 'block/link': 3, 'block/refs': [3]}),
            # Plain text block with no page reference: ignored.
            7: node('Just some notes', self.ids[6], **{'block/page': 4, 'block/parent': 4, 'block/order': 'a2'}),
            # A [[uuid]]-style duplicate reference to Security: dropped.
            8: node('[[%s]]' % self.ids[1], self.ids[7], **{'block/page': 4, 'block/parent': 4, 'block/order': 'a3', 'block/refs': [2]})}
        self.config = {'home_page': 'Home', 'title': 'My garden', 'navigation': ['Security'],
                       'navigation_page': 'Topics', 'description': 'Notes'}
        self.g = Garden(self.nodes, self.root, self.config)

    def test_nav_order_follows_topics_page_blocks(self):
        self.assertEqual(self.g.nav_ids, [2, 3])

    def test_navigation_page_itself_is_not_published(self):
        self.assertNotIn(4, self.g.pages)
        self.assertNotIn(4, self.g.urls)
        page_list_html = self.g.page_list(self.g.pages)
        self.assertNotIn('Topics', page_list_html)

    def test_missing_navigation_page_falls_back_with_warning(self):
        config = {**self.config, 'navigation_page': 'No Such Page'}
        g = Garden(self.nodes, self.root, config)
        self.assertEqual(g.nav_ids, [2])
        self.assertTrue(any("navigation_page 'No Such Page' not found or empty; using config navigation" in w
                            for w in g.warnings))

    def test_sections_url_style_uses_derived_navigation(self):
        config = {**self.config, 'url_style': 'sections'}
        g = Garden(self.nodes, self.root, config)
        self.assertEqual(g.urls[2], '/security/')
        self.assertEqual(g.urls[3], '/taiwan/')


if __name__ == '__main__':
    unittest.main()
