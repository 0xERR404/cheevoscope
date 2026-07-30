"""
RA-аналог stats.py: собирает retro_report.json из данных RetroAchievements.

В отличие от Steam-версии, "список игр с прогрессом" — это ОДИН вызов
(API_GetUserCompletionProgress, постранично), а не перебор appid по одному —
поэтому здесь нет отдельного fetch_games_list. Per-game вызов
(GetGameInfoAndUserProgress) нужен только там, где нужны реальные Points
(очки), а не просто % — это "полный" режим и он же прогревает кэш для
модалки ачивок (см. achievement_details.py).
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

from . import retro_api
from .cache import cache_get_with_age, cache_set
from .io_utils import atomic_write_json
from .stats import compute_rarity_tiers
from .config import (
    RETRO_REPORT_JSON_FILE,
    RETRO_NO_PROGRESS_CACHE_TTL_HOURS,
    RETRO_MASTERED_CACHE_TTL_HOURS,
)
from .logger_setup import get_logger

logger = get_logger()

ProgressCB = Optional[Callable[[str, int, int], None]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_parallel(items: list, worker, max_workers: int, stage: str, progress_cb: ProgressCB) -> dict:
    done_lock = threading.Lock()
    done = 0
    total = len(items)
    results = {}

    def _wrapped(item):
        return item["GameID"], worker(item)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_wrapped, it) for it in items]
        for fut in as_completed(futures):
            game_id, res = fut.result()
            results[game_id] = res
            if progress_cb:
                with done_lock:
                    done += 1
                    current = done
                progress_cb(stage, current, total)
    return results


def fetch_profile() -> dict:
    """Профиль: очки, RetroPoints, ранг. Меняется часто (очки растут) — без долгого кэша."""
    return retro_api.get_user_profile()


def fetch_completion_progress() -> list:
    """Список всех игр с прогрессом — основной источник для games-grid."""
    return retro_api.get_user_completion_progress()


def fetch_awards() -> list:
    return retro_api.get_user_awards()


def fetch_recent_achievements() -> list:
    return retro_api.get_user_recent_achievements()


def _game_detail_cache_ttl(progress_entry: dict) -> float:
    """
    Тот же принцип "умного" TTL, что у Steam-версии
    (см. stats._achievements_cache_ttl_hours): игра без единого прогресса
    почти не меняется, замастеренная (100% hardcore) — тоже, "в процессе" —
    всегда свежая.
    """
    max_possible = progress_entry.get("MaxPossible") or 0
    num_awarded_hc = progress_entry.get("NumAwardedHardcore") or 0
    num_awarded = progress_entry.get("NumAwarded") or 0
    if num_awarded == 0:
        return RETRO_NO_PROGRESS_CACHE_TTL_HOURS
    if max_possible and num_awarded_hc == max_possible:
        return RETRO_MASTERED_CACHE_TTL_HOURS
    return 0.0


def _fetch_one_game_detail(progress_entry: dict) -> dict:
    """
    Дотягивает GetGameInfoAndUserProgress для одной игры — нужен ради
    реальных Points (очков), которых нет в completion-progress. Кэшируется
    per-game под ключом retro_detail_{game_id}, той же логикой TTL, что и
    личные ачивки в Steam-версии.

    Побочный эффект (полезный): кэш этого вызова — тот же самый, который
    читает achievement_details.get_retro_game_achievements для модалки, так
    что после "Обновить всё" модалка открывается по всем играм мгновенно, без
    похода в сеть.
    """
    game_id = progress_entry["GameID"]
    cache_key = f"retro_detail_{game_id}"
    cached = cache_get_with_age(cache_key)
    ttl = _game_detail_cache_ttl(progress_entry)
    if cached is not None:
        data, age_hours = cached
        if age_hours <= ttl and isinstance(data, dict):
            return data

    detail = retro_api.get_game_info_and_user_progress(game_id)
    cache_set(cache_key, detail)
    return detail


def fetch_game_details(progress_list: list, progress_cb: ProgressCB = None) -> dict:
    """
    Полный прогон по всем играм — используется в "Обновить всё" (полный
    режим), считает реальные очки по консолям. В "быстром" режиме этот шаг
    пропускается (см. retro_pipeline._run_quick_pipeline) — очки по
    консолям в этом случае просто не обновляются в текущем прогоне, но
    остаются из предыдущего полного прогона (report хранит их отдельно).
    """
    return _run_parallel(progress_list, _fetch_one_game_detail, 3, "retro_game_details", progress_cb)


def load_retro_report() -> dict:
    if not RETRO_REPORT_JSON_FILE.exists():
        return {}
    try:
        with open(RETRO_REPORT_JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def generate_report(profile: dict, progress_list: list, awards: list, recent: list, game_details: dict) -> dict:
    games = []
    points_by_console = {}
    total_hardcore_earned = 0
    total_softcore_earned = 0
    total_possible = 0
    mastered_count = 0
    completed_count = 0

    rarity_candidates = []

    for entry in progress_list:
        if not isinstance(entry, dict):
            logger.warning(f"RetroAchievements: пропускаю неожиданный элемент прогресса (не объект): {entry!r}")
            continue
        game_id = entry.get("GameID")
        max_possible = entry.get("MaxPossible") or 0
        num_awarded = entry.get("NumAwarded") or 0
        num_awarded_hc = entry.get("NumAwardedHardcore") or 0
        console = entry.get("ConsoleName", "—")

        hardcore_pct = round(100 * num_awarded_hc / max_possible, 1) if max_possible else 0.0
        softcore_pct = round(100 * num_awarded / max_possible, 1) if max_possible else 0.0
        is_mastered = max_possible > 0 and num_awarded_hc == max_possible
        is_completed = max_possible > 0 and num_awarded == max_possible and not is_mastered

        if is_mastered:
            mastered_count += 1
        elif is_completed:
            completed_count += 1

        total_hardcore_earned += num_awarded_hc
        total_softcore_earned += num_awarded
        total_possible += max_possible

        detail = game_details.get(game_id) or {}
        if not isinstance(detail, dict):
            # Защита от уже испорченного кэша с прошлого падения (см.
            # retro_api._ensure_dict) — просто считаем, что деталей нет.
            detail = {}
        detail_achievements = (detail.get("Achievements") or {}).values()
        hardcore_points = sum(
            int(a.get("Points") or 0) for a in detail_achievements if a.get("DateEarnedHardcore")
        )
        softcore_points = sum(
            int(a.get("Points") or 0) for a in detail_achievements if a.get("DateEarned")
        )
        if detail_achievements:
            bucket = points_by_console.setdefault(console, {"hardcore": 0, "softcore": 0})
            bucket["hardcore"] += hardcore_points
            bucket["softcore"] += softcore_points

        # Кандидаты для библиотечного топ-10 самых редких — только открытые
        # ачивки, только там, где известен знаменатель (число игроков).
        total_players = detail.get("NumDistinctPlayersHardcore") or detail.get("NumDistinctPlayersCasual") or 0
        if total_players:
            for a in detail_achievements:
                if not a.get("DateEarned"):
                    continue
                num_a_hc = a.get("NumAwardedHardcore") or 0
                percent = round(100 * num_a_hc / total_players, 1)
                rarity_candidates.append({
                    "game_id": game_id,
                    "game": entry.get("Title", f"game {game_id}"),
                    "name": a.get("Title", ""),
                    "global_percent": percent,
                })

        games.append({
            "game_id": game_id,
            "title": entry.get("Title", f"game {game_id}"),
            "console": console,
            "image_icon": entry.get("ImageIcon", ""),
            "max_possible": max_possible,
            "num_awarded": num_awarded,
            "num_awarded_hardcore": num_awarded_hc,
            "hardcore_percent": hardcore_pct,
            "softcore_percent": softcore_pct,
            "status": "mastered" if is_mastered else ("completed" if is_completed else "in_progress"),
            "most_recent_award_date": entry.get("MostRecentAwardedDate"),
        })

    games.sort(key=lambda g: (g["most_recent_award_date"] or ""), reverse=True)

    overall_hardcore_percent = round(100 * total_hardcore_earned / total_possible, 1) if total_possible else 0.0

    awards_safe = [a for a in awards if isinstance(a, dict)]
    awards_out = [
        {
            "title": a.get("Title", ""),
            "console": a.get("ConsoleName", ""),
            "award_type": a.get("AwardType", ""),
            "award_date": a.get("AwardedAt", ""),
        }
        for a in sorted(awards_safe, key=lambda a: a.get("AwardedAt", ""), reverse=True)
    ]

    recent_out = [
        {
            "achievement": r.get("Title", ""),
            "game": r.get("GameTitle", ""),
            "date": r.get("Date", ""),
            "hardcore": bool(r.get("HardcoreMode")),
        }
        for r in recent
        if isinstance(r, dict)
    ]

    rarity_candidates.sort(key=lambda c: c["global_percent"])
    rarest_achievements = rarity_candidates[:10]
    rarity_tiers = compute_rarity_tiers(rarity_candidates)

    if not game_details:
        # Быстрый режим не дотягивает GetGameInfoAndUserProgress (дорого по
        # запросам) — сохраняем очки по консолям из предыдущего полного
        # прогона, чтобы блок не обнулялся на каждом "Обновить".
        previous = load_retro_report()
        points_by_console = previous.get("points_by_console", {})
        rarest_achievements = previous.get("rarest_achievements", [])
        rarity_tiers = previous.get("rarity_tiers", {"total_rated": 0, "counts": {}, "coolness_score": 0.0})

    report = {
        "generated_at": _now_iso(),
        "profile": {
            "username": profile.get("User", ""),
            "points": profile.get("TotalPoints", 0),
            "retro_points": profile.get("TotalTruePoints", 0),
            "rank": profile.get("Rank", None),
            "avatar_url": profile.get("UserPic", ""),
        },
        "summary": {
            "games_count": len(games),
            "games_mastered": mastered_count,
            "games_completed": completed_count,
            "achievements_hardcore_total": total_hardcore_earned,
            "achievements_softcore_total": total_softcore_earned,
            "achievements_possible_total": total_possible,
            "overall_hardcore_percent": overall_hardcore_percent,
        },
        "points_by_console": points_by_console,
        "games": games,
        "awards": awards_out,
        "recent_achievements": recent_out,
        "rarest_achievements": rarest_achievements,
        "rarity_tiers": rarity_tiers,
    }

    atomic_write_json(RETRO_REPORT_JSON_FILE, report)
    return report
