import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """
    Пишет JSON во временный файл рядом с целевым и переименовывает его поверх
    оригинала (os.replace — атомарная операция на одной файловой системе).
    Если процесс упадёт посреди записи (например, сервер перезагрузили в
    момент /api/refresh), исходный файл останется целым, а не обрежется
    наполовину — вместо этого просто не будет обновлён.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
