from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULTS = {
    "positive": "给漫画进行上色，颜色不要太淡",
    "negative": "低饱和，灰度，褪色，未上色，模糊，重影，文字变形",
    "width": "",
    "height": "",
    "title": "",
}

def load(path: Path) -> dict[str, str]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        return {key: str(values.get(key, default)) for key, default in DEFAULTS.items()}
    except (OSError, ValueError, TypeError):
        return DEFAULTS.copy()

def save(path: Path, values: dict[str, str]) -> dict[str, str]:
    merged = DEFAULTS.copy()
    merged.update({key: str(values.get(key, "")) for key in DEFAULTS})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="settings_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(merged, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return merged
