import os
import logging
import logging.config
from logging import Logger

LOGGER_DEFAULT_NAME = "AppLogger"

if os.getenv("LOG_LEVEL"):
    level = logging.getLevelName(int(os.getenv("LOG_LEVEL")))
else:
    # Default log level to INFO
    level = logging.getLevelName(20)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(module)s - %(funcName)s - %(lineno)d - %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%SZ",
            "class": "pythonjsonlogger.jsonlogger.JsonFormatter",
        }
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
        }
    },
    "loggers": {
        LOGGER_DEFAULT_NAME: {"handlers": ["stdout"], "level": level},
    },
}


def get_logger() -> Logger:
    """
    Returns our default AppLogger class.

    Returns:
        Logger
    """
    logging.config.dictConfig(LOGGING)
    return logging.getLogger(LOGGER_DEFAULT_NAME)
