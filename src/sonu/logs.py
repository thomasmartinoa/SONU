"""Logging setup: a private rotating log file plus stderr, both passed through secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from sonu.config.paths import PRIVATE_FILE_MODE, SonuPaths
from sonu.redact import redact_text

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RedactingFilter(logging.Filter):
    """Rewrites each record's message with secrets removed."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        return True


def _private_rotating_handler(path: os.PathLike[str]) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=False
    )
    Path(path).chmod(PRIVATE_FILE_MODE)
    return handler


def setup_logging(paths: SonuPaths, verbosity: int = 0) -> None:
    """Configure the ``sonu`` logger. ``verbosity``: 0 = warnings, 1 = info, 2+ = debug on stderr."""
    logger = logging.getLogger("sonu")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redacting = RedactingFilter()
    formatter = logging.Formatter(_LOG_FORMAT)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(
        logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    )
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(redacting)
    logger.addHandler(stderr_handler)

    try:
        # Create every SONU directory top-down so each one gets 0700 (a bare
        # `log_dir.mkdir(parents=True)` would leave its parents at the umask default).
        paths.ensure_private_dirs()
        file_handler = _private_rotating_handler(paths.log_file)
    except OSError as exc:  # logging must never stop SONU from running
        logger.warning("file logging disabled: %s", exc)
        return
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redacting)
    logger.addHandler(file_handler)
