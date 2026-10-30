"""Official-source indicator modules. Importing this package registers every builder."""

import importlib
import pkgutil

from .base import REGISTRY, SECTIONS, Http, register  # noqa: F401

IMPORT_ERRORS: dict = {}


def load_all() -> dict:
    """Import every module in this package; a module that fails to import is skipped and reported."""
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_") or m.name == "base":
            continue
        try:
            importlib.import_module(f"{__name__}.{m.name}")
        except Exception as e:  # noqa: BLE001
            IMPORT_ERRORS[m.name] = f"{type(e).__name__}: {e}"
    return REGISTRY
