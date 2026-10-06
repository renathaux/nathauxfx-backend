def validate_verified_account_snapshot(account):
    account = account or {}

    if not account.get("ok"):
        return {
            "ok": False,
            "reason": "ACCOUNT BALANCE NOT VERIFIED",
            "account_verification_reason": (
                account.get("reason")
                or "Could not confirm active cTrader account balance/equity"
            ),
            "account": account,
        }

    balance = account.get("balance")
    equity = account.get("equity")
    balance_verified = account.get("balance_verified")
    equity_verified = account.get("equity_verified")

    if balance_verified is None:
        balance_verified = balance is not None

    if equity_verified is None:
        equity_verified = equity is not None

    if (
        balance is None
        or equity is None
        or not balance_verified
        or not equity_verified
        or account.get("cached_balance_used")
    ):
        return {
            "ok": False,
            "reason": "ACCOUNT BALANCE NOT VERIFIED",
            "account_verification_reason": "Fresh cTrader account balance/equity was not verified",
            "account": account,
        }

    try:
        account_value = float(equity)
    except (TypeError, ValueError):
        account_value = 0

    if account_value <= 0:
        return {
            "ok": False,
            "reason": "Cannot calculate risk without account equity",
            "account": account,
        }

    return {
        "ok": True,
        "balance": balance,
        "equity": equity,
        "account_equity_used": account_value,
        "account": account,
    }


def log_account_balance_verification_failed(symbol, account, reason=None):
    account = account or {}
    details = {
        "symbol": symbol,
        "reason": reason or account.get("reason") or "Fresh cTrader account balance/equity was not verified",
        "account_id": account.get("account_id"),
        "account_number": account.get("account_number"),
        "broker_environment": account.get("mode"),
        "balance": account.get("balance"),
        "equity": account.get("equity"),
        "equity_source": account.get("equity_source"),
        "balance_verified": account.get("balance_verified"),
        "equity_verified": account.get("equity_verified"),
        "cached_balance_used": account.get("cached_balance_used"),
        "cached_balance": account.get("cached_balance"),
        "cached_equity": account.get("cached_equity"),
        "account_list_last_refresh": account.get("account_list_last_refresh"),
        "snapshot_refreshed_at": account.get("snapshot_refreshed_at"),
    }
    print("ACCOUNT_BALANCE_VERIFICATION_FAILED =", details)
    return details

