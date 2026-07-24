"""Console + rotating file logging. Keep console output plain ASCII for Windows."""
import logging
import logging.handlers
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s", datefmt="%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:
        return

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    filed = logging.handlers.RotatingFileHandler(
        "logs/engine.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    filed.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s")
    )
    root.addHandler(filed)

    for noisy in ("httpx", "httpcore", "apscheduler", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
