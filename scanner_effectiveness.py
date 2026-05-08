from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from db import get_recent_scans
from execution_diagnostics import evaluate_execution_readiness
from models import User
from scan_contract import validate_scan_payload_contract


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _safe_scan_view(scan: Dict[str, Any]) -> Dict[str, Any]:
    contract = validate_scan_payload_contract(scan if isinstance(scan, dict) else {})
    best = (scan.get("best_pick") or scan.get("best") or scan.get("top_pick") or {}) if isinstance(scan, dict) else {}
    normalized = contract.get("normalized_order_fields") or {}
    return {
        "source": scan.get("_source"),
        "db_scan_id": scan.get("db_scan_id"),
        "scan_id": scan.get("scan_id") or scan.get("id"),
        "user_id": scan.get("user_id"),
        "created_at": scan.get("created_at") or scan.get("timestamp"),
        "symbol": normalized.get("symbol") or str(best.get("symbol") or "").upper().strip() or None,
        "decision": contract.get("decision") or None,
        "setup_grade": best.get("setup_grade"),
        "qty": normalized.get("qty"),
        "entry_price": normalized.get("entry_price"),
        "stop_price": normalized.get("stop_price"),
        "target_1": normalized.get("target_1"),
        "target_2": normalized.get("target_2"),
        "missing_order_fields": contract.get("missing_order_fields") or [],
        "payload_shape_notes": contract.get("payload_shape_notes") or [],
        "score_total": best.get("score_total"),
        "min_score_to_execute": (best.get("details") or {}).get("min_score_to_execute"),
        "skip_reason": (best.get("details") or {}).get("skip_reason"),
        "skip_reasons": (best.get("details") or {}).get("skip_reasons") or [],
        "decision_reason": (best.get("details") or {}).get("decision_reason"),
        "setup_grade_reason": (best.get("details") or {}).get("setup_grade_reason"),
        "execution_eligibility_reason": (best.get("details") or {}).get("execution_eligibility_reason"),
        "buy_window_open": best.get("buy_window_open"),
        "opening_range_complete": best.get("opening_range_complete"),
        "breakout_confirmed": best.get("breakout_confirmed"),
        "component_scores": best.get("scores") or {},
        "blocked_reason_codes": [],
    }


def normalize_scan_record(record: dict) -> dict:
    row = dict(record or {})
    payload: dict[str, Any] = {}
    notes: list[str] = []
    raw_payload = row.get("payload_json")
    if isinstance(raw_payload, str) and raw_payload.strip():
        try:
            parsed = json.loads(raw_payload)
            if isinstance(parsed, dict):
                payload = dict(parsed)
            else:
                notes.append("PAYLOAD_JSON_MISSING_OR_INVALID")
        except Exception:
            notes.append("PAYLOAD_JSON_MISSING_OR_INVALID")
    else:
        notes.append("PAYLOAD_JSON_MISSING_OR_INVALID")

    db_scan_id = row.get("id")
    payload["db_scan_id"] = db_scan_id
    payload["scan_id"] = payload.get("scan_id") or db_scan_id
    payload["best_symbol_db"] = row.get("best_symbol")
    payload["best_decision_db"] = row.get("best_decision")
    if not payload.get("created_at") and not payload.get("timestamp") and row.get("created_at"):
        payload["created_at"] = row.get("created_at")

    payload_user_id = payload.get("user_id") or payload.get("report_user_id")
    payload["user_id"] = int(payload_user_id) if str(payload_user_id or "").isdigit() else payload_user_id

    if notes and not payload.get("best_pick") and row.get("best_symbol"):
        payload["best_pick"] = {
            "symbol": str(row.get("best_symbol") or "").upper().strip() or None,
            "decision": row.get("best_decision"),
        }
    if notes:
        payload["payload_shape_notes"] = sorted(set((payload.get("payload_shape_notes") or []) + notes))
    return payload


def _load_scans(user: Optional[Any], limit: int) -> tuple[list[dict], dict[int, dict]]:
    scans = list(get_recent_scans(limit=max(limit * 3, limit)) or [])
    normalized_scans = [normalize_scan_record(scan) for scan in scans]
    from app import redis_client
    scans_by_user: dict[int, dict] = {}

    if user is not None:
        user_ids = [int(user.id)]
    else:
        user_ids = sorted({int(s.get("user_id") or 0) for s in normalized_scans if int(s.get("user_id") or 0) > 0})

    for uid in user_ids:
        try:
            raw = redis_client.get(f"latest_scan:{uid}")
            if raw:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    payload.setdefault("user_id", uid)
                    payload.setdefault("scan_id", payload.get("scan_id"))
                    payload["_source"] = "redis_latest"
                    scans_by_user[uid] = payload
        except Exception:
            continue

    filtered: list[dict] = []
    for normalized in normalized_scans:
        uid = int(normalized.get("user_id") or 0)
        if user is not None and uid != int(user.id):
            continue
        if user is not None and uid <= 0:
            continue
        normalized["_source"] = "db_recent"
        filtered.append(normalized)
    filtered.sort(key=lambda x: int(x.get("db_scan_id") or x.get("scan_id") or 0), reverse=True)
    return filtered[:limit], scans_by_user


