"""Compatibility launcher for CardMirror."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cardmirror.main import main


if __name__ == "__main__":
    main()
