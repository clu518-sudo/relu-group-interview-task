"""Disk cache for the two paid stages (discovery, enrichment).

Only API/model responses land here, so the cache is always regenerable and is
gitignored. With ``use_cache=False`` reads are skipped but writes still happen,
so a forced refresh leaves the cache warm for the next run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class JsonCache:
    def __init__(self, root: Path, use_cache: bool = True) -> None:
        self.root = root
        self.use_cache = use_cache

    def path(self, bucket: str, key: str) -> Path:
        return self.root / bucket / f"{_UNSAFE.sub('_', key)}.json"

    """
    Read cached JSON format events
    """
    def get(self, bucket: str, key: str) -> Any | None:
        if not self.use_cache:
            return None
        path = self.path(bucket, key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A truncated cache file should cost a re-fetch, not crash the run.
            return None

    """
    Cache JSON format events
    """
    def set(self, bucket: str, key: str, value: Any) -> None:
        path = self.path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
