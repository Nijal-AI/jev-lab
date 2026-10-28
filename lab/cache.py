"""Cache des réponses : mémoire + data/cache.json (rechargé au démarrage).

Seules les réponses des vraies API sont mises en cache : le mock est déterministe
et ne doit pas polluer le fichier que l'on committe.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

from . import config

log = logging.getLogger("jev_lab")

_entries: dict[str, dict] = {}
_lock = asyncio.Lock()
cache_only = False


def key(provider: str, model: str, request: dict) -> str:
    blob = json.dumps({"p": provider, "m": model, "s": request["state"], "q": request["questions"]},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def load() -> None:
    if not config.CACHE_FILE.exists():
        return
    try:
        _entries.update(json.loads(config.CACHE_FILE.read_text()))
        log.info("Cache chargé : %d réponses", len(_entries))
    except (OSError, json.JSONDecodeError) as e:
        log.error("cache.json illisible, ignoré : %s", e)


def get(k: str) -> dict | None:
    return _entries.get(k)


def size() -> int:
    return len(_entries)


def dump() -> str:
    return json.dumps(_entries, ensure_ascii=False, indent=0, sort_keys=True)


def _write(text: str) -> None:
    tmp = config.CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, config.CACHE_FILE)


async def put(k: str, entry: dict) -> None:
    entry = {**entry, "cached_at": int(time.time())}
    async with _lock:
        _entries[k] = entry
        try:
            await asyncio.to_thread(_write, dump())
        except OSError as e:
            log.warning("cache.json non écrit : %s", e)
