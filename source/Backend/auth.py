from datetime import datetime, timedelta
import os
from jose import jwt
from jose.exceptions import JWTError
from passlib.context import CryptContext

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def _jwt_signing_key():
    key = os.getenv("JWT_SECRET_KEY")
    if not key or not key.strip():
        raise RuntimeError("JWT_SIGNING_KEY_UNAVAILABLE")
    return key


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    key = _jwt_signing_key()
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    try:
        return jwt.encode(to_encode, key, algorithm=ALGORITHM)
    except Exception:
        raise JWTError("JWT_SIGNING_FAILED") from None


def verify_access_token(token: str):
    key = _jwt_signing_key()
    try:
        return jwt.decode(token, key, algorithms=[ALGORITHM])
    except Exception:
        raise JWTError("JWT_VERIFICATION_FAILED") from None
