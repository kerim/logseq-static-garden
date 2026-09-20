"""Regression tests for export decoding and publishing behavior."""
import re
import tempfile
import unittest
from pathlib import Path

from build import Garden, read_entities, build
from transit_reader import Reader, Tagged


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

    def test_output_cannot_replace_source_or_unrelated_files(self):
        with self.assertRaises(ValueError):
            build(self.root, self.root, self.config)
        out = self.root/'existing'
        out.mkdir(); (out/'precious.txt').write_text('keep')
        with self.assertRaises(ValueError):
            build(self.root, out, self.config)
        self.assertEqual((out/'precious.txt').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