def build_scanner_effectiveness_report(user: Optional[Any] = None, limit: int = 50) -> Dict[str, Any]:
    scans, latest_by_user = _load_scans(user=user, limit=limit)
    all_scans = list(scans)
    seen: set[tuple[Any, Any]] = set()
    for s in all_scans:
        seen.add((s.get("scan_id") or s.get("db_scan_id"), s.get("user_id")))
    for v in latest_by_user.values():
        key = (v.get("scan_id") or v.get("db_scan_id"), v.get("user_id"))
        if key in seen:
            continue
        all_scans.append(v)
        seen.add(key)

    decision_counts: Counter = Counter()
    missing_order_field_counts: Counter = Counter()
    blocked_reason_counts: Counter = Counter()
    scan_contract_failure_counts: Counter = Counter()
    symbol_counts: Counter = Counter()
    executable_symbols: Counter = Counter()
    non_exec_symbols: Counter = Counter()
    qty_invalid_count = 0
    qty_below_one_count = 0
    best_pick_present_count = 0
    executable_payload_ready_count = 0
    scans_by_user_count: dict[int, int] = defaultdict(int)
    source_counts: Counter = Counter()
    skip_reason_counts: Counter = Counter()
    setup_grade_counts: Counter = Counter()
    score_total_buckets: Counter = Counter()
    component_score_totals: defaultdict[str, float] = defaultdict(float)
    component_score_counts: Counter = Counter()
    failures: list[dict] = []
    executable_samples: list[dict] = []
    rejection_reasons: Counter = Counter()

    attributed_scan_count = 0
    unattributed_scan_count = 0

    for scan in all_scans:
        uid = int(scan.get("user_id") or 0)
        source_counts[str(scan.get("_source") or "unknown")] += 1
        if uid:
            attributed_scan_count += 1
            scans_by_user_count[uid] += 1
        else:
            unattributed_scan_count += 1
        contract = validate_scan_payload_contract(scan if isinstance(scan, dict) else {})
        decision = contract.get("decision") or "blank/missing"
        decision_counts[decision] += 1

        if contract.get("has_best_pick"):
            best_pick_present_count += 1
        if contract.get("executable_payload_ready"):
            executable_payload_ready_count += 1

        symbol = (contract.get("normalized_order_fields") or {}).get("symbol") or "UNKNOWN"
        symbol_counts[symbol] += 1
        best_pick = scan.get("best_pick") if isinstance(scan.get("best_pick"), dict) else {}
        setup_grade = str(best_pick.get("setup_grade") or "").strip() or "UNKNOWN"
        setup_grade_counts[setup_grade] += 1
        score_total = best_pick.get("score_total")
        if isinstance(score_total, (int, float)):
            bucket_floor = int(score_total // 10) * 10
            score_total_buckets[f"{bucket_floor:02d}-{bucket_floor + 9:02d}"] += 1
        details = best_pick.get("details") if isinstance(best_pick.get("details"), dict) else {}
        for reason in details.get("skip_reasons") or []:
            skip_reason_counts[str(reason)] += 1
        if details.get("skip_reason"):
            skip_reason_counts[str(details.get("skip_reason"))] += 1
        component_scores = best_pick.get("scores") if isinstance(best_pick.get("scores"), dict) else {}
        for key, value in component_scores.items():
            if isinstance(value, (int, float)):
                component_score_totals[str(key)] += float(value)
                component_score_counts[str(key)] += 1

        if not contract.get("qty_valid"):
            qty_invalid_count += 1
        qty = (contract.get("normalized_order_fields") or {}).get("qty")
        if qty is not None and qty < 1:
            qty_below_one_count += 1

        for f in contract.get("missing_order_fields") or []:
            missing_order_field_counts[f] += 1
        if not contract.get("has_best_pick"):
            scan_contract_failure_counts["NO_BEST_PICK"] += 1
        if not contract.get("decision_is_executable"):
            scan_contract_failure_counts["DECISION_NOT_EXECUTABLE"] += 1
        if contract.get("missing_order_fields"):
            scan_contract_failure_counts["MISSING_ORDER_FIELDS"] += 1
        if not contract.get("qty_valid"):
            scan_contract_failure_counts["QTY_INVALID_OR_BELOW_1"] += 1

        for candidate in (scan.get("watchlist") or []):
            if isinstance(candidate, dict):
                for reason in (candidate.get("rejection_reasons") or candidate.get("failed_filters") or []):
                    rejection_reasons[str(reason)] += 1

        diag = None
        effective_user = user if user is not None else (User.query.get(uid) if uid else None)
        if effective_user:
            diag = evaluate_execution_readiness(effective_user, scan)
            for reason in diag.get("active_mode_blocked_reasons", []):
                code = reason.get("code")
                if code:
                    blocked_reason_counts[code] += 1
        else:
            if uid <= 0:
                blocked_reason_counts["USER_CONTEXT_MISSING"] += 1

        safe_view = _safe_scan_view(scan)
        safe_view["active_mode"] = (diag or {}).get("active_mode")
        safe_view["blocked_reason_codes"] = [r.get("code") for r in (diag or {}).get("active_mode_blocked_reasons", []) if r.get("code")]

        if contract.get("executable_payload_ready"):
            executable_symbols[safe_view.get("symbol") or "UNKNOWN"] += 1
            if len(executable_samples) < 10:
                executable_samples.append({k: safe_view[k] for k in ["scan_id", "user_id", "symbol", "decision", "qty", "entry_price", "stop_price", "target_1", "target_2"]})
        else:
            non_exec_symbols[safe_view.get("symbol") or "UNKNOWN"] += 1
            if len(failures) < 10:
                failures.append(safe_view)

    latest_age = None
    if all_scans:
        dts = []
        for payload in all_scans:
            dt = _parse_dt(payload.get("created_at") or payload.get("timestamp"))
            if dt:
                dts.append(dt)
        if dts:
            latest = max(dts)
            latest_age = int((datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds())

    dominant_symbol = None
    dominant_symbol_pct = 0.0
    same_symbol_count = 0
    repeated_best_pick_warning = False
    latest_dominant_symbol_decisions: list[str] = []
    latest_dominant_symbol_skip_reasons: list[str] = []
    if symbol_counts:
        dominant_symbol, same_symbol_count = symbol_counts.most_common(1)[0]
        if all_scans:
            dominant_symbol_pct = round((same_symbol_count / len(all_scans)) * 100, 2)
        repeated_best_pick_warning = dominant_symbol_pct > 80.0
        for scan in all_scans:
            safe = _safe_scan_view(scan)
            if safe.get("symbol") == dominant_symbol:
                if safe.get("decision"):
                    latest_dominant_symbol_decisions.append(safe["decision"])
                for reason in safe.get("skip_reasons") or []:
                    latest_dominant_symbol_skip_reasons.append(str(reason))
                if len(latest_dominant_symbol_decisions) >= 10 and len(latest_dominant_symbol_skip_reasons) >= 20:
                    break

    component_score_averages = {
        key: round(component_score_totals[key] / component_score_counts[key], 3)
        for key in sorted(component_score_totals.keys())
        if component_score_counts.get(key)
    }

    return {
        "total_scans_analyzed": len(all_scans),
        "scans_by_user_count": dict(scans_by_user_count),
        "source_counts": dict(source_counts),
        "latest_scan_age_seconds": latest_age,
        "best_pick_present_count": best_pick_present_count,
        "executable_payload_ready_count": executable_payload_ready_count,
        "decision_counts": dict(decision_counts),
        "missing_order_field_counts": dict(missing_order_field_counts),
        "qty_invalid_count": qty_invalid_count,
        "qty_below_one_count": qty_below_one_count,
        "symbol_counts": dict(symbol_counts),
        "skip_reason_counts": dict(skip_reason_counts),
        "setup_grade_counts": dict(setup_grade_counts),
        "score_total_buckets": dict(score_total_buckets),
        "component_score_averages": component_score_averages,
        "top_non_executable_symbols": non_exec_symbols.most_common(10),
        "top_executable_symbols": executable_symbols.most_common(10),
        "blocked_reason_counts": dict(blocked_reason_counts),
        "user_context_missing_count": int(blocked_reason_counts.get("USER_CONTEXT_MISSING", 0)),
        "attributed_scan_count": attributed_scan_count,
        "unattributed_scan_count": unattributed_scan_count,
        "dominant_symbol_warning": repeated_best_pick_warning,
        "repeated_best_pick_warning": repeated_best_pick_warning,
        "dominant_symbol": dominant_symbol,
        "dominant_symbol_pct": dominant_symbol_pct,
        "same_symbol_count": same_symbol_count,
        "latest_dominant_symbol_decisions": latest_dominant_symbol_decisions[:10],
        "latest_dominant_symbol_skip_reasons": latest_dominant_symbol_skip_reasons[:20],
        "scan_contract_failure_counts": dict(scan_contract_failure_counts),
        "top_rejection_reasons": dict(rejection_reasons.most_common(20)),
        "sample_recent_failures": failures,
        "sample_recent_executable_payloads": executable_samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args()
    from app import app
    with app.app_context():
        user = User.query.get(args.user_id) if args.user_id else None
        report = build_scanner_effectiveness_report(user=user, limit=max(1, args.limit))
        print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
