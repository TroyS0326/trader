import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)
_FILTERED = "[Filtered]"
_SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|access_token|refresh_token|authorization|cookie|set-cookie|api[_-]?key|"
    r"alpaca|stripe|brevo|gemini|database_url|dsn|session|csrf|client_secret",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)|"
    r"(Basic\s+[A-Za-z0-9+/=]{8,})|"
    r"(sk_(live|test)_[A-Za-z0-9]+)|"
    r"(pk_(live|test)_[A-Za-z0-9]+)|"
    r"(AKIA[0-9A-Z]{16})|"
    r"(postgres(?:ql(?:\+psycopg)?)?://[^:\s]+:[^@\s]+@[^/\s]+/[^\s]+)|"
    r"((?:access_token|refresh_token|password|api[_-]?key|authorization)\s*=\s*[^&\s]+)|"
    r"(https?://[a-z0-9]+@[a-z0-9.-]*sentry\.io/\d+)|"
    r"(xapp-[A-Za-z0-9_-]{12,})|"
    r"(sbp_[A-Za-z0-9]{12,})|"
    r"(AG[A-Za-z0-9_-]{12,})",
    re.IGNORECASE,
)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None and str(value).strip() != "" else default
    except (TypeError, ValueError):
        return default


def _should_filter_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(str(key or "")))


def _scrub_value(value: Any, key_hint: str | None = None) -> Any:
    if key_hint and _should_filter_key(key_hint):
        return _FILTERED

    if isinstance(value, dict):
        return {k: _scrub_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, key_hint) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, key_hint) for item in value)
    if isinstance(value, str):
        if _should_filter_key(value):
            return _FILTERED
        if _SENSITIVE_VALUE_PATTERN.search(value):
            return _FILTERED
    return value


def before_send(event: dict[str, Any], hint: dict[str, Any] | None) -> dict[str, Any]:
    for key in ("message", "transaction", "culprit", "logger", "event_id"):
        if key in event:
            event[key] = _scrub_value(event.get(key), key)

    request = event.get("request") or {}
    if request:
        request["headers"] = _scrub_value(request.get("headers") or {})
        request["cookies"] = _scrub_value(request.get("cookies") or {})
        request["data"] = _scrub_value(request.get("data"))
        request["query_string"] = _scrub_value(request.get("query_string"))
        event["request"] = request

    if "extra" in event:
        event["extra"] = _scrub_value(event.get("extra") or {})
    if "contexts" in event:
        event["contexts"] = _scrub_value(event.get("contexts") or {})
    if "tags" in event:
        event["tags"] = _scrub_value(event.get("tags") or {})
    if "user" in event:
        event["user"] = _scrub_value(event.get("user") or {})
    if "logentry" in event:
        event["logentry"] = _scrub_value(event.get("logentry") or {})

    breadcrumbs = event.get("breadcrumbs") or {}
    if isinstance(breadcrumbs, dict):
        breadcrumbs["values"] = _scrub_value(breadcrumbs.get("values") or [])
        event["breadcrumbs"] = breadcrumbs
    elif isinstance(breadcrumbs, list):
        event["breadcrumbs"] = _scrub_value(breadcrumbs)

    exc_values = (((event.get("exception") or {}).get("values") or []))
    for exc in exc_values:
        if isinstance(exc, dict):
            value = exc.get("value")
            if isinstance(value, str) and (_SENSITIVE_KEY_PATTERN.search(value) or _SENSITIVE_VALUE_PATTERN.search(value)):
                exc["value"] = _FILTERED
    return event


def init_sentry(service_name: str = "xeanvi-web") -> bool:
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        integrations = [
            FlaskIntegration(),
            LoggingIntegration(level=logging.ERROR, event_level=logging.ERROR),
        ]

        try:
            from sentry_sdk.integrations.celery import CeleryIntegration
            integrations.append(CeleryIntegration())
        except Exception:
            logger.info("CeleryIntegration unavailable; continuing with Flask/logging integrations.")

        environment = (os.getenv("SENTRY_ENVIRONMENT") or os.getenv("FLASK_ENV") or "production").strip()
        release = (os.getenv("SENTRY_RELEASE") or "").strip() or None
        traces_sample_rate = _parse_float(os.getenv("SENTRY_TRACES_SAMPLE_RATE"), 0.0)
        profiles_sample_rate = _parse_float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE"), 0.0)
        send_default_pii = _parse_bool(os.getenv("SENTRY_SEND_DEFAULT_PII"), False)

        sentry_sdk.init(
            dsn=dsn,
            integrations=integrations,
            environment=environment,
            release=release,
            traces_sample_rate=traces_sample_rate,
            profiles_sample_rate=profiles_sample_rate,
            send_default_pii=send_default_pii,
            before_send=before_send,
            server_name=service_name,
        )
        return True
    except Exception as exc:
        safe_exc = _scrub_value(str(exc), "error")
        logger.warning("Sentry initialization failed; continuing without Sentry. Reason: %s", safe_exc)
        return False
