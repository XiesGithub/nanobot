"""Prove that handbook validation works after removing the repository dependency."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="portable-handbook-") as temporary:
    copied = Path(temporary) / "study"
    shutil.copytree(root, copied, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
    assert not (copied.parent / "nanobot").exists()
    subprocess.run([sys.executable, str(copied / "tools/check_study.py")], check=True)
    subprocess.run(["node", str(copied / "tools/check_navigation.cjs")], check=True)
    subprocess.run(["node", str(copied / "tools/check_reading_navigation.cjs")], check=True)
    subprocess.run([sys.executable, str(copied / "examples/verify_example.py")], check=True)
print("PASS: standalone study copy validates and runs without repository source")
