from pathlib import Path

path = Path("Backend/api.py")
text = path.read_text(encoding="utf-8")


def replace_once(old, new, label):
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    text = text.replace(old, new, 1)


replace_once(
    "from ctrader_account_context import (\n    account_operation, account_state_operation, current_identity,\n    assert_current_selection, AccountSelectionChanged,\n)\n",
    "from ctrader_account_context import (\n    account_operation, account_state_operation, current_identity, selected_identity,\n    assert_current_selection, AccountSelectionChanged,\n)\n",
    "account identity import",
)

replace_once(
    "from services.strategy_studio_live_candidate import build_studio_candidate\n",
    "from services.strategy_studio_live_candidate import build_studio_candidate\n"
    "from services.strategy_studio_position_manager import (\n"
    "    account_has_managed_position as studio_account_has_managed_position,\n"
    "    suspend_account_management as suspend_studio_account_management,\n"
    "    resume_account_management as resume_studio_account_management,\n"
    ")\n",
    "position manager import",
)

old_endpoint = '''@app.post("/ctrader/accounts/active")
def ctrader_accounts_active_endpoint(payload: dict):
    result = set_active_ctrader_account(
        payload.get("accountId")
        or payload.get("account_id")
    )
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }
'''

helper_and_endpoint = '''def switch_ctrader_account_with_studio_management(account_id, confirmed=False):
    """Switch accounts without allowing Studio app management to cross scope."""
    target_account_id = str(account_id or "").strip()
    if not target_account_id:
        return {"ok": False, "reason": "account_id is required"}

    owner_id = get_enabled_studio_live_owner()
    if not owner_id:
        result = set_active_ctrader_account(target_account_id)
        sync_ctrader_account_state(force=True)
        return {**result, "live_account": LIVE_ACCOUNT_STATE}

    old_identity = selected_identity()
    if old_identity is None or str(old_identity.account_id) == target_account_id:
        result = set_active_ctrader_account(target_account_id)
        sync_ctrader_account_state(force=True)
        return {**result, "live_account": LIVE_ACCOUNT_STATE}

    old_positions = get_open_positions() or []
    has_managed_position = studio_account_has_managed_position(
        owner_id, old_identity, old_positions
    )
    if has_managed_position and confirmed is not True:
        return {
            "ok": False,
            "confirmation_required": True,
            "reason": "STUDIO_MANAGED_POSITION_SWITCH_CONFIRMATION_REQUIRED",
            "warning": (
                "Switching accounts will stop NathauxFX Strategy Studio app management "
                "for the current account. The cTrader position and broker SL/TP remain open."
            ),
            "current_account_id": old_identity.account_id,
            "requested_account_id": target_account_id,
        }

    if has_managed_position:
        suspend_studio_account_management(owner_id, old_identity, old_positions)

    result = set_active_ctrader_account(target_account_id)
    if not result.get("ok", False):
        if has_managed_position:
            try:
                prices = ((get_live_prices() or {}).get("live_prices") or {})
                resume_studio_account_management(
                    owner_id, old_identity, old_positions, prices
                )
            except Exception as exc:
                print("STUDIO_ACCOUNT_SWITCH_ROLLBACK_RESUME_ERROR =", str(exc))
        sync_ctrader_account_state(force=True)
        return {**result, "live_account": LIVE_ACCOUNT_STATE}

    sync_ctrader_account_state(force=True)
    new_identity = selected_identity()
    resume_result = None
    if new_identity is not None:
        new_positions = get_open_positions() or []
        prices = ((get_live_prices() or {}).get("live_prices") or {})
        resume_result = resume_studio_account_management(
            owner_id, new_identity, new_positions, prices
        )

    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
        "studio_management": {
            "owner_id": owner_id,
            "previous_account_suspended": bool(has_managed_position),
            "resume": resume_result,
        },
    }


@app.post("/ctrader/accounts/active")
def ctrader_accounts_active_endpoint(payload: dict):
    return switch_ctrader_account_with_studio_management(
        payload.get("accountId") or payload.get("account_id"),
        confirmed=bool(payload.get("confirmed") or payload.get("confirm")),
    )
'''
replace_once(old_endpoint, helper_and_endpoint, "primary account switch endpoint")

old_legacy = '''@app.post("/set-active-ctrader-account")
def set_active_ctrader_account_endpoint(payload: dict):
    result = set_active_ctrader_account(payload.get("account_id"))
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }
'''
new_legacy = '''@app.post("/set-active-ctrader-account")
def set_active_ctrader_account_endpoint(payload: dict):
    return switch_ctrader_account_with_studio_management(
        payload.get("account_id"),
        confirmed=bool(payload.get("confirmed") or payload.get("confirm")),
    )
'''
replace_once(old_legacy, new_legacy, "legacy account switch endpoint")

path.write_text(text, encoding="utf-8")
