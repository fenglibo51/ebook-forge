import json
import os
from pathlib import Path
from fastapi import APIRouter

SETTINGS_PATH = "/data/settings.json"
DEFAULT_SETTINGS = {
    "naming_rule": "{title}.{author}.{ext}",
    "keep_source": False,
    "default_format": "epub",
}

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _load():
    if Path(SETTINGS_PATH).exists():
        try:
            with open(SETTINGS_PATH) as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def _save(data: dict):
    Path(SETTINGS_PATH).parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT_SETTINGS, **data}
    with open(SETTINGS_PATH, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)


@router.get("")
def get_settings():
    return _load()


@router.put("")
def update_settings(data: dict):
    _save(data)
    return {"success": True}
