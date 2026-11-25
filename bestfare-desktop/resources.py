"""Files the program reads that are not code (the airport list, the coupon rules): from the release in memory, or from disk in the workspace."""
from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent if not __file__.startswith("<") else Path(".")


def read_bytes(name: str) -> bytes:
    try:
        import packed
        if packed.ACTIVE:
            return packed.RESOURCES[name]
    except ImportError:
        pass
    return (APP_DIR / name).read_bytes()


def read_text(name: str) -> str:
    return read_bytes(name).decode("utf-8")
