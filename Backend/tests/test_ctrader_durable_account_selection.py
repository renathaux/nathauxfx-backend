import json

import ctrader_connector as ctrader


def test_durable_selection_wins_over_checked_in_account_file(monkeypatch, tmp_path):
    account_file = tmp_path / "ctrader_accounts.json"
    account_file.write_text(json.dumps({
        "active_account_id": "47810571",
        "active_account_env": "demo",
        "accounts": [
            {"account_id": "47784297", "env": "demo"},
            {"account_id": "47810571", "env": "demo"},
        ],
    }))
    monkeypatch.setattr(ctrader, "CTRADER_ACCOUNTS_PATH", account_file)
    monkeypatch.setattr(ctrader, "load_active_account_selection", lambda: {
        "active_account_id": "47784297", "active_account_env": "demo",
    })

    settings = ctrader.load_ctrader_account_settings()
    assert settings["active_account_id"] == "47784297"
    assert ctrader.get_active_ctrader_account_id() == "47784297"


def test_background_account_file_refresh_cannot_overwrite_durable_selection(monkeypatch, tmp_path):
    account_file = tmp_path / "ctrader_accounts.json"
    monkeypatch.setattr(ctrader, "CTRADER_ACCOUNTS_PATH", account_file)
    durable_writes = []
    monkeypatch.setattr(ctrader, "save_active_account_selection", lambda *args, **kwargs: durable_writes.append((args, kwargs)))

    ctrader.save_ctrader_account_settings({"active_account_id": "47810571", "active_account_env": "demo"})
    assert durable_writes == []

    ctrader.save_ctrader_account_settings(
        {"active_account_id": "47784297", "active_account_env": "demo"},
        persist_selection=True,
    )
    assert durable_writes[0][0] == ("47784297", "demo")
