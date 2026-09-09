"""Console logging helpers shared by the Asana ↔ GitHub CLIs."""

import logging
from pathlib import Path

from asana_sync.cfg import cfg


class ModuleLoggerAdapter(logging.LoggerAdapter):
    """
    Logger adapter that prepends the module name to all log messages.
    This ensures we can track which module created each log entry.
    """

    def __init__(self, logger, module_name):
        super().__init__(logger, {})
        self.module_name = module_name

    def process(self, msg, kwargs):
        """Prepend the module name to the log message."""
        return f"[{self.module_name}] {msg}", kwargs


def configure_logging() -> None:
    """Configure root logging to emit messages to the console."""
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.setLevel(cfg.log_level)
        return

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def get_logger(name: str, file_path: str | None = None) -> ModuleLoggerAdapter:
    """
    Get a logger adapter with 'app' as parent, which propagates to uvicorn.
    The adapter automatically prepends the module name to all log messages.

    Args:
        name: Logger name, typically __name__ from the calling module
        file_path: Optional __file__ path used to derive a nicer label when the
            module is executed as __main__

    Returns:
        ModuleLoggerAdapter instance that prepends module name and propagates through the app hierarchy
    """
    display_name = name
    if name == "__main__" and file_path:
        display_name = Path(file_path).stem

    logger = logging.getLogger(display_name)
    logger.setLevel(cfg.log_level)

    # Return an adapter that prepends the module name
    return ModuleLoggerAdapter(logger, display_name)
