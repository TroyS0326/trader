from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from db import get_recent_scans
from execution_diagnostics import evaluate_execution_readiness
from models import User, WatchCandidate
from scan_contract import validate_scan_payload_contract
from scanner import normalize_skip_reason_code


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

    details = best.get("details") or {}
    explicit_codes = details.get("skip_reason_codes") or []
    skip_reason_values = list(details.get("skip_reasons") or [])
    single_skip_reason = str(details.get("skip_reason") or "").strip()
    if single_skip_reason:
        skip_reason_values.append(single_skip_reason)
    synthesized_codes: list[str] = []
    for reason in skip_reason_values:
        code = normalize_skip_reason_code(reason)
        if code and code not in synthesized_codes:
            synthesized_codes.append(code)
    deduped_codes: list[str] = []
    for code in explicit_codes or synthesized_codes:
        text = str(code or "").strip()
        if text and text not in deduped_codes:
            deduped_codes.append(text)

    safe_scan_diag = {}
    scan_diag = scan.get("scan_diagnostics") if isinstance(scan.get("scan_diagnostics"), dict) else {}
    for key in [
        "candidate_count_raw", "candidate_count_after_dedupe", "candidate_count_after_user_filters",
        "candidate_count_after_price_volume_filters", "top_5_candidates_by_score", "best_pick_selection_reason",
        "executable_candidate_count", "watch_candidate_count", "skip_candidate_count",
        "best_executable_candidate_symbol", "best_watch_candidate_symbol", "best_skip_candidate_symbol",
    ]:
        if key in scan_diag:
            safe_scan_diag[key] = scan_diag.get(key)

    watch_diag = _watch_snapshot(user=user)
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
        "skip_reason": details.get("skip_reason"),
        "skip_reasons": details.get("skip_reasons") or [],
        "decision_reason": details.get("decision_reason"),
        "setup_grade_reason": details.get("setup_grade_reason"),
        "execution_eligibility_reason": details.get("execution_eligibility_reason"),
        "buy_window_open": best.get("buy_window_open"),
        "opening_range_complete": best.get("opening_range_complete"),
        "breakout_confirmed": best.get("breakout_confirmed"),
        "component_scores": best.get("scores") or {},
        "blocked_reason_codes": [],
        "skip_reason_codes": deduped_codes,
        "scanner_now_et": details.get("scanner_now_et"),
        "feed_used": details.get("feed_used") or details.get("data_feed_used"),
        "intraday_bar_count": details.get("intraday_bar_count"),
        "today_session_bar_count": details.get("today_session_bar_count"),
        "opening_range_bar_count": details.get("opening_range_bar_count"),
        "latest_bar_timestamp_et": details.get("latest_bar_timestamp_et"),
        "earliest_today_bar_timestamp_et": details.get("earliest_today_bar_timestamp_et"),
        "opening_range_start_et": details.get("opening_range_start_et"),
        "opening_range_end_et": details.get("opening_range_end_et"),
        "opening_range_complete_reason": details.get("opening_range_complete_reason"),
        "or_high": details.get("or_high"),
        "or_low": details.get("or_low"),
        "current_price": details.get("current_price") if details.get("current_price") is not None else best.get("current_price"),
        "breakout_threshold_price": details.get("breakout_threshold_price"),
        "breakout_confirmed_reason": details.get("breakout_confirmed_reason"),
        "bars_above_breakout": details.get("bars_above_breakout"),
        "scan_diagnostics": safe_scan_diag,
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




