import os
import sys

# Vercel's Python runtime imports this module from the api/ directory — add the
# repo root to sys.path so `run` and `app` are importable the same way they are locally.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run import app  # noqa: E402  (import after sys.path setup)
