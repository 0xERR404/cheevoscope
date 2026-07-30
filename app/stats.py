import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import steam_api
from .cache import cached_call, cache_get, cache_set, cache_get_with_age
from .io_utils import atomic_write_json
from .config import (
    GAMES_LIST_FILE,
    IMAGES_FILE,
    GAME_IMAGES_DIR,
    ACHIEVEMENTS_STATS_FILE,
    LIBRARY_COST_FILE,
    REVIEWS_FILE,
    REPORT_MD_FILE,
    REPORT_JSON_FILE,
    MANUAL_APPIDS_FILE,
    STEAM_ID,
    NO_ACHIEVEMENTS_CACHE_TTL_HOURS,
    COMPLETED_ACHIEVEMENTS_CACHE_TTL_HOURS,
    API_CONCURRENCY,
    STORE_CONCURRENCY,
    ACHIEVEMENT_SCHEMA_CACHE_TTL_HOURS,
)
from .logger_setup import get_logger

logger = get_logger()

ProgressCB = Optional[Callable[[str, int, int], None]]


def _run_parallel(games: list, worker, max_workers: int, stage: str, progress_cb: ProgressCB) -> dict:
    """
    Прогоняет worker(game) по списку игр в пуле потоков и стримит прогресс.
    Результаты собираются в словарь по appid — порядок завершения потоков не
    важен, вызывающий код всегда обращается к ним по ключу appid.
    """
    done_lock = threading.Lock()
    done = 0
    total = len(games)
    results = {}

    def _wrapped(game):
        return game["appid"], worker(game)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_wrapped, g) for g in games]
        for fut in as_completed(futures):
            appid, res = fut.result()
            results[appid] = res
            if progress_cb:
                with done_lock:
                    done += 1
                    current = done
                progress_cb(stage, current, total)
    return results


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _achievements_cache_ttl_hours(cached_data: dict) -> float:
    """
    Порог свежести кэша личных достижений по категории игры:
      - без единого достижения — почти никогда не меняется, кэш надолго
      - пройдена на 100% — редко меняется, кэш на неделю
      - "в процессе" — тут всё интересное, кэш не используется вообще (0)
    """
    achievements = cached_data.get("achievements", [])
    total = len(achievements)
    if total == 0:
        return NO_ACHIEVEMENTS_CACHE_TTL_HOURS
    unlocked = sum(1 for a in achievements if a.get("achieved") == 1)
    if unlocked == total:
        return COMPLETED_ACHIEVEMENTS_CACHE_TTL_HOURS
    return 0.0


