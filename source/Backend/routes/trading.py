from fastapi import APIRouter

from fundamentals.ingestion import start_fundamental_ingestion_scheduler
from routes.fundamentals import router as fundamentals_router
from routes.user_auth import router as user_auth_router

router = APIRouter()
router.include_router(fundamentals_router)
router.include_router(user_auth_router)


@router.on_event("startup")
def start_fundamental_collection():
    try:
        import api
        from services.customer_forex_guard import install_owner_forex_mutation_guard
        guard = install_owner_forex_mutation_guard(api.app, api.SESSIONS)
        print("CUSTOMER_FOREX_MUTATION_GUARD =", guard)
    except Exception as exc:
        print("CUSTOMER_FOREX_MUTATION_GUARD_ERROR =", str(exc))
        raise
    # Security wrappers still install on blocked/standby workers. A scheduler
    # cannot use late router registration to bypass account recovery admission.
    from startup_recovery.runtime import require_worker_admission
    from startup_recovery.types import RecoveryError
    try:
        require_worker_admission()
    except RecoveryError as exc:
        return {'ok': False, 'reason': exc.code}
    result = start_fundamental_ingestion_scheduler()
    print('FUNDAMENTAL_SCHEDULER_START =', result)
    return result


@router.get("/trading/health")
def trading_health():
    return {
        "ok": True,
        "message": "Trading route module loaded",
    }
