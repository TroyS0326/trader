from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis

import config

logger = logging.getLogger(__name__)

MARKET_STATE_PREFIX = "market_state:"
DATA_FRESHNESS_PREFIX = "data_freshness:"
DYNAMIC_ORB_STATE_NAME = "dynamic_orb_state"
DYNAMIC_ORB_KEY = "market_state:dynamic_orb_state"

_redis_client: redis.Redis | None = None
_redis_init_failed = False


def _get_redis_client() -> redis.Redis | None:
    global _redis_client, _redis_init_failed
    if _redis_client is not None:
        return _redis_client
    if _redis_init_failed:
        return None
    try:
        _redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        return _redis_client
    except Exception as exc:
        _redis_init_failed = True
        logger.warning("Redis initialization failed in market_state: %s", exc)
        return None


def now_utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def set_json(key: str, payload: Any, ttl_seconds: int | None = None) -> bool:
    client = _get_redis_client()
    if client is None:
        return False
    try:
        raw = json.dumps(payload)
    except Exception as exc:
        logger.warning("JSON serialization failed for key %s: %s", key, exc)
        return False
    try:
        if ttl_seconds is not None:
            client.setex(key, int(ttl_seconds), raw)
        else:
            client.set(key, raw)
        return True
    except Exception as exc:
        logger.warning("Redis write failed for key %s: %s", key, exc)
        return False


def get_json(key: str, default: Any = None) -> Any:
    client = _get_redis_client()
    if client is None:
        return default
    try:
        raw = client.get(key)
        if raw is None:
            return default
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis read/JSON decode failed for key %s: %s", key, exc)
        return default


def set_market_state(name: str, payload: Any, ttl_seconds: int = 43200) -> bool:
    key = f"{MARKET_STATE_PREFIX}{name}"
    return set_json(key, payload, ttl_seconds=ttl_seconds)


def get_market_state(name: str, default: Any = None) -> Any:
    key = f"{MARKET_STATE_PREFIX}{name}"
    return get_json(key, default=default)


def set_data_freshness(source_name: str) -> bool:
    key = f"{DATA_FRESHNESS_PREFIX}{source_name}"
    payload = {"ts": now_utc_ts()}
    return set_json(key, payload)


def get_data_age_seconds(source_name: str) -> float | None:
    key = f"{DATA_FRESHNESS_PREFIX}{source_name}"
    payload = get_json(key, default=None)
    if not isinstance(payload, dict):
        return None
    ts = payload.get("ts")
    try:
        return max(0.0, now_utc_ts() - float(ts))
    except Exception:
        return None


def is_data_fresh(source_name: str, max_age_seconds: float | None = None) -> bool:
    if max_age_seconds is None:
        max_age_seconds = config.DATA_FRESHNESS_MAX_AGE_SECONDS
    age = get_data_age_seconds(source_name)
    if age is None:
        return False
    try:
        return age <= float(max_age_seconds)
    except Exception:
        return False