def _load_manual_appids() -> list:
    """
    Игры, которые Steam не отдаёт НИ ОДНИМ официальным Web API-эндпоинтом —
    единичные appid'ы, которые есть в аккаунте, но не видны ни GetOwnedGames,
    ни GetRecentlyPlayedGames. Владелец добавляет их вручную в
    manual_appids.json ({"appid": ..., "name": "..."}), и они подмешиваются
    в библиотеку здесь. Достижения/цена/отзывы для них всё равно тянутся
    напрямую по appid — время в игре показать не можем, Steam его не отдаёт
    вне GetOwnedGames.
    """
    if not MANUAL_APPIDS_FILE.exists():
        return []
    try:
        with open(MANUAL_APPIDS_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Не удалось прочитать manual_appids.json: {e}")
        return []
    return entries if isinstance(entries, list) else []


# ---------- Этап 2: библиотека и время ----------

def fetch_games_list() -> dict:
    """
    Собирает ПОЛНЫЙ список игр аккаунта, объединяя все источники, какие
    только доступны — без разделения на "основной список" / "ожидает
    синхронизации" / "добавлено вручную". На выходе просто плоский список
    игр: если appid где-то нашёлся, он в библиотеке, точка.

    Источники (по порядку, поздние не переопределяют более ранние — только
    дополняют недостающие appid):

      1. GetOwnedGames — официальный основной источник с реальным playtime.
      2. GetRecentlyPlayedGames — недавняя активность за 2 недели; ловит
         игры, которые GetOwnedGames ещё не успел отреплицировать (задержка
         от минут до часов на свежие покупки/сессии).
      3. manual_appids.json — appid, вписанные владельцем вручную, потому
         что Steam их не отдаёт вообще ни одним официальным Web API
         эндпоинтом.
      4. Публичный XML-фид страницы "Игры" профиля (см.
         steam_api.get_full_library) — тот же список, что виден на
         steamcommunity.com/id/<ник>/games/?tab=all. Ловит F2P-игры, ни
         разу не запущенные через клиент (GetOwnedGames их принципиально
         не отдаёт), и игры из Family Sharing, которые видны в клиенте, но
         не "куплены" на аккаунт. Это единственный шаг, требующий
         публичных "Сведений об играх" в приватности профиля — если они
         скрыты, шаг тихо ничего не добавляет (см. get_full_library),
         остальные три источника это не затрагивает.

    Время в игре есть только у источника (1) и частично у (2); для игр,
    найденных только через (3)/(4), playtime_forever = 0 — Steam его вне
    GetOwnedGames в принципе не отдаёт.
    """
    logger.info("Запрашиваю список игр (GetOwnedGames)...")
    owned = steam_api.get_owned_games()
    known_appids = {g["appid"] for g in owned}
    extra_games = []

    for g in steam_api.get_recently_played_games():
        appid = g["appid"]
        if appid in known_appids:
            continue
        extra_games.append(
            {
                "appid": appid,
                "name": g.get("name") or f"appid {appid}",
                "playtime_forever": g.get("playtime_forever", 0),
                "_unverified": True,
            }
        )
        known_appids.add(appid)

    for entry in _load_manual_appids():
        appid = entry.get("appid")
        if appid is None or appid in known_appids:
            continue
        extra_games.append(
            {"appid": appid, "name": entry.get("name") or f"appid {appid}", "playtime_forever": 0, "_unverified": True}
        )
        known_appids.add(appid)

    try:
        steamid64 = steam_api.resolve_steamid64(STEAM_ID)
        full_library = steam_api.get_full_library(steamid64)
    except Exception as e:  # noqa: BLE001 — резервный шаг, не должен ронять весь пайплайн
        logger.warning(f"Не удалось сверить полный список библиотеки через профиль: {e}")
        full_library = []

    library_extra_count = 0
    for g in full_library:
        appid = g["appid"]
        if appid in known_appids:
            continue
        extra_games.append({"appid": appid, "name": g["name"], "playtime_forever": 0, "_unverified": True})
        known_appids.add(appid)
        library_extra_count += 1

    if extra_games:
        logger.info(
            f"Дополнительно найдено {len(extra_games)} игр(ы) вне GetOwnedGames "
            f"(из них {library_extra_count} — только через полный список библиотеки)."
        )

    games = owned + extra_games
    total_minutes = sum(g.get("playtime_forever", 0) for g in games)
    result = {
        "fetched_at": _now_iso(),
        "games_count": len(games),
        "total_hours": round(total_minutes / 60, 1),
        "games": games,
    }
    atomic_write_json(GAMES_LIST_FILE, result)
    logger.info(f"Найдено игр: {result['games_count']}, общее время: {result['total_hours']} часов")
    return result


# ---------- Этап 4: достижения ----------

def _fetch_one_game_achievements(game: dict) -> Optional[dict]:
    appid = game["appid"]
    name = game.get("name", f"appid {appid}")

    global_pct = cached_call(
        f"global_pct_{appid}",
        lambda a=appid: steam_api.get_global_achievement_percentages(a),
    )

    # Личные достижения — "умный" кэш (см. _achievements_cache_ttl_hours):
    # игры без ачивок/пройденные на 100% берём из кэша подолгу, игры "в
    # процессе" — всегда свежими. Игры, найденные не через основной
    # GetOwnedGames (недавняя активность/manual_appids.json/полный список
    # библиотеки — см. fetch_games_list), кэш вообще не используют: данные
    # по ним могут быть ещё нестабильны, и залипание "нет ачивок" на 30 дней
    # тут не годится.
    skip_cache = bool(game.get("_unverified"))
    cache_key = f"player_ach_{appid}"
    player_data = None
    if not skip_cache:
        cached = cache_get_with_age(cache_key)
        if cached is not None:
            cached_data, age_hours = cached
            if age_hours <= _achievements_cache_ttl_hours(cached_data):
                player_data = cached_data

    if player_data is None:
        player_data = steam_api.get_player_achievements(appid)
        if not player_data.get("transient_error") and not skip_cache:
            cache_set(cache_key, player_data)

    player_achievements = player_data.get("achievements", [])
    total_achievements = len(player_achievements)  # авторитетный источник — всегда полный список ачивок игры
    if total_achievements == 0:
        return None

    unlocked = [a for a in player_achievements if a.get("achieved") == 1]
    unlocked_count = len(unlocked)

    # Даты разлочки — Steam и так отдаёт unlocktime (unix-время) в каждой
    # записи achievements, просто раньше это поле нигде не использовалось.
    # unlocktime == 0 означает "неизвестно/никогда" даже при achieved == 1
    # (бывает у очень старых записей) — такие в календарь не включаем.
    unlock_dates = []
    for a in unlocked:
        ts = a.get("unlocktime") or 0
        if ts > 0:
            unlock_dates.append(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"))

    rarest_unlocked = None
    unlocked_rarities = []
    if unlocked and global_pct:
        # Steam отдаёт percent СТРОКОЙ (например "45.9"), а не числом — та же
        # проблема, что была в achievement_details.py для модалки. Здесь она
        # не падала сразу (min()/сортировка строк работают), а тихо портила
        # compute_rarest_achievements дальше по цепочке (round() на строке).
        pct_by_name = {}
        for a in global_pct:
            try:
                pct_by_name[a["name"]] = float(a["percent"])
            except (TypeError, ValueError):
                pass
        rarities = [
            (a["apiname"], pct_by_name.get(a["apiname"]))
            for a in unlocked
            if pct_by_name.get(a["apiname"]) is not None
        ]
        if rarities:
            rarest_unlocked = min(rarities, key=lambda x: x[1])
            # Все открытые ачивки этой игры с известным % редкости — не
            # только самая редкая. Нужно для библиотечного топ-10 самых
            # редких (см. compute_rarest_achievements) — без этого списка
            # пришлось бы заново обходить все игры отдельным запросом.
            unlocked_rarities = [
                {"apiname": apiname, "global_percent": percent}
                for apiname, percent in rarities
            ]

    return {
        "name": name,
        "unlocked": unlocked_count,
        "total": total_achievements,
        "percent_complete": round(100 * unlocked_count / total_achievements, 1),
        "rarest_unlocked": (
            {"apiname": rarest_unlocked[0], "global_percent": rarest_unlocked[1]}
            if rarest_unlocked
            else None
        ),
        "unlocked_rarities": unlocked_rarities,
        "unlock_dates": unlock_dates,
    }


def compute_rarest_achievements(achievements_data: dict, games: list, top_n: int = 10) -> list:
    """
    Топ-N самых редких ОТКРЫТЫХ ачивок по всей библиотеке разом (не по одной
    на игру, как rarest_unlocked) — для секции "Общая статистика".

    Имена игр по appid уже есть в fetch_games_list — не считаем их заново.
    Человекочитаемое название самой ачивки (не голый apiname) дотягивается
    ЦЕЛЕНАПРАВЛЕННО только для этих top_n игр (а не для всей библиотеки) —
    отдельным вызовом GetSchemaForGame, результат кэшируется тем же ключом
    schema_ru_{appid}, что использует модалка "все ачивки", так что при
    повторных прогонах/открытии модалки повторного похода в Steam не будет.
    """
    name_by_appid = {g["appid"]: g.get("name", f"appid {g['appid']}") for g in games}

    candidates = _collect_unlocked_rarity_candidates(achievements_data)

    candidates.sort(key=lambda c: c["global_percent"])
    top = candidates[:top_n]

    result = []
    schema_cache = {}
    for c in top:
        appid = c["appid"]
        if appid not in schema_cache:
            schema_cache[appid] = cached_call(
                f"schema_ru_{appid}",
                lambda a=appid: steam_api.get_schema_for_game(a),
                ttl_hours=ACHIEVEMENT_SCHEMA_CACHE_TTL_HOURS,
            )
        display_name = c["apiname"]
        for a in schema_cache[appid]:
            if a.get("name") == c["apiname"]:
                display_name = a.get("displayName", c["apiname"])
                break
        result.append({
            "appid": appid,
            "game": name_by_appid.get(appid, f"appid {appid}"),
            "name": display_name,
            "global_percent": round(c["global_percent"], 1),
        })
    return result


def _collect_unlocked_rarity_candidates(achievements_data: dict) -> list:
    candidates = []
    for appid_str, info in achievements_data.get("games", {}).items():
        for entry in info.get("unlocked_rarities", []):
            candidates.append({
                "appid": int(appid_str),
                "apiname": entry["apiname"],
                "global_percent": entry["global_percent"],
            })
    return candidates


# Тиры "крутости" ачивки по её мировой редкости (global_percent = доля
# игроков, открывших её) — та же идея, что качество предметов в WoW: чем
# меньше людей открыли ачивку, тем она круче. Верхняя граница включительно.
# Полностью настраиваемо — поменяете границы, ничего больше трогать не надо.
RARITY_TIERS = [
    ("gold",   1.0,  "#dcb35c"),   # ≤1% игроков — легендарная редкость
    ("purple", 3.0,  "#b478f0"),   # ≤3% — эпическая
    ("blue",   8.0,  "#4a90d9"),   # ≤8%
    ("green",  20.0, "#4cb389"),   # ≤20%
    ("white",  50.0, "#d7dbe0"),   # ≤50%
    ("gray",   101.0,"#6b7280"),   # >50% — массовая ачивка
]


def _rarity_tier_for(percent: float) -> str:
    for tier_id, ceiling, _color in RARITY_TIERS:
        if percent <= ceiling:
            return tier_id
    return "gray"


def compute_activity_heatmap(achievements_data: dict) -> dict:
    """
    Календарь активности (как GitHub-контрибьюшены) — сколько достижений
    разлочено в каждый день, по всей библиотеке разом. Считается из
    unlocktime, который Steam и так отдаёт в GetPlayerAchievements — без
    отдельных запросов.
    """
    counts: dict[str, int] = {}
    for info in achievements_data.get("games", {}).values():
        for date_str in info.get("unlock_dates", []):
            counts[date_str] = counts.get(date_str, 0) + 1
    return counts


def compute_rarity_tiers(candidates: list) -> dict:
    """
    Распределение ВСЕХ открытых ачивок (с известным % редкости) по тирам
    gray/white/green/blue/gold — насколько круто в целом набита библиотека,
    а не только топ-10 самых редких находок.

    coolness_score: 100 - средний global_percent открытых ачивок. Чем ниже
    средний процент игроков, открывших ваши ачивки, тем выше число (0-100).
    Это не "процент прохождения" — это про то, НАСКОЛЬКО РЕДКИЕ вещи вы
    открываете, а не сколько всего.
    """
    counts = {tier_id: 0 for tier_id, _, _ in RARITY_TIERS}
    total = 0
    percent_sum = 0.0
    for c in candidates:
        pct = c.get("global_percent")
        if pct is None:
            continue
        counts[_rarity_tier_for(pct)] += 1
        percent_sum += pct
        total += 1

    coolness_score = round(100 - (percent_sum / total), 1) if total else 0.0

    return {
        "total_rated": total,
        "counts": counts,
        "coolness_score": coolness_score,
    }


def fetch_achievements_stats(games: list, progress_cb: ProgressCB = None) -> dict:
    raw = _run_parallel(games, _fetch_one_game_achievements, API_CONCURRENCY, "achievements", progress_cb)
    per_game = {str(appid): info for appid, info in raw.items() if info is not None}

    result = {"fetched_at": _now_iso(), "games": per_game}
    result["rarest_achievements"] = compute_rarest_achievements(result, games)
    result["rarity_tiers"] = compute_rarity_tiers(_collect_unlocked_rarity_candidates(result))
    result["activity_heatmap"] = compute_activity_heatmap(result)
    atomic_write_json(ACHIEVEMENTS_STATS_FILE, result)
    return result


# ---------- Этап 3: картинки ----------

# Приоритет, из какого поля брать картинку для реального скачивания —
# должен совпадать с приоритетом на фронтенде (см. imgCandidates в
# index.html): header_image (460x215, ровно пропорции плитки, без обрезки)
# приоритетнее capsule_imagev5 (616x353, крупнее, но обрежется по бокам),
# который в свою очередь приоритетнее старого мелкого capsule_image.
def _best_image_url(info: dict) -> str | None:
    return info.get("header_image") or info.get("capsule_imagev5") or info.get("capsule_image")


def _download_one_game_image(args: tuple) -> dict:
    """
    Скачивает картинку на диск (GAME_IMAGES_DIR/{appid}.jpg) и возвращает
    итоговую запись для images.json. Если ссылка не изменилась с прошлого
    раза И файл уже лежит на диске — повторно НЕ качает, просто возвращает
    существующую запись как есть (иначе "Обновить всё" перекачивало бы
    сотни файлов заново, даже если у игр ничего не поменялось).
    """
    appid, info, existing_entry = args
    url = _best_image_url(info)
    entry = {
        "header_image": info.get("header_image"),
        "capsule_image": info.get("capsule_image"),
        "capsule_imagev5": info.get("capsule_imagev5"),
        "local_image": existing_entry.get("local_image"),
        "source_url": existing_entry.get("source_url"),
    }

    if not url:
        entry["local_image"] = None
        entry["source_url"] = None
        return entry

    # Расширение — из самого URL картинки (Steam CDN отдаёт и .jpg, и
    # .png/.webp для части игр), а не всегда ".jpg" — иначе PNG/WEBP
    # сохранялся бы под неверным расширением и мог не открыться как надо.
    ext = url.split("?", 1)[0].rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"  # неизвестный/нестандартный URL — безопасный дефолт, как раньше
    local_path = GAME_IMAGES_DIR / f"{appid}.{ext}"

    if existing_entry.get("source_url") == url and local_path.exists():
        return entry  # без изменений — файл уже на диске, перекачивать незачем

    data = steam_api.download_image_bytes(url)
    if data is None:
        # Не удалось скачать (сеть/CDN недоступен) — оставляем старый локальный
        # файл, если он был, иначе фронт откатится на прямую ссылку на Steam.
        return entry

    # Если расширение сменилось с прошлого раза (например, был .jpg, стал
    # .png) — подчищаем старый файл, чтобы на диске не копились сироты.
    old_local = existing_entry.get("local_image")
    if old_local and old_local != f"/static/game_images/{appid}.{ext}":
        old_path = GAME_IMAGES_DIR / Path(old_local).name
        old_path.unlink(missing_ok=True)

    try:
        local_path.write_bytes(data)
        entry["local_image"] = f"/static/game_images/{appid}.{ext}"
        entry["source_url"] = url
    except OSError as e:
        logger.warning(f"Не удалось сохранить картинку appid={appid} на диск: {e}")

    return entry


def fetch_game_images(games: list, progress_cb: ProgressCB = None) -> dict:
    """
    РЕАЛЬНО скачивает картинки на диск сервера (GAME_IMAGES_DIR), а не
    только запоминает ссылку — раньше отчёт хранил лишь URL от Steam, и
    браузер каждый раз заново тянул файл с CDN Steam при каждом открытии
    страницы. Теперь картинка сохраняется локально и отдаётся через /static
    (см. web.py) — при повторных заходах грузится с вашего сервера, а не
    с Steam.

    Устройство: сначала как раньше получаем сам URL картинки через
    store.steampowered.com/api/appdetails (это дёргает тот же
    _fetch_one_game_price, что и подсчёт цены — см. там, кэш общий на
    неделю). Затем СКАЧИВАЕМ файл по этому URL — это уже не витрина Steam
    с её жёстким rate-limit, а обычный статический CDN
    (cdn.cloudflare.steamstatic.com/cdn.akamai.steamstatic.com), поэтому
    качаем с большим параллелизмом и без общего троттлинга (см.
    steam_api.download_image_bytes).

    Повторное скачивание пропускается, если URL картинки не изменился и
    файл уже лежит на диске — see _download_one_game_image.

    Результат СЛИВАЕТСЯ с уже сохранённым файлом, а не затирает его целиком:
    "Обновить" (быстрое обновление) вызывает эту функцию только для НОВЫХ
    игр — если просто перезаписать файл, картинки всех остальных игр
    пропали бы из отчёта до следующего полного обновления.
    """
    existing = load_images().get("games", {})
    raw = _run_parallel(games, _fetch_one_game_price, STORE_CONCURRENCY, "images", progress_cb)

    download_args = [
        (game["appid"], raw.get(game["appid"], {}), existing.get(str(game["appid"]), {}))
        for game in games
    ]
    # Скачивание файлов — отдельный проход с бОльшим параллелизмом, чем у
    # витрины Steam выше: это статический CDN, не store API, ему не нужен
    # тот же щадящий throttle (см. steam_api.download_image_bytes).
    downloaded = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_download_one_game_image, args): args[0] for args in download_args}
        for fut in as_completed(futures):
            appid = futures[fut]
            downloaded[appid] = fut.result()

    per_game = dict(existing)
    for game in games:
        per_game[str(game["appid"])] = downloaded[game["appid"]]

    found = sum(1 for v in per_game.values() if v.get("local_image") or v.get("header_image") or v.get("capsule_image"))
    result = {"fetched_at": _now_iso(), "games_with_images": found, "games": per_game}
    atomic_write_json(IMAGES_FILE, result)
    logger.info(f"Картинки: {found} из {len(per_game)} игр в библиотеке (обновлено сейчас: {len(games)}).")
    return result


