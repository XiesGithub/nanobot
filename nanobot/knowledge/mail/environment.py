"""Read-only capability checks. Never download models during application startup."""

import importlib.util
import sqlite3
from pathlib import Path


def capabilities(model_path: str = "") -> dict[str, bool | str]:
    with sqlite3.connect(":memory:") as connection:
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(terms)")
            fts5 = True
        except sqlite3.OperationalError:
            fts5 = False
    dependencies = all(importlib.util.find_spec(m) is not None for m in (
        "numpy", "sentence_transformers",
    ))
    model_present = bool(model_path) and (Path(model_path) / "config.json").is_file()
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": fts5,
        "embedding_dependencies": dependencies,
        "model_files_present": model_present,
        # Ready requires actually loading and validating the pinned encoder.
        "semantic_status": "not_loaded" if dependencies and model_present else "semantic_unavailable",
    }
