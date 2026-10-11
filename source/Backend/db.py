import os
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
except ImportError:  # Environment variables remain authoritative in production.
    def load_dotenv(*_args, **_kwargs):
        return False
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from paths import BASE_DIR, DATABASE_DIR


# Server environment is supplied before importing database definitions. Mutable
# dotenv files are not read by application/model imports.


def normalize_database_url(value=None):
    url = str(
        value
        or os.getenv("DATABASE_URL")
        or f"sqlite:///{DATABASE_DIR / 'flowsignal.db'}"
    ).strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def redact_database_url(value):
    """Return a log-safe URL without leaking Neon credentials."""
    url = normalize_database_url(value)
    if url.startswith("sqlite"):
        return url
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    username = f"{parsed.username}:***@" if parsed.username else ""
    return urlunsplit((parsed.scheme, f"{username}{hostname}{port}", parsed.path, "", ""))


DATABASE_URL = normalize_database_url()

engine_kwargs = {
    "pool_pre_ping": True,
}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Neon may suspend an idle compute. Pre-ping and short-lived pooled
    # connections recover cleanly without recycling stale server sockets.
    engine_kwargs.update({
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE_SECONDS", "300")),
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "5")),
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "30")),
        "connect_args": {
            "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10")),
            "application_name": "flowsignal-render",
        },
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def database_status():
    return {
        "backend": engine.url.get_backend_name(),
        "driver": engine.url.get_driver_name(),
        "configured_by_database_url": bool(os.getenv("DATABASE_URL")),
        "url": redact_database_url(DATABASE_URL),
        "durable": engine.url.get_backend_name() == "postgresql",
    }


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
