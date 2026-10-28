"""Home Assistant datetime platform entry point."""
from .utils.datetime import CJPDateTime, async_setup_entry

__all__ = ["CJPDateTime", "async_setup_entry"]
