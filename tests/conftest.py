"""Put the project root on sys.path so `import app` works from tests/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
