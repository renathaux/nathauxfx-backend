"""Authenticated serving-process observations. No execution capability."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from live_integrity.auth import AccessDenied, read_admin, read_legacy_owner
from live_integrity.read_store import read_only
from startup_recovery.observability import StartupRecorder, UNKNOWN, timestamp

_HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


def _credentials(request):
    headers = request.headers.getlist('Authorization')
    if len(headers) > 1:
        raise AccessDenied(401)
    scheme = 'modern'
    if headers:
        header = headers[0]
        if header.startswith('Bearer '):
            scheme, token = 'legacy-owner', header[len('Bearer '):]
        elif header.startswith('FlowSignalUser '):
            token = header[len('FlowSignalUser '):].strip()
        else:
            raise AccessDenied(401)
    else:
        token = request.cookies.get('flowsignal_session', '')
    if not token or any(c.isspace() for c in token):
        raise AccessDenied(401)
    csrf_headers = request.headers.getlist('X-FlowSignal-CSRF')
    csrf = csrf_headers[0] if len(csrf_headers) == 1 else ''
    if len(token) > 2048 or (scheme == 'modern' and (not csrf or len(csrf) > 128)):
        raise AccessDenied(403)
    return scheme, token, csrf


def snapshot(api):
    recorder = getattr(api, '_runtime_identity', None)
    result = (recorder if isinstance(recorder, StartupRecorder) else StartupRecorder()).snapshot()
    # This is the API's installed runtime reference, not a census of thread-local
    # capabilities or a durable fencing check. Presence never means authority.
    token_present, producer = UNKNOWN, UNKNOWN
    missing = object()
    runtime = getattr(api, '_recovery_runtime', missing)
    if runtime is None:
        token_present, producer = False, False
    elif runtime is not missing:
        token = getattr(runtime, 'token', missing)
        if token is not missing:
            token_present = token is not None
        if getattr(runtime, 'writer', missing) is None:
            producer = False
    live = getattr(api, 'LIVE_AUTO_TRADE_ENABLED', None)
    enabled = live.get('enabled') if type(live) is dict else None
    result.update(schema_version='flowsignal-runtime-identity/v1',
        runtime_observation=dict(observed_at=timestamp(),
            live_auto_enabled=enabled if type(enabled) is bool else UNKNOWN,
            execution_active=UNKNOWN, runtime_token_present=token_present,
            checkpoint_producer_admitted=producer),
        authority_observation=dict(observed_at=timestamp(),
            broker_mutation_authority=UNKNOWN, REAL_ORDER_DISPATCH_AVAILABLE=False),
        captured_at=timestamp())
    return result


def install(api, *, engine=None):
    """Called by server setup, never by a request; injection is for local tests."""
    if engine is None:
        from db import engine
    router = APIRouter()

    @router.get('/admin/runtime-identity', include_in_schema=False)
    def identity(request: Request):
        try:
            scheme, token, csrf = _credentials(request)
            with read_only(engine) as connection:
                if scheme == 'legacy-owner':
                    read_legacy_owner(connection, token)
                else:
                    read_admin(connection, token, csrf)
            body = snapshot(api)
            return JSONResponse(body, headers=_HEADERS)
        except AccessDenied as exc:
            return JSONResponse({'detail': 'ACCESS_DENIED'}, status_code=exc.status, headers=_HEADERS)
        except Exception:
            # No arbitrary exception, URL, token or SQL parameters enter logs/output.
            return JSONResponse({'detail': 'DIAGNOSTIC_UNAVAILABLE'}, status_code=503, headers=_HEADERS)

    api.app.include_router(router)
