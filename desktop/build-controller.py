#!/usr/bin/env python3
"""Build the common controller for the Python PC host (a C11 compiler needed)."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
out = root / ('controller' + suffix)
if sys.platform == 'win32' and os.environ.get('CC', 'cl') == 'cl':
    cmd = ['cl', '/nologo', '/O2', '/LD', '/std:c11', str(root / 'controller.c'), '/link', '/OUT:' + str(out)]
else:
    cmd = [os.environ.get('CC', 'cc'), '-O2', '-std=c11', '-shared', '-fPIC', str(root / 'controller.c'), '-o', str(out)]
subprocess.run(cmd, cwd=root, check=True)
print(out)
