import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ID = os.getenv("STEAM_ID", "")

# RetroAchievements Web API (https://retroachievements.org/APIDemo.php).
# z=username, y=api key — та же схема авторизации, что у Steam-ключа: не
# требует OAuth, один статический токен на весь запрос.
RA_USERNAME = os.getenv("RA_USERNAME", "")
RA_API_KEY = os.getenv("RA_API_KEY", "")

# Необязательная защита дашборда на уровне самого приложения (см. web.py).
# Актуальна в первую очередь, если install.sh НЕ настраивал домен/Caddy —
# тогда сервис слушает 0.0.0.0:8000 без всякой авторизации. Если оба поля
# пустые (по умолчанию) — авторизация отключена, поведение как раньше.
DASHBOARD_LOGIN = os.getenv("DASHBOARD_LOGIN", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / "cache"
LOG_FILE = BASE_DIR / "logs.txt"

# Локальный кэш картинок игр — реально СКАЧАННЫЕ файлы (не просто URL),
# отдаются через уже смонтированный /static (см. web.py). После первого
# скачивания браузер грузит их с вашего же сервера, а не с CDN Steam
# каждый раз заново.
GAME_IMAGES_DIR = BASE_DIR / "static" / "game_images"

DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)
GAME_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# Пауза между запросами к Steam API (сек), чтобы не словить бан за флуд.
# Это ГЛОБАЛЬНЫЙ лимит на весь процесс — соблюдается общим throttle'ом,
# даже если запросы идут параллельно из нескольких потоков (см. steam_api.py).
# Основной Web API гораздо терпимее к частоте запросов, чем витрина ниже —
# 0.25с (4 запроса/сек) на практике не приводит к 429, а на 400+ играх
# (по 1-2 запроса на игру для достижений) экономит несколько минут за прогон.
# Если всё же словите массовые 429 — код уже ретраит с экспоненциальной
# паузой (см. get_player_achievements/get_global_achievement_percentages),
# просто увеличьте это значение обратно.
REQUEST_DELAY = 0.25

# Сколько запросов к основному Web API можно держать "в полёте" одновременно.
# Ускоряет прогон за счёт параллелизма сетевого ожидания (RTT), не нарушая
# общий REQUEST_DELAY — воркеры делят одну и ту же паузу между собой.
API_CONCURRENCY = 10

# Отдельная, более медленная пауза для store.steampowered.com (витрина цен) —
# у неё гораздо более жёсткий rate-limit (~200 запросов/5 минут на IP), чем у
# основного Web API. 0.8с (~1.25 запроса/сек) — уже не самый консервативный
# вариант, но всё ещё заметно ниже потолка Steam; retry-с-backoff на 429
# (см. _get_store) подхватит редкие всплески без потери данных.
STORE_REQUEST_DELAY = 0.8

# Витрина терпит меньше параллелизма, чем основной API — берём с запасом.
STORE_CONCURRENCY = 5

# Регион для запроса цены. ТОЛЬКО US — единственный регион, который Steam
# гарантированно денежит в USD. Раньше тут был список регионов на случай,
# если игра снята с продажи в США (пробовали eu/gb/ru/kz), но это ломало
# требование "всё в долларах": appdetails всегда отдаёт цену в валюте того
# региона, который запросили, а не в USD, и цены из RU/KZ мешались с
# долларами один-в-один без конвертации. Теперь считаем строго в USD (см.
# get_app_price): если игры нет в US-сторе — она просто попадёт в "цена не
# найдена", а не добавит в сумму библиотеки цифру в чужой валюте.
PRICE_FALLBACK_COUNTRIES = ["us"]

# Сколько часов кэш считается свежим для ГЛОБАЛЬНОЙ статистики достижений
# (% игроков, открывших каждую ачивку — общая по игре, не привязана к вам
# лично). Меняется очень медленно, неделя — разумный запас, не 20 часов:
# на "Обновить всё" эта часть исторически была узким местом, хотя реально
# обновлять её так часто незачем (список игр и личный прогресс всё равно
# пересчитываются каждый раз заново).
CACHE_TTL_HOURS = 24 * 7

# "Умный" кэш личных достижений (см. fetch_achievements_stats) — свежесть
# зависит от категории игры, а не единый TTL для всех:
#   - игра БЕЗ единого достижения — почти никогда не обзаводится ачивками
#     задним числом, можно кэшировать надолго
NO_ACHIEVEMENTS_CACHE_TTL_HOURS = 24 * 30
#   - игра пройдена на 100% — редко меняется (разве что вышло DLC с новыми
#     ачивками), кэшируем на неделю
COMPLETED_ACHIEVEMENTS_CACHE_TTL_HOURS = 24 * 7
#   - игра "в процессе" (не 0%, не 100%) — тут как раз происходит движение,
#     кэш не используется вообще, всегда свежий запрос (см. fetch_achievements_stats)

MANUAL_APPIDS_FILE = BASE_DIR / "manual_appids.json"

GAMES_LIST_FILE = DATA_DIR / "games_list.json"
IMAGES_FILE = DATA_DIR / "images.json"
ACHIEVEMENTS_STATS_FILE = DATA_DIR / "achievements_stats.json"
LIBRARY_COST_FILE = DATA_DIR / "library_cost.json"
REVIEWS_FILE = DATA_DIR / "reviews.json"
REPORT_MD_FILE = DATA_DIR / "report.md"
REPORT_JSON_FILE = DATA_DIR / "report.json"
STATUS_FILE = DATA_DIR / "status.json"

# --- RetroAchievements: отдельные файлы отчёта/статуса, свой пайплайн,
# чтобы ничего не пересекалось со Steam-данными выше. ---
RETRO_REPORT_JSON_FILE = DATA_DIR / "retro_report.json"
RETRO_STATUS_FILE = DATA_DIR / "retro_status.json"

# Игры "в процессе" пересчитываем всегда; замастеренные (100% hardcore) и
# полностью софткорные (100% softcore, mastery уже невозможен) — редко
# меняются, кэшируем подолгу. Та же идея, что NO_ACHIEVEMENTS_CACHE_TTL_HOURS
# / COMPLETED_ACHIEVEMENTS_CACHE_TTL_HOURS у Steam-версии.
RETRO_NO_PROGRESS_CACHE_TTL_HOURS = 24 * 30
RETRO_MASTERED_CACHE_TTL_HOURS = 24 * 7

# Список ачивок игры (схема + описания + иконки) в модалке "показать все
# ачивки" — не зависит от вас лично, меняется почти никогда (разве что автор
# сета добавит новую ачивку). Кэшируем надолго и для Steam, и для RA.
ACHIEVEMENT_SCHEMA_CACHE_TTL_HOURS = 24 * 30


def validate_config():
    missing = []
    if not STEAM_API_KEY:
        missing.append("STEAM_API_KEY")
    if not STEAM_ID:
        missing.append("STEAM_ID")
    if missing:
        raise RuntimeError(
            f"В .env не заданы переменные: {', '.join(missing)}. "
            f"Скопируйте .env.example в .env и заполните их."
        )


def validate_retro_config():
    missing = []
    if not RA_USERNAME:
        missing.append("RA_USERNAME")
    if not RA_API_KEY:
        missing.append("RA_API_KEY")
    if missing:
        raise RuntimeError(
            f"В .env не заданы переменные: {', '.join(missing)}. "
            f"Получить RA_API_KEY можно в настройках профиля на "
            f"retroachievements.org (Settings → Keys)."
        )
