"""
Почасовая автопроверка новых достижений — quick-режим по обеим платформам.

Запускается systemd-таймером cheevoscope-refresh.timer (см. install.sh), а
не вручную. Специально НЕ ходит через HTTP/веб-сервер: напрямую вызывает
тот же код, что кнопка "Обновить" на сайте, и пишет те же report.json /
retro_report.json на диске — сайт их просто читает, ему не важно, кто
именно их обновил.

Почему quick, а не full: quick и так подтягивает новые ачивки для игр "в
процессе" (см. app/pipeline.py) — этого достаточно для регулярной
автопроверки. full раз в час означал бы наравне с этим ежечасную полную
очистку кэша цен/отзывов/картинок — совершенно не нужно для того, чтобы
просто узнать про новые ачивки, и было бы намного тяжелее для Steam API.
Если нужен именно full — это по-прежнему кнопка "Обновить всё" вручную.

Ручной запуск для проверки: python -m app.hourly_refresh
"""
from . import pipeline
from . import retro_pipeline
from .config import RA_USERNAME, RA_API_KEY
from .logger_setup import get_logger

logger = get_logger()


def main() -> None:
    logger.info("Почасовая автопроверка: запускаю Steam (quick).")
    try:
        pipeline.run_refresh_sync("quick")
    except Exception as e:
        # run_refresh_sync и так ловит свои ошибки и пишет их в status.json,
        # но STEAM_API_KEY/STEAM_ID в .env вообще отсутствуют — это уже
        # ошибка конфигурации, которая всплывёт как исключение раньше, до
        # try/except внутри _run_pipeline. Ловим здесь, чтобы одна
        # некорректно настроенная платформа не обрывала вторую.
        logger.error(f"Почасовая автопроверка: Steam не выполнен — {e}")

    if RA_USERNAME and RA_API_KEY:
        logger.info("Почасовая автопроверка: запускаю RetroAchievements (quick).")
        try:
            retro_pipeline.run_refresh_sync("quick")
        except Exception as e:
            logger.error(f"Почасовая автопроверка: RetroAchievements не выполнен — {e}")
    else:
        logger.info("Почасовая автопроверка: RA_USERNAME/RA_API_KEY пусты — RA пропущен.")


if __name__ == "__main__":
    main()
