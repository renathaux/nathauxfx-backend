from ctrader_connector import (
    build_ctrader_authorization_url,
    fetch_ctrader_accounts,
    forget_ctrader_account,
    get_active_ctrader_account_id,
    get_ctrader_candle_cache_status,
    get_ctrader_diagnostics,
    set_active_ctrader_account,
)


def get_oauth_debug():
    return build_ctrader_authorization_url()


def get_account_list(refresh=False):
    return fetch_ctrader_accounts(refresh=refresh)


def select_active_account(account_id):
    return set_active_ctrader_account(account_id)


def forget_account(account_id):
    return forget_ctrader_account(account_id)


def get_health_snapshot():
    diagnostics = get_ctrader_diagnostics()
    candle_status = get_ctrader_candle_cache_status()

    return {
        "ctrader_auth_status": diagnostics.get("auth_status"),
        "active_account": get_active_ctrader_account_id(),
        "last_candle_time": candle_status.get("ctrader_last_success"),
        "last_candle_error": candle_status.get("ctrader_last_error"),
    }
