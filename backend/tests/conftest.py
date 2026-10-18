"""Test configuration: set safe environment defaults before the app is imported."""

import os
import sys
from pathlib import Path

# Never reach out to metadata providers from tests.
os.environ.setdefault("CACHE_ONLY_MODE", "true")
os.environ.setdefault("SEEDR_SESSION_SECRET", "test-only-session-secret")

# Make `import main` work when pytest is started from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
