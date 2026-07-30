import threading
import time
import xml.etree.ElementTree as ET

import requests

from .config import (
    STEAM_API_KEY,
    STEAM_ID,
    REQUEST_DELAY,
    STORE_REQUEST_DELAY,
    API_CONCURRENCY,
    STORE_CONCURRENCY,
    PRICE_FALLBACK_COUNTRIES,
)
from .logger_setup import get_logger

logger = get_logger()


class RateLimiter:
    """
    Потокобезопасный ограничитель: до `max_concurrency` запросов "в полёте"
    одновременно, но моменты СТАРТА запросов разнесены не менее чем на
    `min_interval` секунд — то есть общий темп отправки запросов остаётся
    прежним (не бьём по rate-limit'у Steam), а ускорение получаем за счёт
    перекрытия сетевого ожидания (RTT) между потоками, а не самого лимита.

    Пример: раньше 400 запросов по 0.5с паузы = минимум 200с только на
    паузы, ПЛЮС RTT каждого запроса добавлялся последовательно. Теперь RTT
    нескольких запросов перекрывается, пока действует та же пауза между
    стартами — прогон быстрее в разы без увеличения нагрузки на Steam.
    """

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


_api_limiter = RateLimiter(REQUEST_DELAY, API_CONCURRENCY)
_store_limiter = RateLimiter(STORE_REQUEST_DELAY, STORE_CONCURRENCY)


