import json
import logging
import os
from typing import Any, Dict

# Configure logger for this module
logger = logging.getLogger(__name__)

# Attempt to import the Gemini SDK. If it's missing, the app won't crash;
# it will just gracefully use the deterministic fallback logic.
try:
    import google.generativeai as genai

    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False


def generate_fallback_thesis(setup: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generates a deterministic fallback thesis if Gemini fails, times out,
    or the API key is not configured.
    """
    # Safely extract data with default fallbacks
    symbol = setup.get("symbol", "UNKNOWN")
    score = setup.get("score_total", "N/A")
    grade = setup.get("setup_grade", "N/A")
    entry = setup.get("entry_price", setup.get("entry", "N/A"))
    stop = setup.get("stop_price", setup.get("stop", "N/A"))
    target_1 = setup.get("target_1", "N/A")
    target_2 = setup.get("target_2", "N/A")

    # Extract deeply nested data safely
    details = setup.get("details", {})
    rvol = details.get("rvol", "N/A")
    catalyst = setup.get("scores", {}).get("catalyst", "N/A")

    # Build a list of supporting factors based on what's available
    supporting_factors = []
    if rvol != "N/A":
        supporting_factors.append(f"RVOL of {rvol}")
    if catalyst != "N/A":
        supporting_factors.append(f"catalyst score of {catalyst}")

    support_str = (
        " / ".join(supporting_factors)
        if supporting_factors
        else "technical parameters"
    )

    # Assemble the exact requested fallback string
    thesis = (
        f"XeanVI is flagging this setup because {symbol} has a score_total of {score} "
        f"with setup grade {grade}. The setup is supported by {support_str} and is being "
        f"evaluated against entry {entry}, stop {stop}, and targets {target_1} / {target_2}. "
        "This is a probability-based setup, not a guaranteed outcome."
    )

    return {
        "thesis": thesis,
        "key_reasons": [
            f"Algorithm setup grade: {grade}",
            f"Total alignment score: {score}",
            "Defined risk/reward boundaries established",
        ],
        "risk_note": "This is a probability-based setup, not a guaranteed outcome. Past performance does not indicate future results.",
    }


def generate_trade_thesis(setup: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ingests an algorithmic trade setup payload and returns a natural language
    explanation of the trade using Gemini, returning a structured JSON response.
    Returns a deterministic fallback if the API call fails or times out.
    """
    api_key = os.getenv("GEMINI_API_KEY")

    # Fast-fail to fallback if Gemini isn't configured
    if not HAS_GEMINI or not api_key:
        logger.info("Gemini SDK or API key not found. Using fallback explainability thesis.")
        return generate_fallback_thesis(setup)

    try:
        # Securely configure the client (avoids printing or exposing the key)
        genai.configure(api_key=api_key)

        # Allow configurable model overriding, default to fast & cheap 2.0-flash
        model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-2.0-flash")

        system_instruction = (
            "You are a professional trading system algorithm explaining a quantitative trade setup. "
            "Analyze the provided trade setup data and explain why it is strong. "
            "Cite specific data points from the payload such as RVOL, score_total, setup_grade, range breaks, "
            "entry, stop, targets, and catalyst score if they exist in the data. "
            "CRITICAL RULES: "
            "1. Never promise profit. "
            "2. Never use words like 'guaranteed', 'risk-free', 'certain', or 'can't lose'. "
            "3. Write objectively, like an institutional execution log, not financial advice. "
            "4. You must respond in strict JSON format with exactly three keys: "
            "'thesis' (string, natural language explanation), "
            "'key_reasons' (list of 3 string reasons), "
            "'risk_note' (string, a short disclaimer about risk/probability)."
        )

        # Initialize the model with strict JSON formatting and low temperature
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_instruction,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2,  # Keep generation highly analytical and deterministic
            ),
        )

        # Build prompt from the setup dictionary
        prompt = (
            "Analyze this trade setup data and generate the JSON payload:\n"
            f"{json.dumps(setup, indent=2)}"
        )

        # Execute generation with a strict 10.0-second timeout to prevent blocking /api/execute
        response = model.generate_content(prompt, request_options={"timeout": 10.0})

        # Parse the JSON response
        result = json.loads(response.text)

        # Validate that the model returned the exact keys we need
        required_keys = {"thesis", "key_reasons", "risk_note"}
        if required_keys.issubset(result.keys()):
            return result

        raise ValueError(
            "Gemini JSON response missing required keys. "
            f"Found: {list(result.keys())}"
        )

    except Exception as exc:
        # Catch timeouts, JSON parse errors, missing keys, or API outages gracefully
        # Logging just the error string prevents leaking full stack traces or payloads into basic logs
        logger.warning(
            "Gemini explainability generation failed (%s). "
            "Falling back to deterministic thesis.",
            str(exc),
        )
        return generate_fallback_thesis(setup)
