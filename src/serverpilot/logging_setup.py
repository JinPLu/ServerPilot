"""One logging configuration, applied once, by the process that serves.

The daemon used to write only whatever uvicorn's default handler emitted, into
a file launchd appended to forever. That file carried no timestamps, no process
id and no rotation, so a 12 MB log spanning weeks of daemon generations could
not answer "when did this happen" or "which daemon said it" -- the two questions
anyone actually opens a log to ask.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FILE_BYTES = 8 * 1024 * 1024
LOG_FILE_BACKUPS = 3
_FORMAT = "%(asctime)s.%(msecs)03dZ %(process)d %(levelname)s %(name)s %(message)s%(endpoint)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class _EndpointField(logging.Filter):
    """Give every record the endpoint suffix the format string expects.

    A record that names an endpoint should say so in the same place every time,
    and one that does not should not print an empty field.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        endpoint_id = getattr(record, "endpoint_id", None)
        record.endpoint = f" endpoint={endpoint_id}" if endpoint_id else ""
        return True


def configure_logging(log_dir: Path, *, level: int = logging.INFO) -> None:
    """Attach one rotating handler to the loggers this process writes through."""

    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log",
        maxBytes=LOG_FILE_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
    formatter.converter = __import__("time").gmtime
    handler.setFormatter(formatter)
    handler.addFilter(_EndpointField())

    root = logging.getLogger()
    root.setLevel(level)
    # Replace rather than add: called twice, this would otherwise write every
    # line as many times as it was called.
    for existing in [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]:
        root.removeHandler(existing)
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "asyncio"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
