"""The page calls showPage() while its first script is still running, before names declared
later (in that script or the second one) exist. Touching one of them there throws, and the
rest of the page's setup never runs: opening Frame Control at #games did exactly that."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STUBS = ['$', 'toggleLive', 'scrollToY', 'loadMacView', 'loadPanels', 'loadPanelSwitcher', 'loadTitles']

RUN = r'''
const vm = require('vm');
const {code, hashes} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const failures = [];
for (const hash of hashes) {
  const context = vm.createContext({
    location: {hash}, document: {querySelectorAll: () => [], title: ''}, live: false,
  });
  try { vm.runInContext(code, context); }
  catch (e) { failures.push(`${hash}: ${e.message}`); }
}
console.log(JSON.stringify(failures));
'''


@unittest.skipUnless(shutil.which('node'), 'Node runs the page code')
class PageStartup(unittest.TestCase):
    def test_opening_any_page_or_section_at_startup_runs(self):
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        first, second = re.findall(r'<script>(.*?)</script>', page, re.S)[:2]
        tables = first[first.index('const PAGES ='):first.index('let page =')]
        start = first.index('function showPage(')
        show = first[start:first.index('\n}\n', start) + 3]
        # Everything declared after the startup call is still uninitialised when it runs.
        call = re.search(r'^showPage\(\);', first, re.M).end()
        later = re.findall(r'^(?:const|let)\s+(\w+)', first[call:] + second, re.M)
        later = [n for n in dict.fromkeys(later) if n not in STUBS and n not in ('PAGES', 'SECTION_PAGE', 'page')]
        self.assertIn('link', later)  # the name that broke #games
        stubs = ''.join(f'function {n}() {{ return {{ classList: {{ toggle() {{}} }}, scrollIntoView() {{}} }}; }}\n'
                        for n in STUBS if n != '$')
        code = ('const $ = id => id === "nowhere" ? null : { classList: { toggle() {} }, scrollIntoView() {} };\n' + stubs + tables + 'let page = "home";\n' + show +
                'showPage();\n' + ''.join(f'let {n};\n' for n in later))
        hashes = ['', '#home', '#games', '#android', '#tools', '#settings', '#devices', '#nowhere']
        hashes += ['#' + k for k in re.findall(r'(\w+): "', tables)]
        r = subprocess.run(['node', '-e', RUN], input=json.dumps({'code': code, 'hashes': hashes}),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [])


if __name__ == '__main__':
    unittest.main()
