from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """Return the writable portable directory beside the executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = app_root()
WORK = ROOT / "work"
OUTPUT = ROOT / "output"
MODELS = ROOT / "models"


def ensure_dirs() -> None:
    for path in (WORK, OUTPUT, MODELS):
        path.mkdir(parents=True, exist_ok=True)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base / relative


def portable_env() -> None:
    os.environ.setdefault("HF_HOME", str(MODELS / "huggingface"))
    os.environ.setdefault("TORCH_HOME", str(MODELS / "torch"))

