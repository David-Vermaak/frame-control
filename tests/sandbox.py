"""Imported first by every test module: nothing a test does reaches this person's
app data, their telemetry, or the shared compatibility database.

Must run before any ui module is imported, since those read these at import time.
"""
import atexit
import os
import shutil
import tempfile

_dir = tempfile.mkdtemp(prefix="frame-control-tests-")
atexit.register(shutil.rmtree, _dir, ignore_errors=True)
os.environ["FRAME_CONTROL_DATA_DIR"] = _dir
os.environ["FRAME_CONTROL_TELEMETRY"] = "0"
# A maintainer's machine holds the database key; send anything that slips through nowhere.
os.environ["FRAME_COMPAT_DB_URL"] = "http://127.0.0.1:9"
