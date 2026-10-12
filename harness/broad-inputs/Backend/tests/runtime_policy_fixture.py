"""Explicit, function-scoped policy assembly for downstream policy tests."""
from contextlib import ExitStack
from importlib import import_module
from unittest.mock import patch

import pytest


@pytest.fixture
def assembled_runtime_policies(monkeypatch):
    # Snapshot only modules whose bindings the real assembly (including its
    # lazy provider-cache wrapper) can change. Never install policies at import.
    modules = [
        import_module(name) for name in (
            'services.indicator_event_stream_service',
            'services.paper_live_entry_service',
            'services.strategy_diagnostics_service',
            'services.v2_shadow_service',
            'ctrader_connector',
            'api',
        )
    ]
    with ExitStack() as stack:
        for module in modules:
            stack.enter_context(patch.dict(vars(module)))
        from services import assemble_runtime_policies
        assemble_runtime_policies()
        try:
            yield
        finally:
            # Test patches captured the assembled functions. Undo those first,
            # then restore pre-assembly bindings so unrelated tests stay pure.
            monkeypatch.undo()
