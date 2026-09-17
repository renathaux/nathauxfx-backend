from unittest.mock import MagicMock

import api
from ctrader_account_context import AccountIdentity


def test_account_switch_without_managed_studio_position_uses_existing_switch(monkeypatch):
    old = AccountIdentity("acct-a", "demo")
    new = AccountIdentity("acct-b", "demo")
    switched = MagicMock(return_value={"ok": True, "account_id": "acct-b"})
    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda: "owner-1")
    monkeypatch.setattr(api, "selected_identity", lambda: old, raising=False)
    monkeypatch.setattr(api, "get_open_positions", lambda: [])
    monkeypatch.setattr(api, "studio_account_has_managed_position", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(api, "set_active_ctrader_account", switched)
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda force=False: {"account_id": "acct-b"})

    result = api.switch_ctrader_account_with_studio_management("acct-b", confirmed=False)

    assert result["ok"] is True
    assert result.get("confirmation_required") is not True
    switched.assert_called_once_with("acct-b")


def test_switch_away_with_managed_studio_position_requires_explicit_confirmation(monkeypatch):
    old = AccountIdentity("acct-a", "demo")
    switched = MagicMock()
    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda: "owner-1")
    monkeypatch.setattr(api, "selected_identity", lambda: old, raising=False)
    monkeypatch.setattr(api, "get_open_positions", lambda: [{"position_id": "pos-1", "symbol": "EURUSD"}])
    monkeypatch.setattr(api, "studio_account_has_managed_position", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(api, "set_active_ctrader_account", switched)

    result = api.switch_ctrader_account_with_studio_management("acct-b", confirmed=False)

    assert result["ok"] is False
    assert result["confirmation_required"] is True
    assert "cTrader" in result["warning"]
    assert "management" in result["warning"].lower()
    switched.assert_not_called()


def test_confirmed_switch_suspends_old_then_switches_then_resumes_new(monkeypatch):
    old = AccountIdentity("acct-a", "demo")
    new = AccountIdentity("acct-b", "demo")
    identities = iter([old, new])
    sequence = []
    old_positions = [{"position_id": "pos-old", "symbol": "EURUSD"}]
    new_positions = [{"position_id": "pos-new", "symbol": "XAUUSD"}]
    position_reads = iter([old_positions, new_positions])

    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda: "owner-1")
    monkeypatch.setattr(api, "selected_identity", lambda: next(identities), raising=False)
    monkeypatch.setattr(api, "get_open_positions", lambda: next(position_reads))
    monkeypatch.setattr(api, "studio_account_has_managed_position", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(
        api,
        "suspend_studio_account_management",
        lambda owner, identity, positions: sequence.append(("suspend", identity.account_id, positions)) or {"suspended": 1},
        raising=False,
    )
    monkeypatch.setattr(
        api,
        "set_active_ctrader_account",
        lambda account_id: sequence.append(("switch", account_id)) or {"ok": True, "account_id": account_id},
    )
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda force=False: sequence.append(("sync", force)) or {"account_id": "acct-b"})
    monkeypatch.setattr(api, "get_live_prices", lambda: {"live_prices": {"XAUUSD": {"bid": 4000, "ask": 4000.1}}})
    monkeypatch.setattr(
        api,
        "resume_studio_account_management",
        lambda owner, identity, positions, prices: sequence.append(("resume", identity.account_id, positions, prices)) or {"actions": []},
        raising=False,
    )

    result = api.switch_ctrader_account_with_studio_management("acct-b", confirmed=True)

    assert result["ok"] is True
    assert [item[0] for item in sequence] == ["suspend", "switch", "sync", "resume"]
    assert sequence[0][1] == "acct-a"
    assert sequence[-1][1] == "acct-b"


def test_studio_gate_off_keeps_account_switch_behavior_unchanged(monkeypatch):
    switched = MagicMock(return_value={"ok": True, "account_id": "acct-b"})
    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda: None)
    monkeypatch.setattr(api, "set_active_ctrader_account", switched)
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda force=False: {"account_id": "acct-b"})
    monkeypatch.setattr(api, "get_open_positions", lambda: (_ for _ in ()).throw(AssertionError("gate off must not inspect positions")))

    result = api.switch_ctrader_account_with_studio_management("acct-b", confirmed=False)

    assert result["ok"] is True
    switched.assert_called_once_with("acct-b")
