"""Root conftest: make services/store.py importable from decoder subdirectories."""
import sys
import os

# Add services/ to path so `import store` works in all decoder packages
sys.path.insert(0, os.path.dirname(__file__))
