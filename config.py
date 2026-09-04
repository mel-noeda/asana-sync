"""Compatibility alias so callers can import `config` or `cfg`."""

from cfg import Config, cfg

config = cfg

__all__ = ["Config", "cfg", "config"]
