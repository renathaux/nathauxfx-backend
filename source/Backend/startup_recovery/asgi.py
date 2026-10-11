"""Propagate this boot's admitted capability; never look up a replacement owner."""
from startup_recovery.types import RecoveryError


class RecoveryContextMiddleware:
    def __init__(self, app, *, runtime_api):
        self.app, self.runtime_api = app, runtime_api

    async def __call__(self, scope, receive, send):
        runtime = getattr(self.runtime_api, '_recovery_runtime', None)
        if scope['type'] not in {'http', 'websocket'} or runtime is None:
            return await self.app(scope, receive, send)
        context = runtime.runtime_context()
        try:
            context.__enter__()
        except RecoveryError:
            # Liveness/login/read-only diagnostics remain reachable. Mutation
            # guards see no caller capability and fail closed at their boundary.
            return await self.app(scope, receive, send)
        try:
            return await self.app(scope, receive, send)
        finally:
            context.__exit__(None, None, None)


def install(api):
    if getattr(api, '_RECOVERY_STARTUP_INSTALLED', False):
        return
    api._recovery_runtime = None
    api.app.add_middleware(RecoveryContextMiddleware, runtime_api=api)

    def startup():
        from startup_recovery.bootstrap import start
        start(api)
    startup._recovery_startup = True
    api.app.router.on_startup.append(startup)
    api._RECOVERY_STARTUP_INSTALLED = True
