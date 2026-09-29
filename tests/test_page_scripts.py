"""The page's inline scripts share one global scope, so a second top-level function or
variable with a name already used replaces the first everywhere, silently."""
import collections
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PageScripts(unittest.TestCase):
    def test_no_top_level_name_is_declared_twice(self):
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        names = collections.Counter()
        for js in re.findall(r'<script>(.*?)</script>', page, re.S):
            for m in re.finditer(r'^(?:async\s+)?function\s+(\w+)|^(?:const|let|var)\s+(\w+)\s*=', js, re.M):
                names[m.group(1) or m.group(2)] += 1
        self.assertEqual({k: n for k, n in names.items() if n > 1}, {})


if __name__ == '__main__':
    unittest.main()
