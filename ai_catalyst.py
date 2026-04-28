import logging
import json
import time
import os
from typing import List, Dict, Any
import numpy as np
import xgboost as xgb
from transformers import pipeline
import pandas as pd
import yfinance as yf

from feature_store import store
from scanner import get_company_news

logger = logging.getLogger(__name__)

try:
    import google.generativeai as genai
    HAS_GEMINI = True
except Exception:
    HAS_GEMINI = False

try:
    with open('catalyst_feedback.json', 'r', encoding='utf-8') as f:
        HISTORICAL_WEIGHTS = json.load(f)
except FileNotFoundError:
    HISTORICAL_WEIGHTS = {}
except Exception as e:
    logger.warning(f"Failed to load catalyst_feedback.json: {e}")
    HISTORICAL_WEIGHTS = {}

# --- NEW: Catalyst Keyword Definitions ---
# Tier 1: High-conviction institutional drivers (+0.25 probability)
TIER_1_KEYWORDS = [
    'FDA APPROVAL', 'PHASE 3', 'EARNINGS BEAT', 'RAISED GUIDANCE',
    'ACQUISITION', 'MERGER', 'CONTRACT WIN', 'BUYBACK'
]

# Tier 2: Positive but speculative drivers (+0.10 probability)
TIER_2_KEYWORDS = [
    'PARTNERSHIP', 'UPGRADE', 'PRODUCT LAUNCH', 'PHASE 2', 'PATENT'
]

# Penalties: Known "Bull Traps" or dilution (-0.20 probability)
NEGATIVE_KEYWORDS = [
    'OFFERING', 'DILUTION', 'SEC INVESTIGATION', 'MISS', 'LOWERED GUIDANCE', 'RESIGNATION'
]

# Load local FinBERT
try:
    logger.info("Loading local FinBERT model...")
    finbert = pipeline("text-classification", model="yiyanghkust/finbert-tone", top_k=None)
except Exception as e:
    logger.error(f"Failed to load FinBERT: {e}")
    finbert = None


def calculate_keyword_boost(headlines: List[Dict[str, Any]]) -> float:
    """Scans headlines for specific high-impact keywords to boost or penalize probability."""
    boost = 0.0
    # Combine all headlines for the lookback period into one uppercase string
    text = " ".join([h.get('headline', '').upper() for h in headlines if h.get('headline')])

    if not text:
        return 0.0

    # 1. Check for Tier 1 "Game Changers"
    for word in TIER_1_KEYWORDS:
        if word in text:
            boost += 0.25
            logger.info(f"TIER 1 CATALYST DETECTED: {word}")
            break  # Don't stack multiple Tier 1s

    # 2. Check for Tier 2 "Momentum Drivers"
    for word in TIER_2_KEYWORDS:
        if word in text:
            boost += 0.10
            break

    # 3. Check for "Red Flags" (High-probability of failure)
    for word in NEGATIVE_KEYWORDS:
        if word in text:
            boost -= 0.20
            logger.warning(f"NEGATIVE CATALYST DETECTED: {word}")
            break

    return boost


def compute_finbert_sentiment(headlines: List[Dict[str, Any]]) -> float:
    if not finbert or not headlines:
        return 0.0
    texts = [h.get('headline', '') for h in headlines if h.get('headline')]
    if not texts:
        return 0.0
    try:
        results = finbert(texts)
        scores = []
        for res in results:
            score_dict = {lbl['label']: lbl['score'] for lbl in res}
            net_sentiment = score_dict.get('Positive', 0) - score_dict.get('Negative', 0)
            scores.append(net_sentiment)
        return float(np.mean(scores))
    except Exception as e:
        logger.error(f"FinBERT inference failed: {e}")
        return 0.0


def verify_multisource_catalyst(
    sentiment_score: float,
    keyword_boost: float,
    rvol: float,
    gap_pct: float,
) -> float:
    """
    Ensures that news (NLP + Keywords) is confirmed by technical participation.
    Returns an alignment multiplier (0.5 to 1.5).
    """
    alignment = 1.0

    # 1. Verification: High-impact news must have high volume.
    if keyword_boost >= 0.25 and rvol < 2.0:
        alignment *= 0.6
        logger.warning("News Alignment Failed: Tier 1 news with insufficient volume.")

    # 2. Convergence: News + high volume + clean gap.
    if keyword_boost > 0 and rvol > 3.0 and (2.0 <= gap_pct <= 10.0):
        alignment *= 1.4

    # 3. Sentiment consistency check against keyword direction.
    if sentiment_score > 0.5 and keyword_boost > 0:
        alignment *= 1.1
    elif sentiment_score < 0 and keyword_boost > 0:
        alignment *= 0.8

    return max(0.5, min(1.5, alignment))


