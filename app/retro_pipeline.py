import threading
import traceback

from . import retro_stats
from .config import RETRO_STATUS_FILE, validate_retro_config
from .logger_setup import get_logger
from .status_tracker import now_iso as _now_iso, make_status_tracker

logger = get_logger()

_lock = threading.Lock()
_is_running = False

read_status, _write_status, _progress_cb = make_status_tracker(RETRO_STATUS_FILE)


def _run_quick_pipeline() -> None:
    """
    "Обновить": профиль, список игр с прогрессом, награды, лента активности.
    НЕ дотягивает точные очки по консолям (см. retro_stats.fetch_game_details) —
    это самая дорогая по запросам часть (1 вызов на игру), и для быстрой
    кнопки того не стоит: hardcore/softcore % по каждой игре и так уже точны
    из completion-progress.
    """
    _write_status(state="running", stage="profile", progress=None, error=None)
    profile = retro_stats.fetch_profile()

    _write_status(state="running", stage="games", progress=None)
    progress_list = retro_stats.fetch_completion_progress()

    _write_status(state="running", stage="awards", progress=None)
    awards = retro_stats.fetch_awards()
    recent = retro_stats.fetch_recent_achievements()

    _write_status(state="running", stage="report", progress=None)
    retro_stats.generate_report(profile, progress_list, awards, recent, game_details={})


def _run_full_pipeline() -> None:
    """
    "Обновить всё": то же самое + точные очки по консолям (per-game вызов
    GetGameInfoAndUserProgress по каждой игре — используется своим
    per-game кэшем, см. retro_stats._fetch_one_game_detail, поэтому повторные
    полные прогоны быстры для игр, чей прогресс не менялся).
    """
    _write_status(state="running", stage="profile", progress=None, error=None)
    profile = retro_stats.fetch_profile()

    _write_status(state="running", stage="games", progress=None)
    progress_list = retro_stats.fetch_completion_progress()
    retro_stats.generate_report(profile, progress_list, [], [], game_details={})

    _write_status(state="running", stage="game_details", progress=None)
    game_details = retro_stats.fetch_game_details(progress_list, _progress_cb)

    _write_status(state="running", stage="awards", progress=None)
    awards = retro_stats.fetch_awards()
    recent = retro_stats.fetch_recent_achievements()

    _write_status(state="running", stage="report", progress=None)
    retro_stats.generate_report(profile, progress_list, awards, recent, game_details)


def _run_pipeline(mode: str) -> None:
    global _is_running
    try:
        validate_retro_config()

        if mode == "full":
            _run_full_pipeline()
        else:
            _run_quick_pipeline()

        _write_status(state="done", stage=None, progress=None, last_success_at=_now_iso(), error=None)
        logger.info("RetroAchievements: обновление успешно завершено.")
    except Exception as e:
        logger.error(f"RetroAchievements: ошибка при обновлении: {e}\n{traceback.format_exc()}")
        _write_status(state="error", error=str(e))
    finally:
        with _lock:
            _is_running = False


def run_refresh_sync(mode: str = "quick") -> None:
    """См. app.pipeline.run_refresh_sync — тот же смысл, для RA-пайплайна."""
    global _is_running
    with _lock:
        if _is_running:
            logger.info("RetroAchievements: обновление уже идёт — пропускаю.")
            return
        _is_running = True
    _run_pipeline(mode)


def start_refresh(mode: str = "quick") -> bool:
    if mode not in ("quick", "full"):
        raise ValueError(f"Неизвестный режим обновления: {mode!r}")

    global _is_running
    with _lock:
        if _is_running:
            return False
        _is_running = True

    thread = threading.Thread(target=_run_pipeline, args=(mode,), daemon=True)
    thread.start()
    return True
