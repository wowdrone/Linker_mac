"""Explicit private namespace for unmodified Sentinel sources, independent of sys.path.

Each module gets a local import resolver. Linker module globals/sys.modules aliases
are never swapped, including during callbacks. Test-only; not shipped in the app.
"""
import builtins
from pathlib import Path
import sys
import types

class SentinelNamespace:
    def __init__(self, root):
        self.root = Path(root)
        self.name = '_linker_sentinel_fixture'
        self.modules = {}

    def load(self, name):
        if name in self.modules:
            return self.modules[name]
        path = self.root / (name + '.py')
        module = types.ModuleType(self.name + '.' + name)
        module.__file__ = str(path)
        module.__package__ = self.name
        local_builtins = vars(builtins).copy()
        local_builtins['__import__'] = self.import_module
        module.__dict__['__builtins__'] = local_builtins
        self.modules[name] = module
        sys.modules[module.__name__] = module
        exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
        return module

    def import_module(self, name, globals=None, locals=None, fromlist=(), level=0):
        if not level and '.' not in name and (self.root / (name+'.py')).is_file():
            return self.load(name)
        return builtins.__import__(name, globals, locals, fromlist, level)