def batch_process_premarket(symbols: List[str]):
    """
    RUN THIS AT 9:20 AM.
    Processes news sentiment and keyword boosts for the Morning Scan.
    """
    logger.info(f"Starting refined pre-market ML processing for {len(symbols)} symbols...")

    for symbol in symbols:
        # 1. NLP Sentiment and Keyword Analysis
        headlines = get_company_news(symbol, lookback_days=1)
        sentiment_score = compute_finbert_sentiment(headlines)
        keyword_boost = calculate_keyword_boost(headlines)

        # 2. Gather Quantitative Features (Mocked - integrate with Alpaca bars in production)
        premarket_rvol = 2.5
        gap_pct = 4.0

        # 3. Refined Probability Calculation
        base_prob = 0.40
        alignment_mult = verify_multisource_catalyst(
            sentiment_score=sentiment_score,
            keyword_boost=keyword_boost,
            rvol=premarket_rvol,
            gap_pct=gap_pct,
        )
        historical_mult = float(HISTORICAL_WEIGHTS.get(symbol, 1.0))

        # Incorporate FinBERT (NLP tone) + Keyword Boost (Specific Event Type)
        prob = (
            base_prob
            + (sentiment_score * 0.20)
            + (min(premarket_rvol, 5) * 0.05)
            + keyword_boost
        ) * alignment_mult * historical_mult

        if gap_pct > 15.0:
            prob -= 0.15  # Exhaustion penalty

        final_probability = max(0.01, min(0.99, prob))

        # 4. Save to Feature Store
        store.update_symbol_features(symbol, {
            'finbert_sentiment': round(sentiment_score, 3),
            'keyword_boost': round(keyword_boost, 2),
            'p_success': round(final_probability, 4),
            'headline_count': len(headlines)
        })

    logger.info("Pre-market refinement complete. AI high-probability weights are live.")


