"""إدارة مجمع اتصالات PostgreSQL المركزي لمشروع مفرغ الوقاد."""

import atexit
import logging
import os
from contextlib import contextmanager
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("db")

POOL_MIN_SIZE = int(os.environ.get("DATABASE_POOL_MIN", "1"))
POOL_MAX_SIZE = int(os.environ.get("DATABASE_POOL_MAX", "10"))
POOL_TIMEOUT = float(os.environ.get("DATABASE_POOL_TIMEOUT", "30.0"))

_POOLS: dict[str, ConnectionPool] = {}


def get_default_database_url() -> str:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("WARRAQ_DATABASE_URL")
        or ""
    ).strip()


def get_pool(conninfo: Optional[str] = None) -> ConnectionPool:
    """استرجاع مجمع الاتصالات المناسب لعنوان قاعدة البيانات المحدد."""
    global _POOLS
    url = (conninfo or get_default_database_url()).strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")

    if url not in _POOLS:
        _POOLS[url] = ConnectionPool(
            conninfo=url,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            timeout=POOL_TIMEOUT,
            open=True,
            kwargs={"row_factory": dict_row},
        )
        log.info(
            "Created central PostgreSQL connection pool (min=%s, max=%s)",
            POOL_MIN_SIZE,
            POOL_MAX_SIZE,
        )

    return _POOLS[url]


@contextmanager
def get_db_connection(conninfo: Optional[str] = None):
    """استعارة اتصال من مجمع الاتصالات المركزي وإرجاعه تلقائياً عند انتهاء السياق."""
    pool = get_pool(conninfo)
    with pool.connection() as conn:
        yield conn


def close_all_pools():
    """إغلاق جميع مجمعات الاتصالات المفتوحة عند إنهاء التطبيق."""
    global _POOLS
    for url, pool in list(_POOLS.items()):
        try:
            pool.close()
        except Exception:
            pass
    _POOLS.clear()


atexit.register(close_all_pools)
