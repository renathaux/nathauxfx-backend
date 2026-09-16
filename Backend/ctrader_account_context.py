"""Operation-local account identity, not an account activation workflow."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import RLock


class AccountSelectionChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountIdentity:
    account_id: str
    environment: str
    selection_revision: str | None = None

    @property
    def scope(self):
        return f"CTRADER:{self.environment.upper()}:{self.account_id}"


_identity = ContextVar("ctrader_operation_identity", default=None)
# Only serialize local execution-state publication and explicit selection.
# Market fetching/analysis do not acquire this lock.
account_state_lock = RLock()


def current_identity():
    return _identity.get()


def selected_identity():
    import ctrader_connector as connector
    import os
    settings = connector.load_ctrader_account_settings()
    account_id = settings.get("active_account_id")
    environment = settings.get("active_account_env")
    if not settings.get("_durable_selection_authoritative"):
        account_id = account_id or os.getenv("ACTIVE_CTRADER_ACCOUNT_ID") or os.getenv("CTRADER_ACCOUNT_ID")
        environment = environment or os.getenv("ACTIVE_CTRADER_ACCOUNT_ENV") or os.getenv("CTRADER_ENV", "demo")
    if not account_id or str(environment).lower() not in {"demo", "live"}:
        return None
    return AccountIdentity(str(account_id), str(environment).lower(), settings.get("selection_revision"))


def assert_current_selection(identity=None):
    captured = identity or current_identity()
    if captured is not None and captured != selected_identity():
        raise AccountSelectionChanged("cTrader account selection changed during operation")


@contextmanager
def pinned_account(identity=None):
    captured = current_identity() or identity or selected_identity()
    token = _identity.set(captured)
    try:
        yield captured
    finally:
        _identity.reset(token)


def account_operation(function):
    """Capture once; nested connector calls reuse the same account and environment."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with pinned_account():
            return function(*args, **kwargs)
    return wrapped


def account_state_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with account_state_lock, pinned_account():
            return function(*args, **kwargs)
    return wrapped


def market_read_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with pinned_account() as identity:
            result = function(*args, **kwargs)
            assert_current_selection(identity)
            if identity is not None and hasattr(result, "attrs"):
                source = result.attrs.get("ctrader_stream_scope")
                if source and source != identity.scope:
                    raise AccountSelectionChanged("market frame account does not match operation")
                result.attrs["ctrader_stream_scope"] = identity.scope
            return result
    return wrapped
