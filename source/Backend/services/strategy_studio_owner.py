"""Strategy library identity, derived exclusively from authenticated actors."""

from fastapi import HTTPException


def canonical_strategy_owner(actor):
    def field(name):
        return (
            actor.get(name) if isinstance(actor, dict) else getattr(actor, name, None)
        )

    role = str(field("role") or "").strip().lower()
    if role == "admin":
        email = str(field("email") or "").strip().lower()
        # A missing/placeholder identity must never create a second owner library.
        if "@" not in email or any(c.isspace() for c in email):
            raise HTTPException(
                status_code=401, detail="STRATEGY_OWNER_IDENTITY_REQUIRED"
            )
        return f"owner:{email}"
    actor_id = field("id")
    if actor_id is None or not str(actor_id).strip():
        raise HTTPException(status_code=401, detail="STRATEGY_OWNER_IDENTITY_REQUIRED")
    return f"user:{str(actor_id).strip()}"
