"""Connection handling for Neon Postgres: env/secrets loading, retry on cold start, credential redaction."""
from __future__ import annotations

import os
import re
import time

from sqlalchemy import create_engine, event, text

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass

_URL = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"]+")


def redact(msg) -> str:
    return _URL.sub("postgresql://***redacted***", str(msg))


def get_secret(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    try:  # Streamlit secrets as an alternative to .env
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


def normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_engine(url: str, schema: str | None = None):
    # prepare_threshold=None: safe behind Neon's pgbouncer pooler; pool is small because serverless instances multiply
    eng = create_engine(normalize_url(url), pool_pre_ping=True, pool_size=2, max_overflow=2, connect_args={"prepare_threshold": None})
    if schema:
        @event.listens_for(eng, "connect")
        def _set_path(dbapi_conn, _):
            with dbapi_conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            dbapi_conn.commit()
    return eng


class DatabaseUnavailable(Exception):
    """Raised with a redacted, friendly message."""


def ensure_awake(engine, attempts: int = 3, on_wait=None) -> None:
    """Neon scales to zero: the first connection after idle can be slow. Retry with backoff."""
    last = None
    for i in range(attempts):
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return
        except Exception as e:  # noqa: BLE001
            last = e
            if on_wait:
                on_wait(i + 1)
            time.sleep(min(2 ** i, 8))
    raise DatabaseUnavailable("Could not reach the database after %d attempts. %s" % (attempts, redact(last)))


def require_test_url() -> str:
    """Tests may only touch TEST_DATABASE_URL, and it must differ from DATABASE_URL."""
    t = os.environ.get("TEST_DATABASE_URL")
    if not t:
        raise RuntimeError("TEST_DATABASE_URL is not set; refusing to run database tests.")
    if t == os.environ.get("DATABASE_URL"):
        raise RuntimeError("TEST_DATABASE_URL equals DATABASE_URL; refusing to run database tests.")
    return t
