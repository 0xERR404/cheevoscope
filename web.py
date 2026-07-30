import secrets

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import DASHBOARD_LOGIN, DASHBOARD_PASSWORD, REPORT_JSON_FILE, RETRO_REPORT_JSON_FILE, validate_config, BASE_DIR
from app.pipeline import start_refresh, read_status
from app import retro_pipeline
from app import achievement_details
from app.logger_setup import get_logger

import json
import traceback

logger = get_logger()

app = FastAPI(title="CheevoScope")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    """
    Необязательная защита на уровне приложения (см. config.DASHBOARD_LOGIN /
    DASHBOARD_PASSWORD). Актуальна прежде всего для тех, кто НЕ настраивал
    домен/Caddy через install.sh — в этом случае сервис слушает
    0.0.0.0:8000 без какой-либо авторизации, и Caddy-basicauth (см.
    install.sh, шаг 17) просто не в игре.

    Если логин/пароль не заданы в .env — проверка отключена целиком
    (поведение как раньше, ничего не ломаем для тех, кто и так закрыл
    доступ через Caddy или локальный firewall).

    secrets.compare_digest — защита от timing-атак при сравнении пароля.
    """
    if not DASHBOARD_LOGIN or not DASHBOARD_PASSWORD:
        return
    valid = bool(credentials) and secrets.compare_digest(
        credentials.username, DASHBOARD_LOGIN
    ) and secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.on_event("startup")
def on_startup():
    logger.info("Старт успешен")
    if not DASHBOARD_LOGIN or not DASHBOARD_PASSWORD:
        logger.warning(
            "DASHBOARD_LOGIN/DASHBOARD_PASSWORD не заданы в .env — дашборд "
            "открыт без авторизации на уровне приложения. Это нормально, "
            "если доступ уже ограничен через Caddy basicauth (домен настроен "
            "в install.sh) или firewall/VPN, иначе — доступен всем, кто "
            "знает адрес сервера."
        )
    try:
        validate_config()
    except RuntimeError as e:
        # Не роняем сервер — просто дашборд покажет ошибку конфигурации при обновлении
        logger.error(str(e))


def _load_report(path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/manifest.json")
def pwa_manifest(_=Depends(require_auth)):
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def pwa_service_worker(_=Depends(require_auth)):
    # Отдаём именно с корня (не из /static/sw.js) — иначе браузер по
    # умолчанию ограничит scope service worker'а папкой /static/, и он не
    # сможет контролировать саму страницу дашборда на "/".
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


@app.get("/api/report")
def api_report(_=Depends(require_auth)):
    report = _load_report(REPORT_JSON_FILE)
    return JSONResponse(report or {})


@app.get("/api/status")
def api_status(_=Depends(require_auth)):
    return JSONResponse(read_status())


@app.post("/api/refresh")
def api_refresh(mode: str = "quick", _=Depends(require_auth)):
    if mode not in ("quick", "full"):
        return JSONResponse(
            {"error": f"Неизвестный режим обновления: {mode!r}. Допустимо: quick, full."},
            status_code=400,
        )
    started = start_refresh(mode=mode)
    if not started:
        return JSONResponse({"started": False, "message": "Обновление уже идёт"}, status_code=409)
    return JSONResponse({"started": True, "mode": mode})


@app.get("/api/game/{appid}/achievements")
def api_game_achievements(appid: int, _=Depends(require_auth)):
    """Модалка "все ачивки" для Steam-игры — ленивая загрузка по клику на плитку."""
    try:
        return JSONResponse(achievement_details.get_steam_game_achievements(appid))
    except Exception as e:
        logger.error(f"Ошибка при получении ачивок Steam appid={appid}: {e}\n{traceback.format_exc()}")
        return JSONResponse({"available": False, "achievements": [], "error": str(e)}, status_code=200)


# --- RetroAchievements: отдельные роуты, параллельные Steam-версии выше ---

@app.get("/api/retro/report")
def api_retro_report(_=Depends(require_auth)):
    report = _load_report(RETRO_REPORT_JSON_FILE)
    return JSONResponse(report or {})


@app.get("/api/retro/status")
def api_retro_status(_=Depends(require_auth)):
    return JSONResponse(retro_pipeline.read_status())


@app.post("/api/retro/refresh")
def api_retro_refresh(mode: str = "quick", _=Depends(require_auth)):
    if mode not in ("quick", "full"):
        return JSONResponse(
            {"error": f"Неизвестный режим обновления: {mode!r}. Допустимо: quick, full."},
            status_code=400,
        )
    started = retro_pipeline.start_refresh(mode=mode)
    if not started:
        return JSONResponse({"started": False, "message": "Обновление уже идёт"}, status_code=409)
    return JSONResponse({"started": True, "mode": mode})


@app.get("/api/retro/game/{game_id}/achievements")
def api_retro_game_achievements(game_id: int, _=Depends(require_auth)):
    """Та же модалка "все ачивки", источник — RetroAchievements."""
    try:
        return JSONResponse(achievement_details.get_retro_game_achievements(game_id))
    except Exception as e:
        logger.error(f"Ошибка при получении ачивок RA game_id={game_id}: {e}\n{traceback.format_exc()}")
        return JSONResponse({"available": False, "achievements": [], "error": str(e)}, status_code=200)
