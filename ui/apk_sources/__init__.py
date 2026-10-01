"""APK sources: every place Frame Control can find and download APKs.

One module per source kind in this package. Each module exposes the same small
interface so ``search.py`` can query them all and show where every result came
from. Python stdlib only; must run on Python 3.9.

Module interface
----------------
KIND = 'sidequest'            # stable id of the source kind
def sources() -> list[dict]   # configured sources of this kind (a kind can have
                              # several, e.g. one per F-Droid-format repo)
def search(source, query, limit=50) -> list[dict]   # Entry dicts, best first
def details(source, entry_id) -> dict               # Entry with 'versions'
def download(source, entry_id, version_code=None) -> dict
    # {'apk': local path, 'obb': [paths], 'sha256': hex or None, 'verified': bool}
    # Raise SourceError with a user-readable message on failure.

Source dict
-----------
{'id': 'sidequest', 'kind': KIND, 'name': 'SideQuest', 'url': 'https://...',
 'builtin': True, 'enabled': True, 'trust': 'official' | 'community' | 'user'}

Entry dict (missing facts are None, never guessed)
----------
{'source': source id, 'id': source-local id, 'package': 'org.example.app' or None,
 'name': str, 'summary': str, 'icon': url or None, 'page': url or None,
 'version': '1.2', 'version_code': 12, 'min_sdk': 24, 'abis': ['arm64-v8a'],
 'vr': True/False/None, 'size': bytes, 'free': True, 'license': 'GPL-3.0' or None,
 'updated': 'YYYY-MM-DD', 'downloadable': bool,   # False = open page only
 'versions': [ {version, version_code, min_sdk, size, updated}, ... ]}  # details() only

Rules
-----
- Only sources that distribute APKs with the developer's consent: free listings,
  never paid apps re-hosted, no licence or entitlement workarounds.
- Honour each site's terms and robots rules; if automated download isn't allowed,
  return entries with 'downloadable': False and a 'page' link instead.
- Cache indexes under frame_host.cache_dir('apk-sources'); send a clear
  User-Agent ('FrameControl/<version>'); keep requests modest.
- Tests use recorded fixtures, never the network.
"""


class SourceError(Exception):
    """User-readable failure from a source (network, format, verification)."""


class SourceLimited(SourceError):
    """The source's host asked us to slow down; retry_after is in seconds."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after
