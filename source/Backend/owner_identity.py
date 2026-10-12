"""Inert server-owned identity shared by normal auth and isolated diagnostics."""
import hashlib
from fastapi import HTTPException

LEGACY_OWNER_EMAIL = 'flowsignal.contact@gmail.com'


def owner_session_hash(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def legacy_owner_actor():
    return dict(email=LEGACY_OWNER_EMAIL, role='admin', auth_method='persistent_owner_session')


def canonical_strategy_owner(actor):
    def field(name):
        return actor.get(name) if isinstance(actor, dict) else getattr(actor, name, None)
    role = str(field('role') or '').strip().lower()
    if role == 'admin':
        email = str(field('email') or '').strip().lower()
        if '@' not in email or any(c.isspace() for c in email):
            raise HTTPException(status_code=401, detail='STRATEGY_OWNER_IDENTITY_REQUIRED')
        return f'owner:{email}'
    actor_id = field('id')
    if actor_id is None or not str(actor_id).strip():
        raise HTTPException(status_code=401, detail='STRATEGY_OWNER_IDENTITY_REQUIRED')
    return f'user:{str(actor_id).strip()}'


def legacy_owner_identity():
    actor = legacy_owner_actor()
    owner = canonical_strategy_owner(actor)
    if (actor.get('role') != 'admin' or actor.get('email') != LEGACY_OWNER_EMAIL
        or owner != 'owner:flowsignal.contact@gmail.com'):
        raise ValueError('LEGACY_OWNER_IDENTITY_INVALID')
    return owner