def _get(url: str, params: dict, timeout: int = 15) -> dict:
    with _api_limiter:
        resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get_store(url: str, params: dict, timeout: int = 15, max_retries: int = 4) -> dict:
    """
    Запрос к store.steampowered.com с собственным rate-limiter'ом и
    retry-с-backoff на 429. Если Steam прислал заголовок Retry-After —
    уважаем его, иначе ждём по экспоненте (2с, 4с, 8с, 16с).
    """
    for attempt in range(max_retries):
        with _store_limiter:
            resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else (2 ** (attempt + 1))
            logger.warning(
                f"429 от store.steampowered.com (попытка {attempt + 1}/{max_retries}), "
                f"жду {wait:.0f}с..."
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    # Все попытки исчерпаны — пусть вызывающий код решает, что делать
    resp.raise_for_status()
    return {}


def get_owned_games() -> list:
    """IPlayerService/GetOwnedGames — список игр пользователя с playtime."""
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": STEAM_ID,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    }
    data = _get(url, params)
    return data.get("response", {}).get("games", [])


def get_recently_played_games() -> list:
    """
    IPlayerService/GetRecentlyPlayedGames — игры, в которые играли за последние
    2 недели, с уже посчитанным playtime_forever.

    Резервный источник для fetch_games_list: использует отдельный, более
    быстрый путь в бэкенде Steam (тот же, что питает "Недавно сыгранные" в
    клиенте), и поэтому иногда видит совсем свежую игровую сессию раньше, чем
    GetOwnedGames успевает её отреплицировать.

    count=0 означает "без ограничения" (вернуть все игры за 2 недели, а не
    только топ по времени). Если Steam ответил ошибкой/недоступен — тихо
    возвращаем пустой список, это не критично: это резервный, а не основной
    источник.

    ВАЖНО: это официальный Web API-эндпоинт (как и GetOwnedGames), требует
    STEAM_API_KEY. Мы намеренно НЕ используем скрейпинг HTML/XML-страниц
    community-профиля и не используем cookie авторизованной сессии — поэтому
    игра, которую Steam не отдаёт НИ ОДНИМ из этих двух официальных
    эндпоинтов (подтверждённый на практике редкий случай — appid виден
    только залогиненному владельцу через веб-профиль), в дашборде просто не
    появится. Это осознанное ограничение: полагаемся только на официальный API.
    """
    url = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
    params = {"key": STEAM_API_KEY, "steamid": STEAM_ID, "count": 0, "format": "json"}
    try:
        data = _get(url, params)
        return data.get("response", {}).get("games", [])
    except requests.exceptions.RequestException as e:
        logger.warning(f"Резервный источник (недавняя активность) недоступен: {e}")
        return []


def resolve_steamid64(raw: str) -> str:
    """
    STEAM_ID в .env может быть чем угодно: готовым SteamID64 (17 цифр),
    просто ником (vanity URL) или целой ссылкой на профиль
    (steamcommunity.com/id/<ник>/ или .../profiles/7656.../) — приводим
    к числовому SteamID64, который единственный принимают официальные
    Web API эндпоинты (GetOwnedGames и т.п.) и XML-фид библиотеки ниже.
    """
    raw = raw.strip()

    if raw.isdigit() and len(raw) == 17:
        return raw

    vanity = raw
    for marker in ("/profiles/", "/id/"):
        if marker in raw:
            tail = raw.split(marker, 1)[1]
            vanity = tail.strip("/").split("/")[0]
            break

    if vanity.isdigit() and len(vanity) == 17:
        return vanity

    logger.info(f"STEAM_ID='{raw}' не похоже на готовый SteamID64 — резолвлю vanity-имя «{vanity}»...")
    url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    data = _get(url, {"key": STEAM_API_KEY, "vanityurl": vanity, "format": "json"})
    response = data.get("response", {})
    if response.get("success") != 1:
        raise RuntimeError(
            f"Steam не смог найти профиль по имени «{vanity}» "
            f"(success={response.get('success')}, message={response.get('message')})."
        )
    return response["steamid"]


def get_full_library(steamid64: str) -> list:
    """
    Полный список игр аккаунта через публичный XML-фид страницы "Игры"
    профиля — тот же самый список, что виден на человекочитаемой странице
    steamcommunity.com/id/<ник>/games/?tab=all, просто в виде XML и по
    числовому SteamID64.

    Это НАМЕРЕННОЕ отступление от политики "только официальный Web API"
    (см. комментарий в get_recently_played_games): GetOwnedGames в принципе
    НИКОГДА не отдаёт F2P-игры, ни разу не запущенные через клиент — а этот
    публичный XML-фид отдаёт. Он не требует авторизации/cookie (обычный
    публичный документ, доступный кому угодно), единственное условие —
    "Сведения об играх" должны быть публичными в настройках приватности
    профиля.

    Возвращает [] (и пишет в лог причину), если профиль/список игр приватны
    или недоступны — это резервный, не основной источник, поэтому тихая
    деградация лучше, чем падение всего обновления библиотеки.
    """
    url = f"https://steamcommunity.com/profiles/{steamid64}/games?tab=all&xml=1"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.text
    except requests.exceptions.RequestException as e:
        logger.warning(f"Не удалось получить полный список библиотеки (XML-фид профиля): {e}")
        return []

    if "<error>" in text:
        error_msg = text.split("<error>", 1)[1].split("</error>", 1)[0]
        logger.warning(
            f"XML-фид библиотеки ответил ошибкой: {error_msg}. Обычно значит, что "
            f"'Сведения об играх' скрыты в настройках приватности профиля."
        )
        return []

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        logger.warning(f"Не удалось разобрать XML-фид библиотеки: {e}")
        return []

    games_node = root.find("games")
    if games_node is None:
        return []

    result = []
    for game in games_node.findall("game"):
        appid_el = game.find("appID")
        name_el = game.find("name")
        if appid_el is None or appid_el.text is None:
            continue
        try:
            appid = int(appid_el.text)
        except ValueError:
            continue
        result.append({"appid": appid, "name": (name_el.text or f"appid {appid}").strip()})
    return result


def get_global_achievement_percentages(appid: int) -> list:
    """ISteamUserStats/GetGlobalAchievementPercentagesForApp — % игроков на ачивку."""
    url = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/"
    params = {"gameid": appid, "format": "json"}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            data = _get(url, params)
            return data.get("achievementpercentages", {}).get("achievements", [])
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            # После ретраев — либо реальное "нет глобальной статистики", либо
            # стойкая ошибка; это поле сейчас используется только для
            # необязательной "редкости" и не влияет на подсчёт % — пусть
            # молча возвращает пусто, не тратя лишний запрос впустую.
            return []
    return []


def get_schema_for_game(appid: int) -> list:
    """
    ISteamUserStats/GetSchemaForGame — статичное описание ачивок игры:
    apiname, displayName (человекочитаемое название), description, иконки
    (цветная — уже открыта, серая — ещё нет).

    l=russian — просим у Steam сразу русскую локализацию названий/описаний,
    если у игры есть перевод (у подавляющего большинства современных игр —
    есть). Если перевода нет, Steam тихо отдаёт английский — это нормально,
    не ошибка.

    До этой функции проект нигде не запрашивал названия/описания ачивок —
    только числа (% выполнено, редкость самой редкой). Нужна отдельно для
    модалки "показать все ачивки", где важно видеть, ЧТО это за достижение,
    а не только голый apiname.

    Некоторые игры (особенно инди без официальной Steam-схемы достижений)
    не имеют statистики вовсе — тогда availableGameStats может отсутствовать
    или прийти пустым; возвращаем [] и вызывающий код обязан считать это
    "данные недоступны", а не ошибкой.
    """
    url = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
    params = {"key": STEAM_API_KEY, "appid": appid, "l": "russian", "format": "json"}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            data = _get(url, params)
            return data.get("game", {}).get("availableGameStats", {}).get("achievements", [])
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            logger.warning(f"Не удалось получить схему ачивок для appid={appid}: {e}")
            return []
        except requests.exceptions.RequestException as e:
            logger.warning(f"Сетевая ошибка при получении схемы ачивок appid={appid}: {e}")
            return []
    return []


def get_player_achievements(appid: int) -> dict:
    """
    ISteamUserStats/GetPlayerAchievements — какие ачивки открыты у вас.

    Различаем два результата (как и в get_app_price):
      - transient_error=False, achievements=[] — Steam ответил явно "нет
        статистики для этой игры" (success=false). Это окончательно, можно
        кэшировать.
      - transient_error=True — сетевая ошибка/429/5xx после ретраев. Это
        НЕЛЬЗЯ кэшировать как "нет достижений" — иначе игра с реальными
        100% может навсегда потеряться из подсчёта до истечения TTL.
    """
    url = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
    params = {"key": STEAM_API_KEY, "steamid": STEAM_ID, "appid": appid, "format": "json"}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            data = _get(url, params)
            response = data.get("playerstats", {})
            if not response.get("success"):
                # Игра без статистики ачивок либо профиль/игра приватные — окончательно
                return {"achievements": [], "transient_error": False}
            response["transient_error"] = False
            return response
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    f"Ошибка {status} при получении достижений appid={appid} "
                    f"(попытка {attempt + 1}/{max_retries}), жду {wait}с..."
                )
                time.sleep(wait)
                continue
            logger.warning(f"Не удалось получить достижения для appid={appid} (после ретраев): {e}")
            return {"achievements": [], "transient_error": True}
        except requests.exceptions.RequestException as e:
            logger.warning(f"Сетевая ошибка при получении достижений appid={appid}: {e}")
            return {"achievements": [], "transient_error": True}
    return {"achievements": [], "transient_error": True}


