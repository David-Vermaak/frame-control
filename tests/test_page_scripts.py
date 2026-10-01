"""The page's inline scripts share one global scope, so a second top-level function or
variable with a name already used replaces the first everywhere, silently."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Compiles the scripts as one strict-mode block. There, functions are block scoped like let
# and const, so V8 itself rejects a name declared twice in the shared scope (however it's
# indented or declared: function, class, let, const, destructuring), while helpers with the
# same name inside different functions stay legal.
CHECK = r'''
const vm = require('vm');
const scripts = JSON.parse(require('fs').readFileSync(0, 'utf8'));
try { new vm.Script('"use strict"; {\n' + scripts.join('\n;\n') + '\n}'); console.log('ok'); }
catch (e) { console.log(e.message); }
'''


def check(scripts):
    r = subprocess.run(['node', '-e', CHECK], input=json.dumps(scripts), capture_output=True, text=True)
    return r.stdout.strip() or r.stderr.strip()


@unittest.skipUnless(shutil.which('node'), 'Node parses the page scripts')
class PageScripts(unittest.TestCase):
    def test_no_top_level_name_is_declared_twice(self):
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        self.assertEqual(check(re.findall(r'<script>(.*?)</script>', page, re.S)), 'ok')

    def test_the_check_finds_what_it_should(self):
        twice = {
            'indented function': ['function loadPanels() {}', '  async function loadPanels() {}'],
            'class': ['class Panel {}', 'class Panel {}'],
            'destructured': ['const { a, b } = {};', 'let [b] = [];'],
            'later declarator': ['let x = 1;', 'const y = 2, x = 3;'],
            'function and const': ['function f() {}', 'const f = 1;'],
        }
        for what, scripts in twice.items():
            with self.subTest(what):
                self.assertIn('already been declared', check(scripts))
        helpers = ['function a() { function help() {} }', 'function b() { const help = 1; }']
        self.assertEqual(check(helpers), 'ok')


if __name__ == '__main__':
    unittest.main()
