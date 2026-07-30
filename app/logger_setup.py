import logging
from logging.handlers import RotatingFileHandler
from .config import LOG_FILE


def get_logger(name: str = "cheevoscope") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        # Логгер уже настроен (например, при повторном импорте) — не дублируем хендлеры
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Ротация: до 5 файлов по 5 МБ (logs.txt, logs.txt.1, ... logs.txt.5) —
    # раньше файл рос бесконечно и никогда не чистился.
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
