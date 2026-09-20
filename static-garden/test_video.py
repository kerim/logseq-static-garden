"""Video macro and image-syntax rendering (YouTube, Vimeo, direct files) and frame-origin restrictions."""
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from build import Garden, HEADERS, video_html


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.id = '0-zDFLbWK1Q'
        self.nodes = {
            1: {'block/title': 'Home', 'block/name': 'home', 'block/uuid': '11111111-1111-4111-8111-111111111111'},
            2: {'block/title': '{{video https://youtu.be/' + self.id + '}}',
                'block/uuid': '22222222-2222-4222-8222-222222222222', 'block/page': 1, 'block/parent': 1},
        }
        self.g = Garden(self.nodes, Path('/tmp'), {'home_page': 'Home'})

    def test_exported_macro_renders_player_and_fallback_without_source_warning(self):
        result = self.g.block(2)
        tags = Tags(result).tags
        frame = next(attrs for tag, attrs in tags if tag == 'iframe')
        self.assertEqual(frame['src'], 'https://www.youtube-nocookie.com/embed/' + self.id + '?playsinline=1')
        self.assertEqual(frame['loading'], 'lazy')
        self.assertEqual(frame['referrerpolicy'], 'strict-origin-when-cross-origin')
        self.assertIn('allowfullscreen', frame)
        self.assertTrue(any(tag == 'a' and attrs.get('href') == 'https://www.youtube.com/watch?v=' + self.id for tag, attrs in tags))
        self.assertNotIn('{{video', result)
        self.assertFalse(self.g.warnings)
        self.assertIn(2, self.g.rendered_ids)

    def test_supported_urls_and_timestamps_are_normalized(self):
        cases = [
            ('https://youtu.be/' + self.id + '?t=15', '15'),
            ('https://www.youtube.com/watch?v=' + self.id + '&t=1m30s&autoplay=1', '90'),
            ('https://m.youtube.com/watch?v=' + self.id + '&amp;start=30', '30'),
            ('https://youtube.com/shorts/' + self.id, None),
            ('https://youtube.com/live/' + self.id, None),
            ('https://www.youtube-nocookie.com/embed/' + self.id + '#t=1h2m3s', '3723'),
            ('[video](https://youtu.be/' + self.id + ')', None),
            ('<https://youtu.be/' + self.id + '>', None),
        ]
        for raw, start in cases:
            with self.subTest(raw=raw):
                frame = next(attrs for tag, attrs in Tags(video_html('{{video ' + raw + '}}')).tags if tag == 'iframe')
                parts = urlsplit(frame['src'])
                self.assertEqual(parts.netloc, 'www.youtube-nocookie.com')
                self.assertEqual(parts.path, '/embed/' + self.id)
                query = parse_qs(parts.query)
                self.assertEqual(query.get('start', [None])[0], start)
                self.assertNotIn('autoplay', query)

    def test_untrusted_hosts_schemes_and_markup_never_become_frames(self):
        for raw in ('javascript:alert(1)', 'https://youtube.com.evil.invalid/watch?v=' + self.id,
                    'https://youtube.com@evil.invalid/watch?v=' + self.id,
                    'https://evil.invalid/embed/' + self.id, 'https://youtu.be/invalid',
                    'https://youtu.be/' + self.id + '" onload="alert(1)',
                    'https://www.youtube.com/watch?v=%22%3E%3Cscript%3E',
                    'https://www.youtube.com:444/watch?v=' + self.id):
            with self.subTest(raw=raw):
                self.nodes[2]['block/title'] = '{{video ' + raw + '}}'
                tags = Tags(self.g.block(2)).tags
                self.assertFalse(any(tag in ('iframe', 'script', 'object') for tag, _ in tags))
                self.assertFalse(any(key.startswith('on') for _, attrs in tags for key in attrs))
        self.assertTrue(self.g.warnings)

    def test_code_examples_are_not_embedded(self):
        original = self.nodes[2]['block/title']
        for text in ('`' + original + '`', '```text\n' + original + '\n```'):
            self.nodes[2]['block/title'] = text
            self.assertNotIn('<iframe', self.g.block(2))
        self.nodes[2]['block/title'] = original
        for display in ('code', 'math'):
            self.nodes[2]['logseq.property.node/display-type'] = display
            self.assertNotIn('<iframe', self.g.block(2))

    def test_heading_metadata_does_not_replace_video_with_macro_text(self):
        self.nodes[2]['logseq.property/heading'] = 2
        self.assertIn('<iframe', self.g.block(2))
        self.assertNotIn('{{video', self.g.block(2))

    def test_frame_policy_allows_only_the_generated_providers(self):
        global_rules = HEADERS.split('\n\n')[0]
        directive = next(part.strip() for part in global_rules.split(';') if part.strip().startswith('frame-src '))
        self.assertEqual(directive, 'frame-src https://www.youtube-nocookie.com https://player.vimeo.com')
        self.assertIn("media-src 'self' https:", global_rules)
        self.assertIn("frame-ancestors 'none'", global_rules)
        self.assertIn('X-Frame-Options: DENY', global_rules)


    def frames(self, html):
        return [attrs for tag, attrs in Tags(html).tags if tag == 'iframe']

    def test_vimeo_urls_render_a_privacy_enhanced_player(self):
        for raw, src in [
            ('https://vimeo.com/76979871', 'https://player.vimeo.com/video/76979871?dnt=1'),
            ('https://www.vimeo.com/76979871/', 'https://player.vimeo.com/video/76979871?dnt=1'),
            ('https://player.vimeo.com/video/76979871', 'https://player.vimeo.com/video/76979871?dnt=1'),
            ('https://vimeo.com/76979871/abc123def4', 'https://player.vimeo.com/video/76979871?h=abc123def4&dnt=1'),
            ('https://player.vimeo.com/video/76979871?h=abc123def4&autoplay=1',
             'https://player.vimeo.com/video/76979871?h=abc123def4&dnt=1'),
        ]:
            with self.subTest(raw=raw):
                html = video_html('{{video ' + raw + '}}')
                frame, = self.frames(html)
                self.assertEqual(frame['src'], src)
                self.assertEqual(frame['loading'], 'lazy')
                self.assertEqual(frame['referrerpolicy'], 'strict-origin-when-cross-origin')
                self.assertIn('Watch on Vimeo', html)

    def test_untrusted_vimeo_urls_never_become_frames(self):
        for raw in ('https://vimeo.com.evil.invalid/76979871', 'https://vimeo.com@evil.invalid/76979871',
                    'https://player.vimeo.com:444/video/76979871', 'https://evil.invalid/video/76979871',
                    'https://player.vimeo.com/video/abc', 'https://vimeo.com/76979871/not-a-hash',
                    'https://player.vimeo.com/video/76979871?h=%22%3E%3Cscript%3E',
                    'https://vimeo.com/76979871" onload="alert(1)', 'javascript:alert(1)'):
            with self.subTest(raw=raw):
                self.nodes[2]['block/title'] = '{{video ' + raw + '}}'
                tags = Tags(self.g.block(2)).tags
                self.assertFalse(any(tag in ('iframe', 'script', 'object', 'video') for tag, _ in tags))
                self.assertFalse(any(key.startswith('on') for _, attrs in tags for key in attrs))

    def test_direct_video_files_render_a_video_element(self):
        html = video_html('{{video https://example.com/media/clip.mp4?token=1}}')
        video, = [attrs for tag, attrs in Tags(html).tags if tag == 'video']
        self.assertEqual(video['src'], 'https://example.com/media/clip.mp4?token=1')
        self.assertEqual(video['preload'], 'none')
        self.assertIn('controls', video)
        for ext in ('webm', 'ogg', 'mov'):
            self.assertIn('<video', video_html('{{video https://example.com/clip.' + ext + '}}'))

    def test_video_files_must_be_https_video_urls(self):
        for raw in ('http://example.com/clip.mp4', 'ftp://example.com/clip.mp4', 'https://example.com/clip.exe',
                    'https://example.com/clip.mp4.html', '/assets/clip.mp4', 'javascript:alert(1)//x.mp4'):
            with self.subTest(raw=raw):
                self.assertIsNone(video_html('{{video ' + raw + '}}'))

    def test_video_file_url_cannot_break_out_of_its_attribute(self):
        html = video_html('{{video https://example.com/a" onerror="alert(1).mp4}}')
        tags = Tags(html).tags
        video, = [attrs for tag, attrs in tags if tag == 'video']
        self.assertEqual(set(video), {'controls', 'playsinline', 'preload', 'src'})
        self.assertFalse(any(key.startswith('on') for _, attrs in tags for key in attrs))

    def test_image_syntax_links_to_videos_render_players(self):
        cases = {
            '![](https://www.youtube.com/watch?v=' + self.id + ')': 'https://www.youtube-nocookie.com/embed/' + self.id + '?playsinline=1',
            '![A talk](https://youtu.be/' + self.id + ')': 'https://www.youtube-nocookie.com/embed/' + self.id + '?playsinline=1',
            '![](https://vimeo.com/76979871)': 'https://player.vimeo.com/video/76979871?dnt=1',
        }
        for markdown, src in cases.items():
            with self.subTest(markdown=markdown):
                html = self.g.md.render(markdown)
                frame, = self.frames(html)
                self.assertEqual(frame['src'], src)
                self.assertNotIn('<img', html)
        self.assertEqual(self.frames(self.g.md.render('![A talk](https://youtu.be/' + self.id + ')'))[0]['title'], 'A talk')
        self.assertIn('<video', self.g.md.render('![](https://example.com/clip.webm)'))
        self.assertIn('<img', self.g.md.render('![](https://example.com/photo.png)'))
        self.assertNotIn('<iframe', self.g.md.render('![](https://evil.invalid/embed/' + self.id + ')'))
        self.assertFalse(self.g.warnings)

    def test_macro_on_its_own_line_inside_longer_text_renders_a_player(self):
        self.nodes[2]['block/title'] = 'Intro paragraph.\n\n{{video https://youtu.be/' + self.id + '}}\n\nOutro paragraph.'
        result = self.g.block(2)
        self.assertEqual(len(self.frames(result)), 1)
        self.assertTrue(result.index('Intro paragraph') < result.index('<iframe') < result.index('Outro paragraph'))
        self.assertNotIn('{{video', result)
        self.assertFalse(self.g.warnings)

    def test_several_macros_in_one_block_each_render(self):
        self.nodes[2]['block/title'] = ('{{video https://youtu.be/' + self.id + '}}\n\n'
                                        '{{video https://vimeo.com/76979871}}\n'
                                        '{{video https://example.com/clip.mp4}}')
        result = self.g.block(2)
        self.assertEqual(len(self.frames(result)), 2)
        self.assertIn('<video', result)
        self.assertFalse(self.g.warnings)

    def test_macro_inside_a_sentence_splits_the_paragraph_around_the_player(self):
        self.nodes[2]['block/title'] = 'Read this first. {{video https://youtu.be/' + self.id + '}} Then continue.'
        result = self.g.block(2)
        self.assertEqual(len(self.frames(result)), 1)
        self.assertIn('<p>Read this first.</p>', result)
        self.assertIn('<p>Then continue.</p>', result)
        self.assertNotRegex(result, r'<p>[^<]*<figure')
        self.assertFalse(self.g.warnings)

    def test_macros_in_code_quotes_and_indented_code_stay_source(self):
        line = '{{video https://youtu.be/' + self.id + '}}'
        for text in ('```\n' + line + '\n```', 'before\n```text\n' + line + '\n\nmore\n```\nafter',
                     '~~~\n' + line + '\n~~~', 'Use `' + line + '` to embed a video.', '``' + line + '``',
                     '> ' + line, '    x\n\n    ' + line, 'x\n\n\t' + line):
            with self.subTest(text=text):
                self.nodes[2]['block/title'] = text
                self.assertNotIn('<iframe', self.g.block(2))

    def test_unrecognized_macro_beside_a_video_still_warns(self):
        self.nodes[2]['block/title'] = '{{video https://youtu.be/' + self.id + '}}\n\n{{query (todo)}}'
        result = self.g.block(2)
        self.assertEqual(len(self.frames(result)), 1)
        self.assertTrue(any('Macro/query retained as source' in w for w in self.g.warnings))


if __name__ == '__main__':
    unittest.main()
