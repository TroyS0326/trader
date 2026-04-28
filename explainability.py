import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Optional Gemini dependency: do not crash if package is unavailable.
try:
    import google.generativeai as genai

    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False


EXPECTED_KEYS = ("thesis", "key_reasons", "risk_note")


def generate_fallback_thesis(setup: Dict) -> Dict[str, Any]:
    """Return a deterministic safety-net thesis payload."""
    symbol = setup.get("symbol", "UNKNOWN")
    setup_grade = setup.get("setup_grade", "N/A")
    score_total = setup.get("score_total", "N/A")
    entry = setup.get("entry_price", setup.get("entry", "N/A"))
    stop = setup.get("stop_price", setup.get("stop", "N/A"))
    target_1 = setup.get("target_1", "N/A")

    return {
        "thesis": (
            f"Deterministic fallback thesis: {symbol} is being evaluated with "
            f"setup_grade={setup_grade}, score_total={score_total}, entry={entry}, "
            f"stop={stop}, target_1={target_1}."
        ),
        "key_reasons": [
            f"Setup grade observed: {setup_grade}",
            f"Total score observed: {score_total}",
            "Risk is bounded by predefined entry/stop/target levels",
        ],
        "risk_note": (
            "This output is a fallback summary and represents a probabilistic setup, "
            "not a guarantee of outcome."
        ),
    }


def _is_valid_payload(payload: Dict[str, Any]) -> bool:
    """Validate strict schema: exactly three keys with expected value types."""
    if set(payload.keys()) != set(EXPECTED_KEYS):
        return False

    if not isinstance(payload["thesis"], str):
        return False

    if not isinstance(payload["risk_note"], str):
        return False

    reasons = payload["key_reasons"]
    if not isinstance(reasons, list) or len(reasons) != 3:
        return False

    return all(isinstance(reason, str) for reason in reasons)


def generate_trade_thesis(setup: Dict) -> Dict[str, Any]:
    """Generate a thesis using Gemini; fallback deterministically on any failure."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not HAS_GEMINI or not api_key:
        return generate_fallback_thesis(setup)

    try:
        genai.configure(api_key=api_key)

        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=(
                "You are an objective, professional algorithmic trading system. "
                "Analyze the provided setup and return strict JSON only."
            ),
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )

        prompt = (
            "Return JSON with exactly three keys: thesis (string), "
            "key_reasons (list of exactly 3 strings), and risk_note (string). "
            "Do not include markdown, commentary, or extra keys.\n"
            "Trade setup payload:\n"
            f"{json.dumps(setup, separators=(',', ':'))}"
        )

        response = model.generate_content(
            prompt,
            request_options={"timeout": 10.0},
        )
        parsed = json.loads(response.text)

        if _is_valid_payload(parsed):
            return parsed

        raise ValueError("Gemini response failed strict schema validation")
    except Exception as exc:
        logger.warning("Gemini thesis generation failed: %s", exc)
        return generate_fallback_thesis(setup)
