"""App/request-scoped Explorer paths; legacy deployments retain environment defaults."""
import os
from contextvars import ContextVar
from pathlib import Path

PATHS = ContextVar("nutriomics_explorer_paths", default=None)


def configured_path(name, default):
    overrides = PATHS.get()
    return Path(overrides[name] if overrides and name in overrides else os.environ.get(name, default))


class ExplorerContextMiddleware:
    def __init__(self, app, paths):
        self.app, self.paths = app, dict(paths)

    async def __call__(self, scope, receive, send):
        token = PATHS.set(self.paths)
        try:
            await self.app(scope, receive, send)
        finally:
            PATHS.reset(token)
