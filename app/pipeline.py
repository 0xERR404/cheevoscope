import threading
import traceback

from . import stats
from .config import STATUS_FILE, CACHE_DIR, validate_config
from .logger_setup import get_logger
from .status_tracker import now_iso as _now_iso, make_status_tracker

logger = get_logger()

_lock = threading.Lock()
_is_running = False

read_status, _write_status, _progress_cb = make_status_tracker(STATUS_FILE)


def _run_quick_pipeline() -> None:
    """
    Кнопка "Обновить" — быстрое обновление, для скорости трогает по
    минимуму:

      1. Список игр (все источники, см. stats.fetch_games_list) — дёшево,
         это пара лёгких запросов, не сравнить с перебором appdetails по
         каждой игре.
      2. Достижения — для ВСЕХ игр, но за счёт "умного" кэша
         (см. _achievements_cache_ttl_hours) реально сходит в Steam только
         за играми "в процессе" и совсем новыми; пройденные на 100%/без
         единой ачивки берутся из кэша и не тормозят обновление.
      3. Картинки — ТОЛЬКО для НОВЫХ игр, которых не было в библиотеке на
         момент предыдущего обновления (сравниваем appid с тем, что лежало
         на диске до этого прогона). Для уже известных игр картинки не
         трогаем — не нужно перебирать appdetails по всей библиотеке ради
         скорости этой кнопки.

    Цены и отзывы этот режим НЕ считает вообще — если они нужны свежими,
    для этого есть "Обновить всё".
    """
    previous = stats.load_games_list()
    previous_appids = {g["appid"] for g in previous["games"]} if previous else set()

    _write_status(state="running", stage="games_list", progress=None, error=None)
    games_data = stats.fetch_games_list()
    images_data = stats.load_images()
    stats.generate_report(
        games_data, stats.load_achievements_stats(), stats.load_library_cost(), stats.load_reviews(), images_data
    )

    new_games = [g for g in games_data["games"] if g["appid"] not in previous_appids]
    if new_games:
        logger.info(f"Обнаружено новых игр: {len(new_games)} — подтягиваю для них картинки.")
        _write_status(state="running", stage="images", progress=None)
        images_data = stats.fetch_game_images(new_games, _progress_cb)
        stats.generate_report(
            games_data, stats.load_achievements_stats(), stats.load_library_cost(), stats.load_reviews(), images_data
        )

    _write_status(state="running", stage="achievements", progress=None)
    achievements_data = stats.fetch_achievements_stats(games_data["games"], _progress_cb)

    _write_status(state="running", stage="report", progress=None)
    stats.generate_report(games_data, achievements_data, stats.load_library_cost(), stats.load_reviews(), images_data)


def _run_full_pipeline() -> None:
    """
    Кнопка "Обновить всё" — жёсткий пересчёт с нуля. Кэш ПОЛНОСТЬЮ чистится
    перед запуском (см. _clear_all_cache в start_refresh), после чего идёт
    полный прогон по всем данным, в таком порядке:

      1. Список игр (все источники сразу) + картинки
      2. Достижения
      3. Отзывы
      4. Цены

    После КАЖДОГО этапа пересобираем report.json теми данными, что уже
    есть — отчёт не ждёт конца всего прогона, а дополняется по ходу дела:
    сначала на экране появляется список игр с картинками, затем у части из
    них проставляются достижения, затем отзывы, затем цены.
    """
    _write_status(state="running", stage="games_list", progress=None, error=None)
    games_data = stats.fetch_games_list()
    stats.generate_report(
        games_data, stats.load_achievements_stats(), stats.load_library_cost(), stats.load_reviews(), stats.load_images()
    )

    _write_status(state="running", stage="images", progress=None)
    images_data = stats.fetch_game_images(games_data["games"], _progress_cb)
    stats.generate_report(
        games_data, stats.load_achievements_stats(), stats.load_library_cost(), stats.load_reviews(), images_data
    )

    _write_status(state="running", stage="achievements", progress=None)
    achievements_data = stats.fetch_achievements_stats(games_data["games"], _progress_cb)
    stats.generate_report(games_data, achievements_data, stats.load_library_cost(), stats.load_reviews(), images_data)

    _write_status(state="running", stage="reviews", progress=None)
    reviews_data = stats.fetch_reviews(games_data["games"], _progress_cb)
    stats.generate_report(games_data, achievements_data, stats.load_library_cost(), reviews_data, images_data)

    _write_status(state="running", stage="library_cost", progress=None)
    cost_data = stats.fetch_library_cost(games_data["games"], _progress_cb)

    _write_status(state="running", stage="report", progress=None)
    stats.generate_report(games_data, achievements_data, cost_data, reviews_data, images_data)