# ---------- Этап 5: стоимость библиотеки ----------

def _fetch_one_game_price(game: dict) -> dict:
    appid = game["appid"]
    cache_key = f"price_{appid}"
    price_info = cache_get(cache_key, ttl_hours=24 * 7)
    if price_info is None:
        price_info = steam_api.get_app_price(appid)
        # Временные ошибки (429/таймаут после ретраев) НЕ кэшируем — иначе
        # они застрянут как "цена не найдена" на неделю. Кэшируем только
        # окончательные результаты (нашли цену или Steam явно сказал "нет цены").
        if not price_info.get("transient_error"):
            cache_set(cache_key, price_info)
    return price_info


def fetch_library_cost(games: list, progress_cb: ProgressCB = None) -> dict:
    raw = _run_parallel(games, _fetch_one_game_price, STORE_CONCURRENCY, "library_cost", progress_cb)

    per_game = {}
    total_cost = 0.0
    not_found = 0
    rate_limited = 0

    for game in games:
        appid = game["appid"]
        name = game.get("name", f"appid {appid}")
        price_info = raw.get(appid, {})

        if price_info.get("price_found"):
            price = price_info.get("initial_price_usd", 0.0)
            per_game[str(appid)] = {
                "name": name,
                "price_usd": price,
                "header_image": price_info.get("header_image"),
                "capsule_image": price_info.get("capsule_image"),
                "capsule_imagev5": price_info.get("capsule_imagev5"),
            }
            total_cost += price
        elif price_info.get("transient_error"):
            # Настоящая "не знаем пока" — сеть/429/5xx после всех попыток.
            # НЕ считаем как $0: следующее обновление должно повторить запрос
            # (см. _fetch_one_game_price — такие результаты не кэшируются).
            not_found += 1
            rate_limited += 1
            per_game[str(appid)] = {
                "name": name,
                "price_usd": None,
                "header_image": price_info.get("header_image"),
                "capsule_image": price_info.get("capsule_image"),
                "capsule_imagev5": price_info.get("capsule_imagev5"),
            }
        else:
            # Steam окончательно ответил "цены нет" (снята с продажи везде,
            # DLC/саундтрек без отдельной цены и т.п.) — это не "неизвестно",
            # а фактический ноль. Считаем как $0 и учитываем как "найдено",
            # а не выкидываем из статистики навсегда.
            per_game[str(appid)] = {
                "name": name,
                "price_usd": 0.0,
                "header_image": price_info.get("header_image"),
                "capsule_image": price_info.get("capsule_image"),
                "capsule_imagev5": price_info.get("capsule_imagev5"),
            }

    result = {
        "fetched_at": _now_iso(),
        "total_cost_usd": round(total_cost, 2),
        "games_priced": len(games) - not_found,
        "games_price_not_found": not_found,
        "games_price_rate_limited": rate_limited,
        "games": per_game,
    }
    atomic_write_json(LIBRARY_COST_FILE, result)
    logger.info(
        f"Общая стоимость библиотеки без скидок: ${result['total_cost_usd']} "
        f"(ожидают повтора из-за rate-limit: {not_found})"
    )
    return result


