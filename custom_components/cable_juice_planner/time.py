"""Home Assistant time platform entry point."""
from .utils.time import CJPTime, async_setup_entry

__all__ = ["CJPTime", "async_setup_entry"]
