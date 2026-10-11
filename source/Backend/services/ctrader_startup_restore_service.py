from __future__ import annotations


def restore_single_authorized_ctrader_account(fetch_accounts, set_active_account):
    """Recover a stale/missing cTrader selection without guessing.

    A deploy can start with an old checked-in account file while the durable
    OAuth token authorizes a newer account. If exactly one account is currently
    authorized, it is safe to restore that sole account. With multiple
    authorized accounts we never choose on the user's behalf.
    """
    try:
        accounts_result = fetch_accounts(refresh=True)
    except Exception as exc:
        return {
            "ok": False,
            "restored": False,
            "reason": f"account refresh failed: {exc}",
        }

    if not isinstance(accounts_result, dict) or not accounts_result.get("ok"):
        return {
            "ok": False,
            "restored": False,
            "reason": (
                accounts_result.get("reason")
                if isinstance(accounts_result, dict)
                else "invalid account refresh response"
            ),
        }

    active_account_id = str(
        accounts_result.get("active_account_id") or ""
    ).strip() or None
    authorized_account_ids = [
        str(value).strip()
        for value in (accounts_result.get("authorized_account_ids") or [])
        if str(value or "").strip()
    ]

    if active_account_id and active_account_id in authorized_account_ids:
        return {
            "ok": True,
            "restored": False,
            "reason": "active account already authorized",
            "active_account_id": active_account_id,
            "authorized_account_ids": authorized_account_ids,
        }

    if len(authorized_account_ids) != 1:
        return {
            "ok": True,
            "restored": False,
            "reason": (
                "no authorized account available"
                if not authorized_account_ids
                else "multiple authorized accounts require explicit selection"
            ),
            "active_account_id": active_account_id,
            "authorized_account_ids": authorized_account_ids,
        }

    recovered_account_id = authorized_account_ids[0]
    try:
        selection_result = set_active_account(recovered_account_id)
    except Exception as exc:
        return {
            "ok": False,
            "restored": False,
            "reason": f"account restore failed: {exc}",
            "active_account_id": active_account_id,
            "authorized_account_ids": authorized_account_ids,
        }

    if not isinstance(selection_result, dict) or not selection_result.get("ok"):
        return {
            "ok": False,
            "restored": False,
            "reason": (
                selection_result.get("reason")
                if isinstance(selection_result, dict)
                else "invalid account selection response"
            ),
            "active_account_id": active_account_id,
            "authorized_account_ids": authorized_account_ids,
        }

    return {
        "ok": True,
        "restored": True,
        "reason": "sole authorized account restored",
        "active_account_id": recovered_account_id,
        "authorized_account_ids": authorized_account_ids,
        "environment": selection_result.get("env"),
    }