# ---------- Этап 5.5: отзывы (оценка "Очень положительные" и т.п.) ----------

def _fetch_one_game_review(game: dict) -> dict:
    appid = game["appid"]
    cache_key = f"review_{appid}"
    review_info = cache_get(cache_key, ttl_hours=24 * 7)
    if review_info is None:
        review_info = steam_api.get_app_reviews(appid)
        if not review_info.get("transient_error"):
            cache_set(cache_key, review_info)
    return review_info


def fetch_reviews(games: list, progress_cb: ProgressCB = None) -> dict:
    """
    Оценка отзывов Steam по каждой игре. Кэшируется так же долго и по той же
    логике, что и цены (неделя, транзиентные ошибки не кэшируются) — это тот
    же store.steampowered.com, с той же строгой rate-limit-политикой.
    """
    raw = _run_parallel(games, _fetch_one_game_review, STORE_CONCURRENCY, "reviews", progress_cb)

    per_game = {}
    found = 0
    total = len(games)

    for game in games:
        appid = game["appid"]
        name = game.get("name", f"appid {appid}")
        review_info = raw.get(appid, {})

        if review_info.get("reviews_found"):
            found += 1
            per_game[str(appid)] = {
                "name": name,
                "review_score": review_info.get("review_score", 0),
                "review_desc": review_info.get("review_desc", ""),
                "total_reviews": review_info.get("total_reviews", 0),
                "positive_percent": review_info.get("positive_percent", 0),
            }
        else:
            per_game[str(appid)] = None

    result = {"fetched_at": _now_iso(), "games_with_reviews": found, "games": per_game}
    atomic_write_json(REVIEWS_FILE, result)
    logger.info(f"Отзывы найдены для {found} из {total} игр.")
    return result


