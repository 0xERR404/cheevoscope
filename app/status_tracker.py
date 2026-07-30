"""
Трекер статуса обновления (state/stage/progress/last_success_at/error) —
Steam- и RA-пайплайну нужна одна и та же логика, отличается только путь к
файлу статуса. Раньше это дублировалось почти дословно в pipeline.py и
retro_pipeline.py — вынесено сюда один раз.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from .io_utils import atomic_write_json

_IDLE_STATUS = {"state": "idle", "stage": None, "progress": None, "last_success_at": None, "error": None}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_status_tracker(status_file: Path):
    """
    Возвращает (read_status, write_status, progress_cb), уже привязанные
    к конкретному status_file — по одному набору на пайплайн (Steam/RA).
    """
    def read_status() -> dict:
        if not status_file.exists():
            return dict(_IDLE_STATUS)
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return dict(_IDLE_STATUS)

    def write_status(**kwargs) -> None:
        current = read_status()
        current.update(kwargs)
        current["updated_at"] = now_iso()
        atomic_write_json(status_file, current)

    def progress_cb(stage: str, done: int, total: int) -> None:
        write_status(state="running", stage=stage, progress={"done": done, "total": total})

    return read_status, write_status, progress_cb
