from fastapi import APIRouter, HTTPException
from services.risk_service import get_risk_settings, update_risk_settings
from services.settings_service import load_feature_flags, save_feature_flags
from services import active_strategy_config_service as active_strategy_config
from services.v3b_strategy_settings_sync import install_v3b_strategy_settings_sync
from routes.user_auth import router as user_auth_router
from routes.password_reset import router as password_reset_router


# Install before api.py imports the legacy strategy-settings functions. This
# keeps the existing authenticated /strategy/settings API path stable while its
# source of truth becomes the active V3B production profile.
install_v3b_strategy_settings_sync()

# V3B's 1.90R target is still coupled to the legacy broker-core RR guard. Keep
# that one value fixed until the broker core is migrated too. The protection
# trigger and protected-stop percentages are fully synchronized/editable now.
from services import strategy_settings_service as _strategy_settings_compat

_synced_get_strategy_settings = _strategy_settings_compat.get_strategy_settings
_synced_save_strategy_settings = _strategy_settings_compat.save_strategy_settings


def _get_strategy_settings_with_editability(*args, **kwargs):
    data = _synced_get_strategy_settings(*args, **kwargs)
    if isinstance(data, dict):
        data = dict(data)
        data["editable"] = [
            "protection_trigger_percent",
            "protected_stop_percent",
        ]
    return data


def _save_strategy_settings_with_v3b_rr_guard(payload, *args, **kwargs):
    if isinstance(payload, dict) and "target_rr" in payload:
        requested = float(payload.get("target_rr"))
        expected = float(active_strategy_config.defaults()["target_rr"])
        if abs(requested - expected) > 1e-9:
            raise _strategy_settings_compat.StrategySettingsValidationError(
                "target_rr is fixed at 1.90R for the current V3B broker profile"
            )
        payload = dict(payload)
        payload.pop("target_rr", None)
    return _synced_save_strategy_settings(payload, *args, **kwargs)


_strategy_settings_compat.get_strategy_settings = _get_strategy_settings_with_editability
_strategy_settings_compat.save_strategy_settings = _save_strategy_settings_with_v3b_rr_guard

router = APIRouter()
# api.py already mounts this router. Include the database-backed auth routers
# here so customer signup/login/verification/session/password reset are exposed.
router.include_router(user_auth_router)
router.include_router(password_reset_router)


def _strategy_synced_risk_settings(risk=None):
    result = dict(risk if isinstance(risk, dict) else get_risk_settings())
    active = active_strategy_config.get_active_strategy_settings()
    current = active.get("current") or active_strategy_config.defaults()
    # These two legacy Risk Management fields are the same V3B management
    # controls. Keep their old JSON names for frontend compatibility, but make
    # the active strategy profile the authoritative source.
    result["tp1PercentOfTp2"] = float(current["protection_trigger_percent"])
    result["protectedSlPercentOfTp2"] = float(current["protected_stop_percent"])
    return result


@router.get("/settings/risk")
def settings_risk_get():
    return {
        "ok": True,
        "risk": _strategy_synced_risk_settings(),
        "strategy_profile": active_strategy_config.ACTIVE_STRATEGY_PROFILE,
        "strategy_version": active_strategy_config.ACTIVE_STRATEGY_VERSION,
    }


@router.post("/settings/risk")
def settings_risk_post(payload: dict):
    strategy_update = {}
    if "tp1PercentOfTp2" in payload:
        strategy_update["protection_trigger_percent"] = payload["tp1PercentOfTp2"]
    if "protectedSlPercentOfTp2" in payload:
        strategy_update["protected_stop_percent"] = payload["protectedSlPercentOfTp2"]

    try:
        # Validate the strategy-owned values against the complete active profile
        # before writing either store. This catches impossible protected-stop
        # geometry (for example protected SL beyond the trigger).
        if strategy_update:
            current = active_strategy_config.get_active_values()
            active_strategy_config.validate({**current, **strategy_update}, merge_defaults=True)
        risk = update_risk_settings(payload)
        if strategy_update:
            active_strategy_config.save_active_strategy_settings(
                strategy_update,
                updated_by="risk_settings_api",
            )
    except (ValueError, active_strategy_config.ActiveStrategyConfigError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "risk": _strategy_synced_risk_settings(risk),
        "strategy_profile": active_strategy_config.ACTIVE_STRATEGY_PROFILE,
        "strategy_version": active_strategy_config.ACTIVE_STRATEGY_VERSION,
    }


@router.get("/feature-flags")
def feature_flags_get():
    return {
        "ok": True,
        "flags": load_feature_flags(),
    }


@router.post("/feature-flags")
def feature_flags_post(payload: dict):
    return {
        "ok": True,
        "flags": save_feature_flags(payload),
    }
