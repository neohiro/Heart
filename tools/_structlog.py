# Heart/tools/_structlog.py
#
# Shared structlog configuration for Heart scrapers and pollers.
# Idempotent: repeated imports are no-ops via already_configured.
#
# Use:
#   from _structlog import configure_logger
#   LOG = configure_logger("heart.<name>", key_order=["event", "phase", ...])
import structlog


def _drop_none(logger, method, event_dict):
    return {k: v for k, v in event_dict.items() if v is not None}


def configure_logger(name: str, key_order: list[str]) -> structlog.stdlib.BoundLogger:
    structlog.configure(
        processors=[
            _drop_none,
            structlog.processors.KeyValueRenderer(key_order=key_order),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    return structlog.get_logger(name)
