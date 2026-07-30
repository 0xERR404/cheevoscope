"""
Данные для модалки "все ачивки игры + % редкости + у меня выбито/нет" —
одна и та же модалка на Steam- и RA-вкладках, разные источники данных.

Дёргается лениво по клику на плитку (см. web.py: /api/game/{appid}/achievements
и /api/retro/game/{game_id}/achievements), а не складывается в основной
report.json/retro_report.json — иначе тот разбухает в разы (список ачивок с
описаниями по каждой из 300+ игр, а не только числа).
"""
from . import steam_api, retro_api
from .cache import cached_call
from .config import ACHIEVEMENT_SCHEMA_CACHE_TTL_HOURS, CACHE_TTL_HOURS
from .logger_setup import get_logger

logger = get_logger()


def get_steam_game_achievements(appid: int) -> dict:
    """
    Собирает по одной игре: apiname, название, описание, иконка, % игроков
    (редкость), выбита ли у вас лично. Схема и глобальные % кэшируются
    надолго (см. ACHIEVEMENT_SCHEMA_CACHE_TTL_HOURS/CACHE_TTL_HOURS) — не
    зависят от вас лично; личный прогресс запрашивается каждый раз заново
    (дешёвый одиночный вызов, важна свежесть галочки "выбито").
    """
    schema = cached_call(
        f"schema_ru_{appid}",
        lambda: steam_api.get_schema_for_game(appid),
        ttl_hours=ACHIEVEMENT_SCHEMA_CACHE_TTL_HOURS,
    )
    if not schema:
        return {"appid": appid, "available": False, "achievements": []}

    global_pct = cached_call(
        f"global_pct_{appid}",
        lambda: steam_api.get_global_achievement_percentages(appid),
        ttl_hours=CACHE_TTL_HOURS,
    )
    pct_by_name = {a["name"]: a.get("percent") for a in global_pct}

    player_data = steam_api.get_player_achievements(appid)
    unlocked_by_name = {
        a["apiname"]: a.get("unlocktime")
        for a in player_data.get("achievements", [])
        if a.get("achieved") == 1
    }

    achievements = []
    for a in schema:
        apiname = a.get("name")
        raw_percent = pct_by_name.get(apiname)
        # Steam отдаёт percent СТРОКОЙ (например "45.9"), а не числом — round()
        # падает с TypeError на str. Приводим явно, с защитой на случай мусора.
        percent = None
        if raw_percent is not None:
            try:
                percent = float(raw_percent)
            except (TypeError, ValueError):
                percent = None
        achievements.append({
            "apiname": apiname,
            "name": a.get("displayName", apiname),
            "description": a.get("description", ""),
            "icon": a.get("icon", ""),
            "icon_gray": a.get("icongray", ""),
            "global_percent": round(percent, 1) if percent is not None else None,
            "unlocked": apiname in unlocked_by_name,
            "unlock_time": unlocked_by_name.get(apiname),
        })

    # Сначала самые редкие (интереснее видеть их первыми), неизвестная
    # редкость (None) — в конец, а не в начало (иначе перемешает список).
    achievements.sort(key=lambda a: (a["global_percent"] is None, a["global_percent"] or 0))

    return {"appid": appid, "available": True, "achievements": achievements}


def get_retro_game_achievements(game_id: int) -> dict:
    """
    RA отдаёт всё нужное одним вызовом (см. retro_api.get_game_info_and_user_progress) —
    для каждой ачивки уже есть и NumAwarded/NumAwardedHardcore (редкость), и
    DateEarned/DateEarnedHardcore (выбита ли у вас, в каком режиме).

    Кэш под тем же ключом, что использует retro_stats при полном обновлении
    (retro_detail_{game_id}) — если недавно был "Обновить всё", модалка
    открывается мгновенно без похода в сеть; иначе — свежий запрос.
    """
    from .cache import cache_get_with_age, cache_set  # локальный импорт — та же кэш-схема, что retro_stats

    cache_key = f"retro_detail_{game_id}"
    cached = cache_get_with_age(cache_key)
    detail = None
    if cached is not None:
        data, age_hours = cached
        if age_hours <= 6 and isinstance(data, dict):  # для модалки достаточно свежести в пределах нескольких часов
            detail = data
    if detail is None:
        detail = retro_api.get_game_info_and_user_progress(game_id)
        if detail:
            cache_set(cache_key, detail)

    if not detail or not detail.get("Achievements"):
        return {"game_id": game_id, "available": False, "achievements": []}

    total_players = detail.get("NumDistinctPlayersHardcore") or detail.get("NumDistinctPlayersCasual") or 0

    achievements_raw = detail.get("Achievements")
    if not isinstance(achievements_raw, dict):
        return {"game_id": game_id, "available": False, "achievements": []}

    achievements = []
    for a in achievements_raw.values():
        if not isinstance(a, dict):
            continue
        num_awarded_hc = a.get("NumAwardedHardcore") or 0
        rarity_hardcore = round(100 * num_awarded_hc / total_players, 1) if total_players else None
        achievements.append({
            "id": a.get("ID"),
            "name": a.get("Title", ""),
            "description": a.get("Description", ""),
            "points": a.get("Points", 0),
            "badge_url": f"https://retroachievements.org/Badge/{a.get('BadgeName')}.png" if a.get("BadgeName") else "",
            "global_percent": rarity_hardcore,
            "unlocked": bool(a.get("DateEarned")),
            "unlocked_hardcore": bool(a.get("DateEarnedHardcore")),
            "unlock_time": a.get("DateEarnedHardcore") or a.get("DateEarned"),
        })

    achievements.sort(key=lambda a: (a["global_percent"] is None, a["global_percent"] or 0))

    return {"game_id": game_id, "available": True, "achievements": achievements}
