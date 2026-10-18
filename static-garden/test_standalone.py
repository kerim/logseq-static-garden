"""The exporter builds without a personal checkout or source JavaScript."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from build import HERE, build


class StandaloneTests(unittest.TestCase):
    def test_external_source_config_branding_and_math(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'public export'
            shutil.copytree(HERE.parent / 'examples/minimal', source)
            config = json.loads((source / 'site.json').read_text())
            config.update(title='Independent site', url='https://garden.example')
            (source / 'site.json').write_text(json.dumps(config))
            shutil.copytree(HERE / 'branding', source / 'branding')
            logo = source / 'branding/logo.svg'
            logo.write_text(logo.read_text().replace('#1b1920', '#332244'))
            # A raw export can contain arbitrary runtime JS. Never execute it.
            (source / 'static/js').mkdir(parents=True)
            (source / 'static/js/katex.min.js').write_text('throw new Error("Source JS executed!");')
            (source / 'LICENSE.md').write_text('Fictional publisher license')
            output = root / 'website'
            result = subprocess.run([sys.executable, str(HERE / 'build.py'), '--output', str(output)],
                                    cwd=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            html = (output / 'index.html').read_text()
            self.assertIn('Independent site', html)
            self.assertIn('https://garden.example/', html)
            logo_hash = sha256(logo.read_bytes()).hexdigest()[:12]
            self.assertEqual((output / f'site/logo-{logo_hash}.svg').read_bytes(), logo.read_bytes())
            self.assertEqual((output / 'licenses/SITE-LICENSE.md').read_text(), 'Fictional publisher license')
            report = json.loads((root / 'website-report.json').read_text())
            self.assertEqual(report['equations'], 1)
            self.assertEqual(report['warnings'], [])
            notes = next(output.glob('page/notes--*/index.html')).read_text()
            self.assertIn('<math', notes)
            self.assertFalse((output / 'static/js').exists())
            # Numbered lists need the builder's own list styles, whatever theme the site uses.
            stylesheet = next((output / 'site').glob('garden-*.css')).read_text()
            self.assertIn('.list-number', stylesheet)

    def test_incomplete_branding_fails_instead_of_mixing_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            shutil.copytree(HERE.parent / 'examples/minimal', source, dirs_exist_ok=True)
            (source / 'branding').mkdir()
            (source / 'branding/logo.svg').write_text('<svg/>')
            with self.assertRaisesRegex(ValueError, 'Branding file missing'):
                build(source, source / 'dist', json.loads((source / 'site.json').read_text()))

    def test_output_cannot_replace_exporter_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'Output must not replace'):
                build(Path(temporary), HERE.parent, {})


if __name__ == '__main__':
    unittest.main()
