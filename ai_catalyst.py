import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gemini-2.5-flash'
model_name = (GEMINI_MODEL or DEFAULT_MODEL).removeprefix('models/')
GEMINI_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent'

STRICT_SYSTEM_PROMPT = (
    'You are a strict day-trading catalyst judge. You must penalize vague PR, conference announcements, '
    'social-media hype, generic partnerships, promotional fluff, secondary offerings, dilution risk, legal problems, '
    'mixed headlines, and stale news. Only give 4 or 5 when the headlines strongly suggest a real same-day catalyst '
    'such as earnings beat with guidance, FDA approval, material contract, takeover news, or hard regulatory event. '
    'If uncertain, score lower. Return strict JSON only.'
)

CACHE_TTL_SECONDS = int(float(os.getenv('GEMINI_CACHE_TTL_MINUTES', '30')) * 60)
CACHE_PATH = Path(os.getenv('GEMINI_CACHE_PATH', '/tmp/veteran_trader_gemini_cache.json'))
MAX_HEADLINES = int(os.getenv('GEMINI_MAX_HEADLINES', '5'))
REQUEST_TIMEOUT = int(os.getenv('GEMINI_TIMEOUT_SECONDS', '25'))
MAX_CACHE_ITEMS = int(os.getenv('GEMINI_MAX_CACHE_ITEMS', '500'))
MAX_OUTPUT_TOKENS = int(os.getenv('GEMINI_MAX_OUTPUT_TOKENS', '800'))
RETRY_DELAYS = [float(x.strip()) for x in os.getenv('GEMINI_RETRY_DELAYS', '2,5,10').split(',') if x.strip()]

_COOLDOWN_UNTIL = 0.0


def _fallback(reason: str) -> Dict[str, Any]:
    return {
        'used_ai': False,
        'score': None,
        'catalyst_type': 'unknown',
        'direction': 'unknown',
        'confidence': 'low',
        'reason': reason,
        'hard_pass': False,
        'cache_hit': False,
    }


