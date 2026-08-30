"""Root test configuration.

Puts the API package on ``sys.path`` and exposes the governed content root, so
every test tier of §51 runs against the *same* protocol and red-flag content the
deployed service loads — never against a test-only copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
API_ROOT = REPO_ROOT / "services" / "api"
CONTENT_ROOT = REPO_ROOT / "content"

for path in (API_ROOT,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