# ---------- Загрузка кэша этапов, которые в этом прогоне не обновлялись ----------
#
# Кнопки "Обновить" (список игр + достижения) и "Обновить цены и отзывы"
# (стоимость + отзывы) в дашборде независимы — каждая прогоняет только свою
# половину пайплайна. Но generate_report собирает ОДИН отчёт сразу из всех
# четырёх источников, поэтому для той половины, которая в этот раз не
# обновлялась, берём последний посчитанный результат с диска, а не гоняем
# API заново. Если файла ещё нет (самый первый запуск), возвращаем пустую,
# но корректную по форме структуру — generate_report просто покажет "нет
# данных" по этим полям вместо падения с KeyError.


def _load_json_or_default(path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Не удалось прочитать {path}: {e}, использую пустые данные.")
        return default


def load_games_list() -> Optional[dict]:
    """None, если списка игр ещё никогда не было — вызывающий код должен
    попросить сначала нажать «Обновить» (обновление цен без списка игр
    бессмысленно: непонятно, для каких appid их считать)."""
    if not GAMES_LIST_FILE.exists():
        return None
    return _load_json_or_default(GAMES_LIST_FILE, None)


def load_achievements_stats() -> dict:
    return _load_json_or_default(
        ACHIEVEMENTS_STATS_FILE,
        {
            "fetched_at": None,
            "games": {},
            "rarest_achievements": [],
            "rarity_tiers": {"total_rated": 0, "counts": {}, "coolness_score": 0.0},
            "activity_heatmap": {},
        },
    )


def load_library_cost() -> dict:
    return _load_json_or_default(
        LIBRARY_COST_FILE,
        {
            "fetched_at": None,
            "total_cost_usd": 0.0,
            "games_priced": 0,
            "games_price_not_found": 0,
            "games_price_rate_limited": 0,
            "games": {},
        },
    )


def load_reviews() -> dict:
    return _load_json_or_default(REVIEWS_FILE, {"fetched_at": None, "games_with_reviews": 0, "games": {}})


def load_images() -> dict:
    return _load_json_or_default(IMAGES_FILE, {"fetched_at": None, "games_with_images": 0, "games": {}})


# ---------- Этап 6: сборка единого отчёта ----------

def generate_report(
    games_data: dict,
    achievements_data: dict,
    cost_data: dict,
    reviews_data: dict = None,
    images_data: dict = None,
) -> dict:
    achievements_by_appid = achievements_data.get("games", {})
    reviews_by_appid = (reviews_data or {}).get("games", {})
    cost_by_appid = cost_data.get("games", {})
    images_by_appid = (images_data or {}).get("games", {})

    # Плитка всех игр: время + процент выполненных достижений (если они есть у игры)
    games_grid = []
    for g in games_data["games"]:
        appid = g["appid"]
        info = achievements_by_appid.get(str(appid))
        review = reviews_by_appid.get(str(appid))
        cost_info = cost_by_appid.get(str(appid))
        image_info = images_by_appid.get(str(appid))
        games_grid.append(
            {
                "appid": appid,
                "name": g.get("name", f"appid {appid}"),
                "hours": round(g.get("playtime_forever", 0) / 60, 1),
                "achievements_percent": info["percent_complete"] if info else None,
                "achievements_unlocked": info["unlocked"] if info else None,
                "achievements_total": info["total"] if info else None,
                # Картинки: local_image — файл, реально СКАЧАННЫЙ на диск
                # сервера и отдаваемый через /static (см. fetch_game_images) —
                # приоритетнее всего на фронте: грузится с вашего сервера, а
                # не с CDN Steam при каждом открытии страницы. Остальные поля —
                # прямые ссылки на Steam (сначала из отдельного этапа "картинки",
                # при отсутствии — из этапа цены, тот же appdetails) — резерв
                # на случай, если локальный файл почему-то не скачался. Если и
                # тут пусто — фронт сам перебирает CDN-шаблоны путей как
                # последний fallback (см. index.html).
                "local_image": (image_info or {}).get("local_image"),
                "header_image": (image_info or {}).get("header_image") or (cost_info or {}).get("header_image"),
                "capsule_image": (image_info or {}).get("capsule_image") or (cost_info or {}).get("capsule_image"),
                "capsule_imagev5": (image_info or {}).get("capsule_imagev5") or (cost_info or {}).get("capsule_imagev5"),
                "review_desc": review["review_desc"] if review else None,
                "review_score": review["review_score"] if review else None,
                "review_positive_percent": review["positive_percent"] if review else None,
                "review_total": review["total_reviews"] if review else None,
            }
        )

    # Сортировка: сначала игры С достижениями (по алфавиту), потом БЕЗ
    # достижений (по алфавиту). Игра в библиотеке — это просто игра, вне
    # зависимости от того, через какой источник её нашёл fetch_games_list
    # (см. его докстринг) — разделения на "основные"/"ожидающие" больше нет.
    with_ach = sorted(
        [g for g in games_grid if g["achievements_percent"] is not None],
        key=lambda g: g["name"].lower(),
    )
    without_ach = sorted(
        [g for g in games_grid if g["achievements_percent"] is None],
        key=lambda g: g["name"].lower(),
    )
    games_grid = with_ach + without_ach

    # Общая статистика "насколько я хорош" — по всей библиотеке разом
    total_ach_unlocked = sum(info["unlocked"] for info in achievements_by_appid.values())
    total_ach_available = sum(info["total"] for info in achievements_by_appid.values())
    overall_percent = (
        round(100 * total_ach_unlocked / total_ach_available, 1) if total_ach_available else 0.0
    )
    games_completed_100 = sum(
        1 for info in achievements_by_appid.values() if info["percent_complete"] == 100.0
    )

    report = {
        "generated_at": _now_iso(),
        "summary": {
            "games_count": games_data["games_count"],
            "total_hours": games_data["total_hours"],
            "library_cost_usd": cost_data["total_cost_usd"],
            "games_priced": cost_data["games_priced"],
            "games_price_not_found": cost_data["games_price_not_found"],
            "games_price_rate_limited": cost_data.get("games_price_rate_limited", 0),
            "achievements_unlocked_total": total_ach_unlocked,
            "achievements_available_total": total_ach_available,
            "achievements_overall_percent": overall_percent,
            "games_completed_100": games_completed_100,
            "games_with_achievements": len(with_ach),
            "games_without_achievements": len(without_ach),
        },
        "games_grid": games_grid,
        "rarest_achievements": achievements_data.get("rarest_achievements", []),
        "rarity_tiers": achievements_data.get("rarity_tiers", {"total_rated": 0, "counts": {}, "coolness_score": 0.0}),
        "activity_heatmap": achievements_data.get("activity_heatmap", {}),
    }

    atomic_write_json(REPORT_JSON_FILE, report)

    _write_markdown_report(report)
    return report


def _write_markdown_report(report: dict) -> None:
    s = report["summary"]
    lines = [
        "# Steam Library Report",
        "",
        f"_Сгенерировано: {report['generated_at']}_",
        "",
        "## Сводка",
        "",
        f"- Игр в библиотеке: **{s['games_count']}**",
        f"- Суммарное время: **{s['total_hours']} часов**",
        f"- Стоимость библиотеки без скидок: **${s['library_cost_usd']}**",
        f"  (цена найдена для {s['games_priced']} игр, не найдена для {s['games_price_not_found']})",
        "",
        "## Прогресс по достижениям (вся библиотека)",
        "",
        f"- Открыто **{s['achievements_unlocked_total']}** из **{s['achievements_available_total']}** "
        f"достижений (**{s['achievements_overall_percent']}%**)",
        f"- Игр пройдено на 100%: **{s['games_completed_100']}**",
        f"- Игр с достижениями: {s['games_with_achievements']}, без достижений: {s['games_without_achievements']}",
        "",
        "## Достижения по играм",
        "",
    ]
    for g in report["games_grid"]:
        if g["achievements_percent"] is not None:
            lines.append(
                f"- {g['name']}: {g['achievements_unlocked']}/{g['achievements_total']} "
                f"({g['achievements_percent']}%)"
            )
        else:
            lines.append(f"- {g['name']}: нет достижений")

    with open(REPORT_MD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