def _watch_snapshot(user: Optional[Any] = None) -> Dict[str, Any]:
    q = WatchCandidate.query
    if user is not None:
        q = q.filter_by(user_id=int(user.id))
    active = q.filter_by(status='ACTIVE').all()
    today = datetime.now(timezone.utc).date()
    promoted_today = q.filter(WatchCandidate.status=='PROMOTED').all()
    expired_today = q.filter(WatchCandidate.status=='EXPIRED').all()
    blockers = Counter()
    latest = []
    for row in active[:10]:
        codes = []
        try:
            codes = json.loads(row.latest_skip_reason_codes_json or '[]')
        except Exception:
            pass
        for c in codes: blockers[str(c)] += 1
        latest.append({'symbol': row.symbol, 'missing_buy_confirmations': json.loads(row.missing_buy_confirmations_json or '[]') if row.missing_buy_confirmations_json else [], 'latest_score_total': row.latest_score_total, 'status': row.status})
    best = sorted(active, key=lambda r: int(r.latest_score_total or 0), reverse=True)[0] if active else None
    return {
        'active_watch_candidate_count': len(active),
        'latest_watch_candidates': latest,
        'watch_promoted_count_today': len([r for r in promoted_today if getattr(r,'promoted_at',None) and r.promoted_at.date()==today]),
        'watch_expired_count_today': len([r for r in expired_today if getattr(r,'last_seen_at',None) and r.last_seen_at.date()==today]),
        'watch_top_blockers': blockers.most_common(10),
        'best_active_watch_symbol': best.symbol if best else None,
        'best_active_watch_missing_confirmations': json.loads(best.missing_buy_confirmations_json or '[]') if best and best.missing_buy_confirmations_json else [],
        'latest_watch_recheck_summary': None,
    }
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
    skip_reason_code_counts: Counter = Counter()
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
        deduped_skip_reasons: list[str] = []
        for reason in details.get("skip_reasons") or []:
            reason_text = str(reason)
            if reason_text and reason_text not in deduped_skip_reasons:
                deduped_skip_reasons.append(reason_text)
        single_skip_reason = str(details.get("skip_reason") or "").strip()
        if single_skip_reason and single_skip_reason not in deduped_skip_reasons:
            deduped_skip_reasons.append(single_skip_reason)
        deduped_codes=[]
        for reason in deduped_skip_reasons:
            skip_reason_counts[reason] += 1
            code = normalize_skip_reason_code(reason)
            if code not in deduped_codes:
                deduped_codes.append(code)
                skip_reason_code_counts[code] += 1
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
    latest_dominant_symbol_skip_reason_codes: list[str] = []
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
                    latest_dominant_symbol_skip_reason_codes.append(normalize_skip_reason_code(str(reason)))
                if len(latest_dominant_symbol_decisions) >= 10 and len(latest_dominant_symbol_skip_reasons) >= 20:
                    break

    component_score_averages = {
        key: round(component_score_totals[key] / component_score_counts[key], 3)
        for key in sorted(component_score_totals.keys())
        if component_score_counts.get(key)
    }
    latest_attr_age = None
    latest_unattr_age = None
    now_utc = datetime.now(timezone.utc)
    for payload in all_scans:
        dt = _parse_dt(payload.get("created_at") or payload.get("timestamp"))
        if not dt:
            continue
        age = int((now_utc - dt.astimezone(timezone.utc)).total_seconds())
        if int(payload.get("user_id") or 0) > 0:
            latest_attr_age = age if latest_attr_age is None else min(latest_attr_age, age)
        else:
            latest_unattr_age = age if latest_unattr_age is None else min(latest_unattr_age, age)

    latest_scan = all_scans[0] if all_scans else {}
    latest_diag = latest_scan.get("scan_diagnostics") if isinstance(latest_scan.get("scan_diagnostics"), dict) else {}
    latest_attr_version = latest_scan.get("scan_attribution_version")
    attr_version_counts: Counter = Counter(str(scan.get("scan_attribution_version")) for scan in all_scans)
    has_new_diag = False
    missing_diag_reason = "NO_SCANS"
    if all_scans:
        if latest_attr_version is None:
            missing_diag_reason = "SCAN_ATTRIBUTION_VERSION_MISSING"
        elif int(latest_attr_version) < 1:
            missing_diag_reason = "OLD_SCAN_PAYLOAD"
        elif not latest_diag:
            missing_diag_reason = "SCAN_DIAGNOSTICS_MISSING"
        else:
            has_new_diag = True
            missing_diag_reason = None

    starvation_flags = []
    if repeated_best_pick_warning:
        starvation_flags.append("DOMINANT_SYMBOL_STUCK")
    if executable_payload_ready_count == 0:
        starvation_flags.append("NO_EXECUTABLE_CANDIDATES")
    if unattributed_scan_count > 0 and (latest_unattr_age is not None and latest_unattr_age < 7200):
        starvation_flags.append("UNATTRIBUTED_RECENT_SCANS")

    latest_candidate_count_after_dedupe = latest_diag.get("candidate_count_after_dedupe")
    missing_daily = latest_diag.get("missing_daily_bars_symbols") or []
    missing_minute = latest_diag.get("missing_minute_bars_symbols") or []
    bars_requested = int(latest_diag.get("bar_data_requested_symbols_count") or 0)
    asset_filter_rejections = latest_diag.get("asset_filter_rejection_counts") or {}
    unsupported_assets = sum(int(v or 0) for k, v in asset_filter_rejections.items() if k in {"UNSUPPORTED_ASSET_TYPE", "WARRANT_OR_RIGHT", "ETF_BLOCKED_BY_SETTINGS", "LEVERAGED_ETF_BLOCKED_BY_SETTINGS", "NOT_TRADABLE", "MISSING_ASSET_METADATA"})
    data_starvation_summary = {
        "bars_requested": bars_requested,
        "missing_daily_count": len(missing_daily),
        "missing_minute_count": len(missing_minute),
        "missing_any_bar_count": len(set(missing_daily + missing_minute)),
    }
    recommended_next_action = "Rerun scanner_effectiveness and inspect scan_diagnostics/opening_range diagnostics for dominant symbols."
    if not has_new_diag:
        recommended_next_action = "Run a fresh manual scan or wait for the next central scan after deploying latest code, then rerun scanner_effectiveness."
    elif latest_diag.get("executable_candidate_count", 0) > 0 and (latest_scan.get("best_pick") or {}).get("decision") == "SKIP":
        recommended_next_action = "Investigate best-pick ranking bug."
    elif repeated_best_pick_warning and isinstance(latest_candidate_count_after_dedupe, int) and latest_candidate_count_after_dedupe <= 3:
        recommended_next_action = "Investigate candidate universe breadth."
    elif repeated_best_pick_warning and isinstance(latest_candidate_count_after_dedupe, int) and latest_candidate_count_after_dedupe > 5:
        recommended_next_action = "Investigate ranking logic and top candidate quality."
    elif any((s.get("opening_range_complete") is False and s.get("opening_range_bar_count") == 0 and isinstance(s.get("latest_bar_timestamp_et"), str) and isinstance(s.get("opening_range_end_et"), str)) for s in failures):
        recommended_next_action = "Investigate Alpaca intraday bars/feed/window filtering for missing opening-range bars."

    primary_blocker_summary = starvation_flags[0] if starvation_flags else "NONE"
    if bars_requested > 0 and len(set(missing_daily + missing_minute)) >= max(3, int(bars_requested * 0.5)):
        primary_blocker_summary = "BAR_DATA_STARVATION"
        recommended_next_action = "Investigate Alpaca bar data coverage/feed or per-symbol bar fetch failures."
    elif unsupported_assets >= max(3, int((bars_requested or 1) * 0.5)):
        primary_blocker_summary = "CANDIDATE_SOURCE_LOW_QUALITY"
        recommended_next_action = "Improve candidate source filtering before analysis."

    catalyst_summary = latest_diag.get('latest_catalyst_score_summary') if isinstance(latest_diag, dict) else {}
    vwap_summary = latest_diag.get('latest_vwap_alignment_summary') if isinstance(latest_diag, dict) else {}
    liq_summary = latest_diag.get('latest_liquidity_failure_summary') if isinstance(latest_diag, dict) else {}
    source_quality_summary = latest_diag.get('latest_candidate_source_quality_summary') if isinstance(latest_diag, dict) else {}
    catalyst_baseline_reason_counts = latest_diag.get('latest_catalyst_baseline_reason_counts') if isinstance(latest_diag, dict) else {}
    catalyst_missing_reason_counts = latest_diag.get('latest_catalyst_missing_reason_counts') if isinstance(latest_diag, dict) else {}
    latest_source_quality_warning = None
    avg_catalyst = float((catalyst_summary or {}).get('average_catalyst_score') or 0)
    checked = int((catalyst_summary or {}).get('symbols_checked') or 0)
    not_aligned = int((vwap_summary or {}).get('not_aligned_count') or 0)
    low_pmv = int((liq_summary or {}).get('low_premarket_dollar_volume_count') or 0) + int((liq_summary or {}).get('unavailable_premarket_volume_count') or 0)
    if checked > 0 and avg_catalyst <= 2.0:
        recommended_next_action = 'Investigate catalyst/news sourcing or scoring before changing trading thresholds.'
    elif checked > 0 and low_pmv >= max(1, int(checked * 0.6)):
        recommended_next_action = 'Candidate universe is producing weak liquidity; improve candidate source quality.'
    elif checked > 0 and not_aligned >= max(1, int(checked * 0.6)):
        recommended_next_action = 'Scanner is finding movers but not confirmed trend setups; wait for confirmed setups or improve source ranking.'
    if isinstance(source_quality_summary, dict) and source_quality_summary:
        source_with_max_analyzed = max(source_quality_summary.items(), key=lambda kv: int((kv[1] or {}).get('analyzed_count') or 0))
        src, meta = source_with_max_analyzed
        analyzed_count = int((meta or {}).get('analyzed_count') or 0)
        skip_count = int((meta or {}).get('skip_count') or 0)
        if analyzed_count > 0 and skip_count == analyzed_count:
            latest_source_quality_warning = f"{src} produced analyzed candidates but all were SKIP."
            if src == 'fallback_market_candidates':
                recommended_next_action = "Improve primary candidate discovery; fallback is driving weak candidates."
    news_summary = latest_diag.get('latest_news_evidence_scoring_summary') if isinstance(latest_diag, dict) else {}
    if isinstance(news_summary, dict) and news_summary:
        qualified_news = news_summary.get('qualified_news_symbols') or []
        adjusted_count = int(news_summary.get('news_symbols_adjusted_count') or 0)
        still_baseline = news_summary.get('still_baseline_after_news_symbols') or []
        positive_symbols = news_summary.get('positive_keyword_symbols') or []
        if qualified_news and adjusted_count == 0 and len(still_baseline) == len(qualified_news):
            recommended_next_action = 'News evidence is reaching scanner but not materially improving catalyst score; review catalyst scoring inputs/model fallback.'
        if positive_symbols:
            top5 = latest_diag.get('top_5_candidates_by_score') or []
            pos_zero = [x for x in top5 if (x.get('catalyst_positive_terms') or []) and float(((x.get('catalyst_score_components') or {}).get('keyword_boost') or 0.0)) == 0.0]
            if pos_zero:
                recommended_next_action = 'Positive catalyst keywords are detected but keyword_boost remains zero; connect keyword evidence to scoring.'
    if isinstance(catalyst_baseline_reason_counts, dict) and catalyst_baseline_reason_counts:
        dominant_baseline = max(catalyst_baseline_reason_counts.items(), key=lambda kv: int(kv[1] or 0))[0]
        if dominant_baseline in {'FEATURE_STORE_BASELINE_ONLY', 'BASELINE_ONLY_NO_NEWS'}:
            recommended_next_action = "Fix catalyst/news feature generation before changing thresholds."
    unknown_sources = [x for x in (latest_diag.get('final_analyzed_symbols_with_source') or []) if (x or {}).get('source') == 'unknown']
    if (latest_diag.get('final_analyzed_symbols_with_source') or []) and len(unknown_sources) >= int(len(latest_diag.get('final_analyzed_symbols_with_source')) * 0.6):
        recommended_next_action = "Fix candidate source propagation before tuning strategy."

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
        "skip_reason_code_counts": dict(skip_reason_code_counts),
        "setup_grade_counts": dict(setup_grade_counts),
        "score_total_buckets": dict(score_total_buckets),
        "component_score_averages": component_score_averages,
        "top_non_executable_symbols": non_exec_symbols.most_common(10),
        "top_executable_symbols": executable_symbols.most_common(10),
        "blocked_reason_counts": dict(blocked_reason_counts),
        "user_context_missing_count": int(blocked_reason_counts.get("USER_CONTEXT_MISSING", 0)),
        "attributed_scan_count": attributed_scan_count,
        "unattributed_scan_count": unattributed_scan_count,
        "latest_attributed_scan_age_seconds": latest_attr_age,
        "latest_unattributed_scan_age_seconds": latest_unattr_age,
        "attribution_warning": bool(unattributed_scan_count > 0 and latest_unattr_age is not None and latest_unattr_age < 7200),
        "dominant_symbol_warning": repeated_best_pick_warning,
        "repeated_best_pick_warning": repeated_best_pick_warning,
        "dominant_symbol": dominant_symbol,
        "dominant_symbol_pct": dominant_symbol_pct,
        "same_symbol_count": same_symbol_count,
        "latest_dominant_symbol_decisions": latest_dominant_symbol_decisions[:10],
        "latest_dominant_symbol_skip_reasons": latest_dominant_symbol_skip_reasons[:20],
        "latest_dominant_symbol_skip_reason_codes": latest_dominant_symbol_skip_reason_codes[:20],
        "scan_contract_failure_counts": dict(scan_contract_failure_counts),
        "top_rejection_reasons": dict(rejection_reasons.most_common(20)),
        "latest_scan_diagnostics": latest_diag,
        "latest_candidate_count_raw": latest_diag.get("candidate_count_raw"),
        "latest_candidate_count_after_dedupe": latest_diag.get("candidate_count_after_dedupe"),
        "latest_candidate_count_after_user_filters": latest_diag.get("candidate_count_after_user_filters"),
        "latest_candidate_count_after_price_volume_filters": latest_diag.get("candidate_count_after_price_volume_filters"),
        "latest_candidate_count_primary_raw": latest_diag.get("candidate_count_primary_raw"),
        "latest_candidate_count_primary_after_dedupe": latest_diag.get("candidate_count_primary_after_dedupe"),
        "latest_candidate_count_after_fallback": latest_diag.get("candidate_count_after_fallback"),
        "latest_candidate_count_final_before_analysis": latest_diag.get("candidate_count_final_before_analysis"),
        "latest_analyzed_symbols": latest_diag.get("analyzed_symbols"),
        "latest_candidate_symbols_sample": latest_diag.get("candidate_symbols_sample"),
        "latest_top_5_candidates_by_score": latest_diag.get("top_5_candidates_by_score"),
        "latest_candidate_source_quality_summary": source_quality_summary,
        "latest_source_quality_warning": latest_source_quality_warning,
        "latest_catalyst_baseline_reason_counts": catalyst_baseline_reason_counts,
        "latest_catalyst_missing_reason_counts": catalyst_missing_reason_counts,
        "latest_news_evidence_scoring_summary": latest_diag.get("latest_news_evidence_scoring_summary"),
        "latest_news_catalyst_score_blockers": latest_diag.get("latest_news_catalyst_score_blockers"),
        "latest_catalyst_feature_store_hit_count": latest_diag.get("latest_catalyst_feature_store_hit_count"),
        "latest_catalyst_feature_store_missing_count": latest_diag.get("latest_catalyst_feature_store_missing_count"),
        "latest_premarket_data_unavailable_count": latest_diag.get("latest_premarket_data_unavailable_count"),
        "latest_premarket_volume_unavailable_symbols": latest_diag.get("latest_premarket_volume_unavailable_symbols"),
        "latest_premarket_volume_too_light_symbols": latest_diag.get("latest_premarket_volume_too_light_symbols"),
        "latest_premarket_volume_summary": latest_diag.get("latest_premarket_volume_summary"),
        "latest_premarkarket_volume_summary": latest_diag.get("latest_premarket_volume_summary"),
        "latest_executable_candidate_count": latest_diag.get("executable_candidate_count"),
        "latest_watch_candidate_count": latest_diag.get("watch_candidate_count"),
        "latest_skip_candidate_count": latest_diag.get("skip_candidate_count"),
        "latest_best_executable_candidate_symbol": latest_diag.get("best_executable_candidate_symbol"),
        "latest_best_watch_candidate_symbol": latest_diag.get("best_watch_candidate_symbol"),
        "latest_best_skip_candidate_symbol": latest_diag.get("best_skip_candidate_symbol"),
        "latest_best_pick_selection_reason": latest_diag.get("best_pick_selection_reason"),
        "latest_scan_starvation_flags": latest_diag.get("scan_starvation_flags") or [],
        "latest_scan_attribution_version": latest_attr_version,
        "scan_attribution_version_counts": dict(attr_version_counts),
        "latest_scan_has_new_diagnostics": has_new_diag,
        "latest_scan_missing_new_diagnostics_reason": missing_diag_reason,
        "latest_bar_data_requested_symbols_count": latest_diag.get("bar_data_requested_symbols_count"),
        "latest_daily_bars_returned_symbols_count": latest_diag.get("daily_bars_returned_symbols_count"),
        "latest_minute_bars_returned_symbols_count": latest_diag.get("minute_bars_returned_symbols_count"),
        "latest_missing_daily_bars_symbols": latest_diag.get("missing_daily_bars_symbols"),
        "latest_missing_minute_bars_symbols": latest_diag.get("missing_minute_bars_symbols"),
        "latest_symbols_with_snapshot_but_no_bars": latest_diag.get("symbols_with_snapshot_but_no_bars"),
        "latest_individual_bar_retry_attempted_count": latest_diag.get("individual_bar_retry_attempted_count"),
        "latest_individual_bar_retry_success_count": latest_diag.get("individual_bar_retry_success_count"),
        "latest_individual_bar_retry_failed_symbols": latest_diag.get("individual_bar_retry_failed_symbols"),
        "latest_asset_filter_rejection_counts": latest_diag.get("asset_filter_rejection_counts"),
        "latest_asset_metadata_requested_count": latest_diag.get("asset_metadata_requested_count"),
        "latest_asset_metadata_success_count": latest_diag.get("asset_metadata_success_count"),
        "latest_asset_metadata_failure_count": latest_diag.get("asset_metadata_failure_count"),
        "latest_asset_metadata_global_failure": latest_diag.get("asset_metadata_global_failure"),
        "latest_asset_metadata_degraded_mode": latest_diag.get("asset_metadata_degraded_mode"),
        "latest_asset_metadata_failure_reason_counts": latest_diag.get("asset_metadata_failure_reason_counts"),
        "latest_asset_metadata_failure_samples": latest_diag.get("asset_metadata_failure_samples"),
        "latest_asset_metadata_endpoint_used": latest_diag.get("asset_metadata_endpoint_used"),
        "latest_asset_metadata_degraded_allowed_count": latest_diag.get("asset_metadata_degraded_allowed_count"),
        "latest_asset_metadata_degraded_allowed_symbols": latest_diag.get("asset_metadata_degraded_allowed_symbols"),
        "latest_asset_metadata_degraded_rejection_counts": latest_diag.get("asset_metadata_degraded_rejection_counts"),
        "latest_asset_metadata_degraded_rejection_samples": latest_diag.get("asset_metadata_degraded_rejection_samples"),
        "latest_data_starvation_summary": data_starvation_summary,
        "sample_recent_failures": failures,
        "primary_blocker_summary": primary_blocker_summary,
        "recommended_next_action": recommended_next_action,
        "scanner_starvation_flags": starvation_flags,
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