def download_image_bytes(url: str) -> bytes | None:
    """
    Скачивает саму картинку по её URL (cdn.cloudflare.steamstatic.com /
    cdn.akamai.steamstatic.com) — это обычный статический CDN для файлов
    магазина, НЕ store.steampowered.com/api/..., у которого строгий
    rate-limit (~200 запросов/5 мин, см. _get_store). Раздача картинок
    устроена как у любого CDN — рассчитана на огромный поток запросов,
    поэтому качаем без общего throttle'а _store_limiter и с большим
    параллелизмом (см. stats.fetch_game_images).
    """
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.content
    except requests.exceptions.RequestException as e:
        logger.warning(f"Не удалось скачать картинку {url}: {e}")
        return None


def get_app_price(appid: int) -> dict:
    """
    Цена игры через store.steampowered.com/api/appdetails.
    Это официальный JSON-эндпоинт витрины Steam (не HTML-скрейпинг),
    отдаёт price_overview с initial (цена без скидки) в минимальных единицах валюты.

    Считаем СТРОГО в USD — appdetails всегда отдаёт цену в валюте того
    региона (cc), который указан в запросе, а не в долларах. Раньше тут
    был перебор нескольких регионов (PRICE_FALLBACK_COUNTRIES) для игр, не
    продающихся в США — но чужая валюта (EUR/GBP/RUB/KZT) без конвертации
    один-в-один мешалась с долларами и раздувала сумму библиотеки в разы.
    Если price_overview найден, но не в USD — пробуем следующий регион из
    PRICE_FALLBACK_COUNTRIES (сейчас там только "us", так что практически
    это просто означает "цена не найдена"); в сумму библиотеки идут только
    настоящие долларовые цены.

    Заодно забираем header_image/capsule_image прямо из ответа Steam — это
    настоящий URL картинки данной игры на CDN, а не догадка по шаблону пути
    (см. проблему с не загружающимися обложками на фронтенде).

    Различаем два разных результата:
      - price_found=False, transient_error=False — Steam ответил (во всех
        проверенных регионах), но у игры реально нет цены (снята с продажи
        везде, DLC-бандл без своей цены и т.п.) или цена есть, но не в USD.
        Это можно кэшировать.
      - price_found=False, transient_error=True — сетевая ошибка/429/5xx после
        всех попыток хотя бы в одном регионе. Это НЕЛЬЗЯ кэшировать надолго —
        нужно повторить в следующий раз, а не запоминать как окончательный
        "цены нет".
    """
    url = "https://store.steampowered.com/api/appdetails"
    image_urls = {}

    for cc in PRICE_FALLBACK_COUNTRIES:
        params = {"appids": appid, "cc": cc, "l": "en"}
        try:
            data = _get_store(url, params)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Не удалось получить цену для appid={appid}, регион {cc}: {e}")
            # Пробуем следующий регион, а не сдаёмся сразу — но если это был
            # последний регион, вернём transient_error, чтобы не закэшировать
            # как "цены нет навсегда".
            if cc == PRICE_FALLBACK_COUNTRIES[-1]:
                return {"price_found": False, "transient_error": True, **image_urls}
            continue

        entry = data.get(str(appid))
        if not entry or not entry.get("success"):
            continue

        app_data = entry.get("data", {})
        if not image_urls:
            if app_data.get("header_image"):
                image_urls["header_image"] = app_data["header_image"]
            if app_data.get("capsule_image"):
                image_urls["capsule_image"] = app_data["capsule_image"]
            # capsule_imagev5 — заметно более крупная картинка (~616x353),
            # появилась вместе с редизайном витрины Steam ~2022 года. Обычный
            # capsule_image из appdetails остаётся крошечным (231x87) и на
            # плитке любого разумного размера растягивается в мыло — v5
            # приоритетнее везде, где Steam её отдаёт (почти все игры моложе
            # ~2022 года и многие старые, которым Valve пересчитал арт).
            if app_data.get("capsule_imagev5"):
                image_urls["capsule_imagev5"] = app_data["capsule_imagev5"]

        if app_data.get("is_free"):
            return {
                "price_found": True,
                "transient_error": False,
                "is_free": True,
                "initial_price_usd": 0.0,
                "price_region": cc,
                **image_urls,
            }
        price_overview = app_data.get("price_overview")
        if price_overview:
            currency = price_overview.get("currency", "USD")
            if currency != "USD":
                # Пользователю нужны ТОЛЬКО настоящие доллары, без прикидочной
                # конвертации курса (тот баг уже был и раздувал сумму в разы —
                # см. историю). appdetails всегда отдаёт цену в валюте региона
                # запроса (cc), а не в USD — для cc=eu/gb/ru/kz это EUR/GBP/
                # RUB/KZT, никогда не доллары. Пробуем следующий регион из
                # PRICE_FALLBACK_COUNTRIES — если ни один не даст цену именно
                # в USD, игра попадёт в "цена не найдена" (честно), а не в
                # сумму библиотеки по угаданному курсу.
                continue
            return {
                "price_found": True,
                "transient_error": False,
                "is_free": False,
                "initial_price_usd": round(price_overview.get("initial", 0) / 100, 2),
                "price_region": cc,
                **image_urls,
            }
        # success=True, но нет price_overview в этом регионе — пробуем следующий

    # Ни один регион не дал цену — это окончательно (снята с продажи везде,
    # либо это DLC/саундтрек без отдельной цены), но картинку могли забрать
    # по пути даже без найденной цены.
    return {"price_found": False, "transient_error": False, **image_urls}