def fetch_sec_financials(symbol: str) -> str:
    """
    Pull key SEC-adjacent financial context via yfinance and return
    a clean, human-readable summary string.
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        financials = ticker.financials
    except Exception as e:
        logger.error(f"Failed to fetch SEC financial data for {symbol}: {e}")
        return f"SEC Financial Snapshot ({symbol.upper()}): unavailable."

    total_debt = info.get("totalDebt", "N/A")
    float_size = info.get("floatShares", "N/A")
    short_interest = info.get("shortPercentOfFloat", info.get("sharesPercentSharesOut", "N/A"))

    # Prefer explicit quarterly revenue growth from info; fallback to a simple
    # QoQ estimate from financial statement rows if possible.
    revenue_growth = info.get("revenueGrowth")
    if revenue_growth is None and isinstance(financials, pd.DataFrame) and not financials.empty:
        try:
            revenue_row = financials.loc["Total Revenue"].dropna()
            if len(revenue_row) >= 2 and revenue_row.iloc[1] != 0:
                revenue_growth = (revenue_row.iloc[0] - revenue_row.iloc[1]) / abs(revenue_row.iloc[1])
        except Exception:
            revenue_growth = "N/A"

    def _fmt_int(value: Any) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{int(value):,}"
        return str(value)

    def _fmt_pct(value: Any) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value * 100:.2f}%"
        return str(value)

    return (
        f"SEC Financial Snapshot ({symbol.upper()}): "
        f"Total Debt: {_fmt_int(total_debt)} | "
        f"Float Size: {_fmt_int(float_size)} shares | "
        f"Short Interest: {_fmt_pct(short_interest)} | "
        f"Latest Quarterly Revenue Growth: {_fmt_pct(revenue_growth)}"
    )


def fetch_social_sentiment(symbol: str) -> str:
    """
    Mock/placeholder social sentiment stream.
    Simulates scraping Reddit (r/pennystocks, r/wallstreetbets) and X for a cashtag.
    """
    cashtag = f"${symbol.upper()}"

    # Deterministic mock levels for robust placeholder behavior.
    social_profiles = {
        "high": "High retail chatter. 80% rocket emojis. Keywords: squeeze, moon.",
        "moderate": "Moderate retail chatter. 52% bullish mentions. Keywords: breakout, momentum.",
        "low": "Low retail chatter. Mixed sentiment. Keywords: watchlist, speculative.",
    }

    bucket = sum(ord(c) for c in cashtag) % 3
    profile_key = ["high", "moderate", "low"][bucket]

    return (
        f"Social Sentiment ({cashtag}) [Mock]: "
        f"Sources=Reddit(r/pennystocks,r/wallstreetbets)+X. "
        f"{social_profiles[profile_key]}"
    )


def _fallback_catalyst_payload(symbol: str, comprehensive_dossier: str) -> Dict[str, Any]:
    """Strict JSON-safe fallback payload for frontend stability."""
    return {
        "symbol": symbol.upper(),
        "catalyst_score": 35,
        "risk_flag": "PUMP_AND_DUMP_RISK",
        "forensic_note": (
            "Fallback path used. Could not validate with Gemini, so score is capped "
            "for safety and marked as pump-and-dump risk."
        ),
        "comprehensive_dossier_excerpt": comprehensive_dossier[:350],
    }


def generate_catalyst_score(symbol: str) -> Dict[str, Any]:
    """
    Gemini-backed catalyst scoring with strict JSON output and forensic pump-and-dump rules.
    """
    news_data = get_company_news(symbol, lookback_days=2)
    sec_financials = fetch_sec_financials(symbol)
    social_sentiment = fetch_social_sentiment(symbol)

    comprehensive_dossier = (
        "=== NEWS DATA ===\n"
        f"{json.dumps(news_data, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "=== SEC FINANCIALS ===\n"
        f"{sec_financials}\n\n"
        "=== SOCIAL SENTIMENT ===\n"
        f"{social_sentiment}"
    )

    api_key = os.getenv("GEMINI_API_KEY")
    if not (HAS_GEMINI and api_key):
        return _fallback_catalyst_payload(symbol, comprehensive_dossier)

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=(
                "You are an institutional forensic auditor. Cross-reference the provided News, "
                "SEC Financials, and Social Sentiment. Your primary goal is to detect Pump and "
                "Dump schemes.\n"
                "RULE 1: If social sentiment is 'high retail chatter' but the SEC data shows high "
                "debt, massive recent dilution, or poor revenue, you MUST flag this as a "
                "'PUMP_AND_DUMP_RISK'.\n"
                "RULE 2: If flagged as a risk, cap the maximum catalyst score at 40/100, regardless "
                "of the news hype.\n"
                "RULE 3: Include a 'forensic_note' in your JSON response detailing why the setup was "
                "validated or flagged.\n"
                "Return STRICT JSON only with keys: symbol (string), catalyst_score (integer 0-100), "
                "risk_flag (string: PUMP_AND_DUMP_RISK or CLEAR), forensic_note (string)."
            ),
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        prompt = (
            "Analyze this comprehensive_dossier and return strict JSON only.\n\n"
            f"comprehensive_dossier:\n{comprehensive_dossier}"
        )
        response = model.generate_content(prompt, request_options={"timeout": 12.0})
        parsed = json.loads(response.text)

        required_keys = {"symbol", "catalyst_score", "risk_flag", "forensic_note"}
        if set(parsed.keys()) != required_keys:
            raise ValueError("Gemini payload keys invalid")
        if not isinstance(parsed["symbol"], str):
            raise ValueError("symbol must be string")
        if not isinstance(parsed["forensic_note"], str):
            raise ValueError("forensic_note must be string")
        if not isinstance(parsed["catalyst_score"], int):
            raise ValueError("catalyst_score must be integer")
        if parsed["risk_flag"] not in {"PUMP_AND_DUMP_RISK", "CLEAR"}:
            raise ValueError("risk_flag invalid")
        if parsed["risk_flag"] == "PUMP_AND_DUMP_RISK":
            parsed["catalyst_score"] = min(parsed["catalyst_score"], 40)
        parsed["catalyst_score"] = max(0, min(parsed["catalyst_score"], 100))
        parsed["symbol"] = symbol.upper()
        return parsed
    except Exception as e:
        logger.warning("Gemini catalyst scoring failed for %s: %s", symbol, e)
        return _fallback_catalyst_payload(symbol, comprehensive_dossier)
