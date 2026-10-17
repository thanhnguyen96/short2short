from __future__ import annotations

import sys
from pathlib import Path

# Make tests/fakes.py importable as `fakes` regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent))