# Официальная шкала Steam (0-9) — стабильная, задокументированная, Valve её
# не меняет. Переводим сами, а не полагаемся на текст review_score_desc от
# Steam — он всегда приходит по-английски независимо от параметра language
# (тот фильтрует, ОТЗЫВЫ на каком языке учитывать в сводке, а не язык самого
# текстового описания оценки).
REVIEW_SCORE_LABELS_RU = {
    9: "Крайне положительные",
    8: "Очень положительные",
    7: "Положительные",
    6: "Скорее положительные",
    5: "Смешанные",
    4: "Скорее отрицательные",
    3: "Отрицательные",
    2: "Очень отрицательные",
    1: "Крайне отрицательные",
    0: "Недостаточно отзывов",
}


def get_app_reviews(appid: int) -> dict:
    """
    Оценка отзывов через store.steampowered.com/appreviews/{appid} — тот же
    официальный витринный эндпоинт, что и цены (не скрейпинг), поэтому
    используем ту же throttle/retry-инфраструктуру (_get_store).

    num_per_page=0 — просим только сводку (review_score_desc, проценты,
    количество), без текста самих отзывов, которые нам не нужны.

    Различаем, как и с ценой:
      - reviews_found=False, transient_error=False — у игры реально нет
        отзывов (например, недавно вышла, или appid снят с продажи).
      - reviews_found=False, transient_error=True — временная ошибка,
        НЕ кэшировать как "отзывов нет".
    """
    url = f"https://store.steampowered.com/appreviews/{appid}"
    params = {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0}
    try:
        data = _get_store(url, params)
        if not data.get("success"):
            return {"reviews_found": False, "transient_error": False}
        summary = data.get("query_summary", {})
        total = summary.get("total_reviews", 0)
        if not total:
            return {"reviews_found": False, "transient_error": False}
        positive = summary.get("total_positive", 0)
        score = summary.get("review_score", 0)
        return {
            "reviews_found": True,
            "transient_error": False,
            "review_score": score,  # 0-9, чем больше — тем позитивнее
            "review_desc": REVIEW_SCORE_LABELS_RU.get(score, summary.get("review_score_desc", "")),
            "total_reviews": total,
            "positive_percent": round(100 * positive / total, 1),
        }
    except requests.exceptions.RequestException as e:
        logger.warning(f"Не удалось получить отзывы для appid={appid} (после ретраев): {e}")
        return {"reviews_found": False, "transient_error": True}