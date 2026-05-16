from __future__ import annotations

import sys

from loguru import logger

from tradingbot.config import Settings


def configure_logging(settings: Settings) -> None:
    settings.log_dir_path.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
            "<level>{level:<7}</level> "
            "<cyan>{name}:{function}:{line}</cyan> | <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        settings.log_dir_path / "tradingbot.log",
        level=settings.log_level,
        rotation="00:00",
        retention="30 days",
        compression="zip",
        enqueue=True,
    )
