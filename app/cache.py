import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .config import CACHE_DIR, CACHE_TTL_HOURS

# Версия формата кэша. Поднимите её, если поменяете структуру данных внутри —
# старые файлы кэша перестанут считаться валидными и перескачаются заново.
CACHE_FORMAT_VERSION = 1


def _cache_path(key: str) -> Path:
    safe_key = key.replace("/", "_").replace(" ", "_")
    return CACHE_DIR / f"{safe_key}.json"


def cache_get_with_age(key: str) -> Optional[tuple]:
    """
    Возвращает (data, age_hours) БЕЗ проверки TTL — решение "ещё свежо или уже
    нет" принимает вызывающий код (например, в зависимости от того, какая
    это игра — см. fetch_achievements_stats: у "без ачивок"/"100% пройдено"
    свой, более длинный порог свежести, чем у "в процессе").
    Возвращает None, если записи в кэше нет вообще или она битая.
    """
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if payload.get("_version") != CACHE_FORMAT_VERSION:
        return None

    age_hours = (time.time() - payload.get("_cached_at", 0)) / 3600
    return payload.get("data"), age_hours


def cache_get(key: str, ttl_hours: float = CACHE_TTL_HOURS) -> Optional[Any]:
    """Обычная проверка TTL поверх cache_get_with_age — вернуть данные, только если не устарели."""
    cached = cache_get_with_age(key)
    if cached is None:
        return None
    data, age_hours = cached
    if age_hours > ttl_hours:
        return None
    return data


def cache_set(key: str, data: Any) -> None:
    path = _cache_path(key)
    payload = {
        "_version": CACHE_FORMAT_VERSION,
        "_cached_at": time.time(),
        "data": data,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def cached_call(key: str, fetch_fn: Callable[[], Any], ttl_hours: float = CACHE_TTL_HOURS) -> Any:
    """Вернуть данные из кэша, если они свежие; иначе вызвать fetch_fn(), сохранить и вернуть."""
    cached = cache_get(key, ttl_hours)
    if cached is not None:
        return cached
    fresh = fetch_fn()
    cache_set(key, fresh)
    return fresh
