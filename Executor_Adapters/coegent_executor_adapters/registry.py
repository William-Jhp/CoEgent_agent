"""Explicit registration and lazy discovery: load only the selected executor."""
from importlib.metadata import entry_points
import re

ENTRY_POINT_GROUP = 'coegent.executors'


class ExecutorRegistry:
    def __init__(self, *, discover_plugins=False):
        self._factories = {}
        self._disabled = set()
        self.discover_plugins = discover_plugins

    def _entries(self):
        return entry_points(group=ENTRY_POINT_GROUP) if self.discover_plugins else ()

    @property
    def names(self):
        return tuple(sorted((set(self._factories) | {e.name for e in self._entries()}) - self._disabled))

    def register(self, name, factory):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', name):
            raise ValueError('invalid executor name')
        if not callable(factory):
            raise ValueError('executor factory must be callable')
        if name in self.names:
            raise ValueError(f'executor already registered: {name}')
        self._factories[name] = factory
        self._disabled.discard(name)

    def unregister(self, name):
        if name not in self.names:
            raise ValueError(f'unknown executor: {name}')
        self._factories.pop(name, None)
        self._disabled.add(name)

    def resolve(self, name):
        if name in self._disabled:
            raise ValueError(f'unknown executor: {name}')
        if name in self._factories:
            return self._factories[name]
        entries = [e for e in self._entries() if e.name == name]
        if len(entries) > 1:
            raise ValueError(f'executor registered by multiple packages: {name}')
        if not entries:
            raise ValueError(f'unknown executor: {name}')
        factory = entries[0].load()
        if not callable(factory):
            raise ValueError('executor entry point must expose a factory')
        self._factories[name] = factory
        return factory
