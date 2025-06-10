import logging
import os
import sys

# Logger for the entire package
logger = logging.getLogger("langgraph_dynamodb")

# Mapping of string levels to logging constants
LEVEL_MAP = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

# Read level from environment variable
env_level = os.getenv("LANGGRAPH_DYNAMODB_LOG_LEVEL", "INFO").upper()
level = LEVEL_MAP.get(env_level, logging.INFO)

# Apply the level to the logger
logger.setLevel(level)

# Warn if the level is invalid
if env_level not in LEVEL_MAP:
    logger.warning(
        f"Invalid LANGGRAPH_DYNAMODB_LOG_LEVEL='{env_level}', defaulting to INFO."
    )

# Add handler if not already configured (avoids duplicate logs)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# Optional: let advanced users override logging programmatically
def configure_logging(level=logging.INFO, stream=None, log_format=None):
    """
    Programmatic way to configure the package logger.

    Args:
        level (int): Logging level (e.g., logging.DEBUG).
        stream (IO): Stream to log to (e.g., sys.stdout or a file object).
        log_format (str): Custom log format.
    """
    stream = stream or sys.stdout
    log_format = log_format or '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'

    logger.setLevel(level)
    logger.handlers.clear()

    handler = logging.StreamHandler(stream)
    formatter = logging.Formatter(log_format)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
