"""Running the app from memory.

A release arrives as encrypted, signed files. Once the licence has been checked and the key delivered, the files are opened IN MEMORY
and imported from there: the program's code is never written to disk in readable form. (This is part of the shell.)
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys

ACTIVE = False
RESOURCES: dict[str, bytes] = {}                 # non-code files of the release (stations.json, ext/core.js ...)
_finder = None


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources: dict[str, bytes]):
        self.sources = sources

    def find_spec(self, fullname, path=None, target=None):
        if "." in fullname or fullname not in self.sources:
            return None
        return importlib.util.spec_from_loader(fullname, self, origin=f"<bestfare:{fullname}.py>")

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        module.__file__ = f"<bestfare:{name}.py>"
        exec(compile(self.sources[name], module.__file__, "exec", dont_inherit=True), module.__dict__)


def install(files: dict[str, bytes]) -> list[str]:
    """Make the release's modules importable. Returns their names. Modules that are already loaded (the shell's own) are not replaced."""
    global ACTIVE, _finder
    mods = {p[:-3]: d for p, d in files.items() if p.endswith(".py") and "/" not in p}
    RESOURCES.clear()
    RESOURCES.update({p: d for p, d in files.items() if p not in {m + ".py" for m in mods}})
    if _finder is not None and _finder in sys.meta_path:
        sys.meta_path.remove(_finder)
    _finder = _Finder({m: d for m, d in mods.items() if m not in sys.modules})
    sys.meta_path.insert(0, _finder)
    ACTIVE = True
    return sorted(mods)


def uninstall() -> None:
    global ACTIVE, _finder
    if _finder is not None and _finder in sys.meta_path:
        sys.meta_path.remove(_finder)
    _finder = None
    ACTIVE = False
    RESOURCES.clear()
