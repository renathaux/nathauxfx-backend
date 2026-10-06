"""DB-session authentication predicates shared with normal auth; no fallback."""
import hashlib
import hmac
import time
from sqlalchemy import text


def valid_session_user(session,user,now):
    if not session or session['revoked_at'] is not None or float(session['expires_at']) <= now:
        return False
    if not user or not user['is_active']:
        return False
    if str(user['role']) == 'user' and (not bool(user['email_verified']) or str(user.get('approval_status') or 'APPROVED') != 'APPROVED'):
        return False
    return True


class AccessDenied(ValueError):
    def __init__(self,status):
        self.status = status
        super().__init__('ACCESS_DENIED')


def read_admin(connection,token,csrf):
    if not token:
        raise AccessDenied(401)
    session = connection.execute(text('SELECT user_id, csrf_token, expires_at, revoked_at FROM flowsignal_sessions WHERE token_hash=:hash'),
        {'hash':hashlib.sha256(token.encode()).hexdigest()}).mappings().one_or_none()
    user = None if session is None else connection.execute(text('SELECT id,email,role,is_active,email_verified,approval_status FROM flowsignal_users WHERE id=:id'),
        {'id':session['user_id']}).mappings().one_or_none()
    if not valid_session_user(session,user,time.time()):
        raise AccessDenied(401)
    if user['role'] != 'admin' or not csrf or not hmac.compare_digest(csrf,session['csrf_token']):
        raise AccessDenied(403)
    email = str(user['email'] or '').strip().lower()
    if '@' not in email or any(c.isspace() for c in email):
        raise AccessDenied(401)
    return 'owner:'+email