def _run_pipeline(mode: str) -> None:
    global _is_running
    try:
        validate_config()

        if mode == "full":
            removed = _clear_all_cache()
            logger.info(f"Обновить всё: кэш полностью очищен ({removed} файлов удалено).")
            _run_full_pipeline()
        else:
            _run_quick_pipeline()

        _write_status(state="done", stage=None, progress=None, last_success_at=_now_iso(), error=None)
        logger.info("Обновление успешно завершено.")
    except Exception as e:
        logger.error(f"Ошибка при обновлении: {e}\n{traceback.format_exc()}")
        _write_status(state="error", error=str(e))
    finally:
        with _lock:
            _is_running = False


def _clear_all_cache() -> int:
    """
    Чистит кэш перед "Обновить всё" — картинки/цены (общий ключ price_*),
    отзывы и ЛИЧНЫЕ достижения по каждой игре (player_ach_*). Это всё, что
    реально может "устареть" применительно к вам лично.

    Глобальную статистику ачивок (global_pct_*, % игроков на каждую ачивку
    по игре) НЕ трогаем — это общая по игре величина, не зависит от вас, и
    меняется очень медленно (недели-месяцы), см. CACHE_TTL_HOURS. Раньше её
    чистили вместе со всем остальным, и это было основным источником
    "тормозов" у "Обновить всё": по appid без изменений в данных всё равно
    гонялся полный повторный запрос.
    """
    removed = 0
    for pattern in ("price_*.json", "review_*.json", "player_ach_*.json"):
        for f in CACHE_DIR.glob(pattern):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def run_refresh_sync(mode: str = "quick") -> None:
    """
    То же самое, что делает _run_pipeline внутри фонового потока из
    start_refresh, но СИНХРОННО — для вызова из отдельного процесса
    (см. app/hourly_refresh.py), а не из живого запроса к сайту. Отдельный
    процесс не имеет смысла запускать в фоновом потоке демоном: он просто
    завершится раньше, чем поток успеет что-то сделать.
    """
    global _is_running
    with _lock:
        if _is_running:
            logger.info("Обновление уже идёт — пропускаю (Steam).")
            return
        _is_running = True
    _run_pipeline(mode)


def start_refresh(mode: str = "quick") -> bool:
    """
    Запускает нужный режим обновления в фоновом потоке. Возвращает False,
    если уже идёт обновление.

    mode="quick" — кнопка "Обновить": список игр (+ появившиеся новые) и
                   достижения. Цены/отзывы не трогает, картинки тянет
                   только для новых игр. Быстро.
    mode="full"  — кнопка "Обновить всё": список игр, картинки, достижения,
                   отзывы, цены — ВСЁ, с предварительной очисткой личного
                   кэша (цены/картинки, отзывы, ваш прогресс по ачивкам —
                   см. _clear_all_cache). Общая по игре статистика ачивок
                   (не привязанная к вам) из кэша не выкидывается — она не
                   меняется от запуска к запуску, и её пересчёт только
                   замедлял бы кнопку без всякой пользы.
    """
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
