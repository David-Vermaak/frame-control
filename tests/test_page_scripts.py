"""The page's inline scripts share one global scope, so a second top-level function or
variable with a name already used replaces the first everywhere, silently."""
import html.parser
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Compiles the scripts as one strict-mode block. There, functions are block scoped like let
# and const, so V8 itself rejects a name declared twice in the shared scope (however it's
# indented or declared: function, class, let, const, destructuring), while helpers with the
# same name inside different functions stay legal. var is the exception (declaring one twice
# is allowed), so the page doesn't use var at all: Node's own copy of the acorn parser finds
# real var declarations, not the word in comments, strings or CSS var().
DUPLICATES = r'''
const vm = require('vm');
const scripts = JSON.parse(require('fs').readFileSync(0, 'utf8'));
try { new vm.Script('"use strict"; {\n' + scripts.join('\n;\n') + '\n}'); console.log('ok'); }
catch (e) { console.log(e.message); }
'''
VARS = r'''
const scripts = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let acorn, walk;
try {
  acorn = require('internal/deps/acorn/acorn/dist/acorn');
  walk = require('internal/deps/acorn/acorn-walk/dist/walk');
} catch { console.log('no parser'); process.exit(0); }
for (const code of scripts) {
  let found = null;
  try {
    walk.simple(acorn.parse(code, { ecmaVersion: 'latest' }), {
      VariableDeclaration(n) { if (n.kind === 'var' && !found) found = code.slice(n.start, n.end).slice(0, 40); },
    });
  } catch (e) { console.log(e.message); process.exit(0); }
  if (found) { console.log('declares with var: ' + found); process.exit(0); }
}
console.log('ok');
'''


def node(script, scripts, *flags):
    r = subprocess.run(['node', *flags, '-e', script], input=json.dumps(scripts), capture_output=True, text=True)
    return r.stdout.strip() or r.stderr.strip()


def check(scripts):
    """'ok', or V8's message for the first name declared twice."""
    return node(DUPLICATES, scripts)


def check_vars(scripts):
    """'ok', or the first var declaration. Skips where Node doesn't include acorn."""
    out = node(VARS, scripts, '--expose-internals')
    if out == 'no parser':
        raise unittest.SkipTest("this Node doesn't include acorn")
    return out


class ClassicScripts(html.parser.HTMLParser):
    """The page's inline classic scripts: no src, and no type other than a JavaScript one (a
    module has its own scope, and JSON data isn't code). The types a browser runs as script:
    https://mimesniff.spec.whatwg.org/#javascript-mime-type"""
    JS = {'', 'application/ecmascript', 'application/javascript', 'application/x-ecmascript',
          'application/x-javascript', 'text/ecmascript', 'text/javascript', 'text/javascript1.0',
          'text/javascript1.1', 'text/javascript1.2', 'text/javascript1.3', 'text/javascript1.4',
          'text/javascript1.5', 'text/jscript', 'text/livescript', 'text/x-ecmascript', 'text/x-javascript'}

    def __init__(self):
        super().__init__()
        self.scripts, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            a = dict(attrs)
            inline = 'src' not in a and (a.get('type') or '').strip().lower() in self.JS
            self.current = [] if inline else None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append(''.join(self.current))
        if tag == 'script':
            self.current = None


def classic_scripts(page):
    p = ClassicScripts()
    p.feed(page)
    return p.scripts


@unittest.skipUnless(shutil.which('node'), 'Node parses the page scripts')
class PageScripts(unittest.TestCase):
    def test_no_top_level_name_is_declared_twice(self):
        scripts = classic_scripts((ROOT / 'ui/index.html').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(scripts), 2)
        self.assertEqual(check(scripts), 'ok')

    def test_the_page_declares_nothing_with_var(self):
        scripts = classic_scripts((ROOT / 'ui/index.html').read_text(encoding='utf-8'))
        self.assertEqual(check_vars(scripts), 'ok')

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

    def test_the_var_check_finds_what_it_should(self):
        # var: legal to declare twice, so not allowed at all; however it's written.
        for code in ['var x = 1;', 'var/*c*/x = 1;', 'var {x} = {x: 1};', 'function f() { var y; }']:
            with self.subTest(code):
                self.assertIn('declares with var', check_vars([code]))
        # The word var elsewhere is fine.
        text = ['// var x\nconst a = "var y", b = `var ${a}`, c = "color: var(--blue)", d = {}.var;']
        self.assertEqual(check_vars(text), 'ok')

    def test_only_inline_classic_scripts_are_checked(self):
        page = ('<script>let a;</script><script type="module">export const m = 1;</script>'
                '<script type="application/json">{"b": 1}</script><script src = "x.js"></script>'
                '<script data-src="y" type="text/javascript">let c;</script>'
                '<script type="text/ecmascript">let d;</script><script type=" Application/X-JavaScript ">let e;</script>')
        self.assertEqual(classic_scripts(page), ['let a;', 'let c;', 'let d;', 'let e;'])


if __name__ == '__main__':
    unittest.main()
