"""
Обёртка над RetroAchievements Web API (https://retroachievements.org/APIDemo.php).

ВАЖНО ДЛЯ ПРОВЕРЯЮЩЕГО КОД: retroachievements.org недоступен из песочницы, в
которой писался этот файл (сетевой доступ туда закрыт на уровне окружения),
поэтому имена полей ответа взяты по документации/памяти, а не проверены на
живом ответе с вашим ключом. Перед тем как полагаться на пайплайн в проде —
прогоните verify_connection() и один реальный вызов каждой функции, посмотрите
глазами на реальный JSON (см. блок if __name__ == "__main__" внизу файла) и
поправьте имена ключей, если что-то разошлось.

Авторизация — статические query-параметры z (username) и y (api key),
не OAuth, аналогично STEAM_API_KEY в steam_api.py.
"""
import threading
import time

import requests

from .config import RA_USERNAME, RA_API_KEY
from .logger_setup import get_logger

logger = get_logger()

BASE_URL = "https://retroachievements.org/API"

# RA не документирует точный rate-limit так же явно, как Steam — по опыту
# сообщества, где-то в районе нескольких запросов в секунду это безопасно.
# Берём консервативную паузу, чтобы не словить временный бан по IP/ключу.
REQUEST_DELAY = 0.5
CONCURRENCY = 3


class RateLimiter:
    """Тот же принцип, что RateLimiter в steam_api.py — см. комментарий там."""

    def __init__(self, min_interval: float, max_concurrency: int):
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._min_interval = min_interval
        self._sem = threading.Semaphore(max_concurrency)

    def __enter__(self):
        self._sem.acquire()
        with self._lock:
            now = time.time()
            start_at = max(now, self._next_slot)
            self._next_slot = start_at + self._min_interval
        wait = start_at - now
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._sem.release()
        return False


_limiter = RateLimiter(REQUEST_DELAY, CONCURRENCY)


def _get(endpoint: str, params: dict | None = None, timeout: int = 15, max_retries: int = 3) -> dict:
    url = f"{BASE_URL}/{endpoint}.php"
    query = {"z": RA_USERNAME, "y": RA_API_KEY, **(params or {})}
    for attempt in range(max_retries):
        with _limiter:
            try:
                resp = requests.get(url, params=query, timeout=timeout)
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                logger.warning(f"Не удалось получить {endpoint} от RA API: {e}")
                return {}
        if resp.status_code == 429 and attempt < max_retries - 1:
            wait = 2 ** (attempt + 1)
            logger.warning(f"429 от RA API ({endpoint}), жду {wait}с...")
            time.sleep(wait)
            continue
        try:
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            logger.warning(f"Не удалось получить {endpoint} от RA API: {e}")
            return {}
    return {}


def _ensure_dict(data, endpoint: str) -> dict:
    """
    RA иногда отвечает НЕ JSON-объектом, а голой строкой — например, при
    неверном RA_USERNAME/RA_API_KEY можно получить валидный JSON вида
    "Invalid API Key" (это парсится как Python str, не dict). Без этой
    проверки любой .get(...) на таком ответе роняет весь пайплайн с
    невнятным "'str' object has no attribute 'get'". Здесь — явная
    проверка типа с логированием реального содержимого ответа, чтобы в
    логе сразу было видно текст ошибки от RA, а не голый traceback.
    """
    if isinstance(data, dict):
        return data
    logger.warning(f"RA API {endpoint} вернул не объект, а {type(data).__name__}: {data!r}")
    return {}


def get_user_profile() -> dict:
    """
    API_GetUserProfile — аватар, ранг, очки, RetroPoints (софткор-взвешенные
    "true points"), motto. Основной источник для шапки RA-вкладки.
    """
    return _ensure_dict(_get("API_GetUserProfile", {"u": RA_USERNAME}), "API_GetUserProfile")


def get_user_completion_progress(max_pages: int = 20) -> list:
    """
    API_GetUserCompletionProgress — постранично ВСЕ игры пользователя с
    прогрессом (hardcore/softcore %, очки, консоль). Пагинация обязательна:
    один вызов отдаёт ограниченный "Count" записей за раз (типично 100),
    "Total" — сколько всего. Собираем все страницы перед агрегацией,
    max_pages — защита от бесконечного цикла при неожиданном ответе API.
    """
    results = []
    offset = 0
    page_size = 100
    for _ in range(max_pages):
        raw = _get(
            "API_GetUserCompletionProgress",
            {"u": RA_USERNAME, "c": page_size, "o": offset},
        )
        data = _ensure_dict(raw, "API_GetUserCompletionProgress")
        if not data:
            break
        chunk = [g for g in data.get("Results", []) if isinstance(g, dict)]
        results.extend(chunk)
        total = data.get("Total", len(results))
        offset += page_size
        if offset >= total or not chunk:
            break
    return results


def get_game_info_and_user_progress(game_id: int) -> dict:
    """
    API_GetGameInfoAndUserProgress — для конкретной игры: метаданные + ПОЛНЫЙ
    список ачивок с одновременно и тем, сколько игроков её открыли
    (NumAwarded/NumAwardedHardcore), и открыли ли лично вы (DateEarned/
    DateEarnedHardcore). Этого одного вызова достаточно и для отчёта по игре,
    и для модалки "показать все ачивки" — отдельный запрос на редкость
    каждой ачивки (API_GetAchievementUnlocks) не нужен.
    """
    raw = _get("API_GetGameInfoAndUserProgress", {"u": RA_USERNAME, "g": game_id})
    return _ensure_dict(raw, "API_GetGameInfoAndUserProgress")


def get_user_awards() -> list:
    """
    API_GetUserAwards — витрина "трофеев": Mastery (100% hardcore),
    Completion (100% softcore), event-бейджи и т.п. У Steam-версии прямого
    аналога нет — это то, чего в стим-дашборде нет вообще.
    """
    data = _ensure_dict(_get("API_GetUserAwards", {"u": RA_USERNAME}), "API_GetUserAwards")
    return [a for a in data.get("VisibleUserAwards", []) if isinstance(a, dict)]


def get_user_recent_achievements(minutes: int = 60 * 24 * 14) -> list:
    """
    API_GetUserRecentAchievements — лента последних разлоченных ачивок.
    minutes по умолчанию — 2 недели, для секции "последняя активность".
    """
    data = _get("API_GetUserRecentAchievements", {"u": RA_USERNAME, "m": minutes})
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def get_console_ids() -> list:
    """API_GetConsoleIDs — справочник ID → название консоли, для подписей платформ."""
    data = _get("API_GetConsoleIDs")
    return data if isinstance(data, list) else []


def verify_connection() -> bool:
    """Быстрая проверка, что RA_USERNAME/RA_API_KEY реально валидны."""
    profile = get_user_profile()
    return bool(profile.get("User"))


if __name__ == "__main__":
    # Ручная проверка — запустить: python -m app.retro_api
    # (с .env, где заполнены RA_USERNAME/RA_API_KEY)
    import json
    print("profile:", json.dumps(get_user_profile(), indent=2)[:1000])
    progress = get_user_completion_progress(max_pages=1)
    print("progress[0]:", json.dumps(progress[0], indent=2) if progress else "пусто")
