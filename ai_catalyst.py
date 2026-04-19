import logging
import json
import time
from typing import List, Dict, Any
import numpy as np
import xgboost as xgb
from transformers import pipeline
import pandas as pd

from feature_store import store
from scanner import get_company_news

logger = logging.getLogger(__name__)

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
