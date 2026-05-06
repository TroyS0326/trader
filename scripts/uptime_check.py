#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests


@dataclass
class CheckResult:
    path: str
    url: str
    ok: bool
    status_code: int | None = None
    reason: str | None = None


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        pass


def parse_paths(raw_paths: str) -> list[str]:
    parts = [part.strip() for part in raw_paths.split(",")]
    return [part for part in parts if part]


def safe_join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def check_endpoint(base_url: str, path: str, timeout_seconds: float) -> CheckResult:
    url = safe_join(base_url, path)
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        return CheckResult(path=path, url=url, ok=False, reason=f"request_error:{exc.__class__.__name__}")

    if response.status_code != 200:
        return CheckResult(path=path, url=url, ok=False, status_code=response.status_code, reason="non_200")

    if path.strip() in {"/healthz", "/readyz"}:
        try:
            payload = response.json()
        except ValueError:
            return CheckResult(path=path, url=url, ok=False, status_code=response.status_code, reason="invalid_json")
        if isinstance(payload, dict) and payload.get("ok") is True:
            return CheckResult(path=path, url=url, ok=True, status_code=response.status_code)
        return CheckResult(path=path, url=url, ok=False, status_code=response.status_code, reason="ok_false")

    return CheckResult(path=path, url=url, ok=True, status_code=response.status_code)


def send_webhook(webhook_url: str, payload: dict[str, Any], timeout_seconds: float) -> bool:
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout_seconds)
        return 200 <= response.status_code < 300
    except requests.RequestException:
        return False


def run_checks(base_url: str, paths: list[str], timeout_seconds: float) -> tuple[int, list[CheckResult]]:
    results = [check_endpoint(base_url, path, timeout_seconds) for path in paths]
    all_ok = all(result.ok for result in results)
    return (0 if all_ok else 1), results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uptime checks for XeanVI endpoints.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON output")
    parser.add_argument("--once", action="store_true", help="Run one check cycle (default behavior)")
    args = parser.parse_args(argv)

    load_env_file("/etc/xeanvi/xeanvi.env")
    load_env_file(".env")

    base_url = os.getenv("UPTIME_BASE_URL", "https://xeanvi.com").strip()
    paths = parse_paths(os.getenv("UPTIME_CHECK_PATHS", "/healthz,/readyz"))
    timeout_seconds = float(os.getenv("UPTIME_TIMEOUT_SECONDS", "10"))

    exit_code, results = run_checks(base_url, paths, timeout_seconds)

    summary = {
        "ok": exit_code == 0,
        "base_url": base_url,
        "checked_paths": paths,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                "path": result.path,
                "url": result.url,
                "ok": result.ok,
                "status_code": result.status_code,
                "reason": result.reason,
            }
            for result in results
        ],
    }

    if args.as_json:
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    else:
        for result in results:
            if result.ok:
                print(f"[OK] {result.path} ({result.url}) status={result.status_code}")
            else:
                print(
                    f"[FAIL] {result.path} ({result.url}) "
                    f"status={result.status_code if result.status_code is not None else 'n/a'} reason={result.reason}"
                )

    fail_webhook = os.getenv("UPTIME_FAIL_WEBHOOK_URL", "").strip()
    success_webhook = os.getenv("UPTIME_SUCCESS_WEBHOOK_URL", "").strip()

    webhook_payload = {
        "ok": summary["ok"],
        "base_url": base_url,
        "timestamp": summary["timestamp"],
        "results": summary["results"],
    }

    if exit_code == 0 and success_webhook:
        send_webhook(success_webhook, webhook_payload, timeout_seconds)
    if exit_code == 1 and fail_webhook:
        send_webhook(fail_webhook, webhook_payload, timeout_seconds)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
