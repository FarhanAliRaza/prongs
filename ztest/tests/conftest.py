import sys
from pathlib import Path

# Make ztest_py importable when running the ztest test suite directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