def _normalize_headlines(headlines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    trimmed: List[Dict[str, Any]] = []
    for item in headlines[:MAX_HEADLINES]:
        trimmed.append({
            'headline': item.get('headline', ''),
            'summary': item.get('summary', ''),
            'source': item.get('source', ''),
            'datetime': item.get('datetime'),
            'url': item.get('url', ''),
        })
    return trimmed


def _cache_key(symbol: str, trimmed: List[Dict[str, Any]]) -> str:
    blob = json.dumps({
        'symbol': symbol.upper(),
        'model': model_name,
        'headlines': trimmed,
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _load_cache() -> Dict[str, Any]:
    try:
        if not CACHE_PATH.exists():
            return {}
        with CACHE_PATH.open('r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        logger.exception('Failed to load Gemini cache from %s', CACHE_PATH)
    return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        items = list(cache.items())
        if len(items) > MAX_CACHE_ITEMS:
            items.sort(key=lambda kv: kv[1].get('saved_at', 0))
            items = items[-MAX_CACHE_ITEMS:]
            cache = dict(items)
        tmp_path = CACHE_PATH.with_suffix('.tmp')
        with tmp_path.open('w', encoding='utf-8') as f:
            json.dump(cache, f)
        tmp_path.replace(CACHE_PATH)
    except Exception:
        logger.exception('Failed to save Gemini cache to %s', CACHE_PATH)


def _get_cached(cache_key: str) -> Optional[Dict[str, Any]]:
    cache = _load_cache()
    entry = cache.get(cache_key)
    if not entry:
        return None
    saved_at = float(entry.get('saved_at', 0))
    if time.time() - saved_at > CACHE_TTL_SECONDS:
        return None
    result = dict(entry.get('result', {}))
    if result:
        result['cache_hit'] = True
        return result
    return None


def _set_cached(cache_key: str, result: Dict[str, Any]) -> None:
    cache = _load_cache()
    cache[cache_key] = {
        'saved_at': time.time(),
        'result': result,
    }
    _save_cache(cache)


def _strip_markdown_and_extract_json(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json|JSON)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)

    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]

    return cleaned.strip()


def _extract_text(data: Dict[str, Any]) -> str:
    candidates = data.get('candidates', [])
    if not candidates:
        raise ValueError(f'No candidates returned. Raw response: {json.dumps(data)[:2000]}')

    parts = candidates[0].get('content', {}).get('parts', [])
    if not parts:
        raise ValueError(f'No content parts returned. Raw response: {json.dumps(data)[:2000]}')

    text = parts[0].get('text', '')
    if not text or not text.strip():
        raise ValueError(f'Empty text returned. Raw response: {json.dumps(data)[:2000]}')

    return _strip_markdown_and_extract_json(text)


def _coerce_result(parsed: Dict[str, Any]) -> Dict[str, Any]:
    try:
        score = int(parsed.get('score') or 1)
    except (TypeError, ValueError):
        score = 1

    score = max(1, min(5, score))

    return {
        'used_ai': True,
        'score': score,
        'catalyst_type': str(parsed.get('catalyst_type', 'unknown') or 'unknown'),
        'direction': str(parsed.get('direction', 'unknown') or 'unknown'),
        'confidence': str(parsed.get('confidence', 'low') or 'low'),
        'reason': str(parsed.get('reason', 'No reason provided.') or 'No reason provided.'),
        'hard_pass': bool(parsed.get('hard_pass', False)),
        'cache_hit': False,
    }


def classify_news_with_gemini(symbol: str, headlines: List[Dict[str, Any]]) -> Dict[str, Any]:
    global _COOLDOWN_UNTIL

    if not GEMINI_API_KEY:
        return _fallback('Gemini disabled: GEMINI_API_KEY is missing.')
    if not headlines:
        return _fallback('Gemini skipped: no headlines available for this symbol.')

    trimmed = _normalize_headlines(headlines)
    key = _cache_key(symbol, trimmed)

    cached = _get_cached(key)
    if cached:
        return cached

    now = time.time()
    if now < _COOLDOWN_UNTIL:
        remaining = max(1, int(_COOLDOWN_UNTIL - now))
        return _fallback(f'Gemini cooling down after rate limit. Retry in {remaining}s.')

    prompt = (
        STRICT_SYSTEM_PROMPT
        + ' Return JSON with keys: '
        + 'score (integer 1-5), catalyst_type '
        + '(earnings,fda,contract,guidance,mna,legal,dilution,macro,noise,other), '
        + 'direction (bullish,bearish,mixed,unknown), confidence (high,medium,low), '
        + 'hard_pass (true/false), reason (max 30 words). '
        + f'Symbol: {symbol}. Headlines: {json.dumps(trimmed)}'
    )

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.05,
            'responseMimeType': 'application/json',
            'maxOutputTokens': MAX_OUTPUT_TOKENS,
        },
    }

    delays = [0.0] + RETRY_DELAYS
    last_error = 'Unknown Gemini failure.'
    last_status_code = None
    last_body = ''

    for attempt, delay in enumerate(delays, start=1):
        if delay > 0:
            time.sleep(delay)

        try:
            resp = requests.post(
                GEMINI_URL,
                headers={
                    'Content-Type': 'application/json',
                    'x-goog-api-key': GEMINI_API_KEY,
                },
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            last_status_code = resp.status_code
            last_body = resp.text[:3000]

            if resp.status_code == 429:
                last_error = f'429 rate limited on attempt {attempt}'
                logger.warning(
                    'Gemini rate limited for %s on attempt %s. status=%s body=%s',
                    symbol, attempt, resp.status_code, last_body
                )
                continue

            resp.raise_for_status()
            data = resp.json()
            text = _extract_text(data)

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                logger.exception(
                    'Gemini JSON parse failed for %s. cleaned_text=%s raw_body=%s',
                    symbol, text[:2000], last_body
                )
                return _fallback(f'Gemini JSON parse failed: {e}')

            result = _coerce_result(parsed)
            _set_cached(key, result)
            return result

        except requests.HTTPError as e:
            last_error = f'HTTP error {resp.status_code if "resp" in locals() else "unknown"}: {e}'
            logger.exception(
                'Gemini HTTP error for %s. status=%s body=%s',
                symbol, last_status_code, last_body
            )
            break
        except requests.RequestException as e:
            last_error = f'Request error: {e}'
            logger.exception('Gemini request error for %s', symbol)
            break
        except Exception as e:
            last_error = f'Unexpected Gemini error: {e}'
            logger.exception(
                'Gemini unexpected error for %s. status=%s body=%s',
                symbol, last_status_code, last_body
            )
            break

    if '429' in last_error:
        cooldown = max(RETRY_DELAYS[-1] if RETRY_DELAYS else 10.0, 15.0)
        _COOLDOWN_UNTIL = time.time() + cooldown
        stale = _load_cache().get(key)
        if stale and stale.get('result'):
            result = dict(stale['result'])
            result['cache_hit'] = True
            result['used_ai'] = True
            result['reason'] = f'Using cached Gemini result during cooldown. Original issue: {last_error}'
            return result
        return _fallback(f'Gemini rate limited. Cooling down for {int(cooldown)}s.')

    if last_status_code is not None:
        return _fallback(f'Gemini call failed. HTTP {last_status_code}. Check logs for raw response body.')

    return _fallback(f'Gemini call failed: {last_error}')
