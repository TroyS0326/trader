import logging
import time
from typing import List, Dict, Any
import numpy as np
import xgboost as xgb
from transformers import pipeline
import pandas as pd

from feature_store import store
from scanner import get_company_news  # Assuming this still fetches Finnhub news

logger = logging.getLogger(__name__)

# Load local FinBERT specifically trained on financial news tone
# This runs locally on your CPU/GPU, eliminating API latency
try:
    logger.info("Loading local FinBERT model...")
    finbert = pipeline("text-classification", model="yiyanghkust/finbert-tone", top_k=None)
except Exception as e:
    logger.error(f"Failed to load FinBERT: {e}")
    finbert = None

# Initialize XGBoost Model (Placeholder for your trained model)
xgb_model = xgb.XGBClassifier()
# xgb_model.load_model("trained_orb_model.json") # Uncomment when you have trained your RLSP model


def compute_finbert_sentiment(headlines: List[Dict[str, Any]]) -> float:
    """Converts headlines into a continuous sentiment score (-1.0 to 1.0)."""
    if not finbert or not headlines:
        return 0.0

    texts = [h.get('headline', '') for h in headlines if h.get('headline')]
    if not texts:
        return 0.0

    try:
        results = finbert(texts)
        scores = []
        for res in results:
            # yiyanghkust/finbert-tone outputs labels: Positive, Negative, Neutral
            score_dict = {lbl['label']: lbl['score'] for lbl in res}
            # Calculate net sentiment: Positive probability - Negative probability
            net_sentiment = score_dict.get('Positive', 0) - score_dict.get('Negative', 0)
            scores.append(net_sentiment)
        return float(np.mean(scores))
    except Exception as e:
        logger.error(f"FinBERT inference failed: {e}")
        return 0.0


def batch_process_premarket(symbols: List[str]):
    """
    RUN THIS AT 9:20 AM.
    Fetches news, scores sentiment, calculates pre-market anomalies,
    runs XGBoost, and saves to the Feature Store.
    """
    logger.info(f"Starting pre-market ML batch processing for {len(symbols)} symbols...")

    for symbol in symbols:
        # 1. NLP Sentiment Scoring
        headlines = get_company_news(symbol, lookback_days=1)
        sentiment_score = compute_finbert_sentiment(headlines)

        # 2. Gather Quantitative Features (Mocked here - pull from Alpaca minute bars in reality)
        # You would calculate these using your existing scanner.py utility functions
        premarket_rvol = 2.5  # Placeholder: (premarket volume / 30-day avg premarket volume)
        gap_pct = 4.0         # Placeholder: ((Current - PrevClose) / PrevClose) * 100
        atr_pct = 0.05        # Placeholder: ATR / Current Price

        # 3. XGBoost Inference
        # In production, use your trained model. For now, we simulate a probability.
        feature_vector = pd.DataFrame([{
            'sentiment': sentiment_score,
            'premarket_rvol': premarket_rvol,
            'gap_pct': gap_pct,
            'atr_pct': atr_pct
        }])

        # simulated_prob = xgb_model.predict_proba(feature_vector)[0][1]

        # DUMMY PROBABILITY LOGIC (Remove after training XGBoost)
        # Higher sentiment, higher RVOL, and moderate gaps increase probability
        base_prob = 0.40
        prob = base_prob + (sentiment_score * 0.20) + (min(premarket_rvol, 5) * 0.05)
        if gap_pct > 15.0:
            prob -= 0.15  # Exhaustion penalty

        final_probability = max(0.01, min(0.99, prob))

        # 4. Save to blazing fast in-memory Feature Store
        store.update_symbol_features(symbol, {
            'finbert_sentiment': round(sentiment_score, 3),
            'p_success': round(final_probability, 4),
            'headline_count': len(headlines)
        })

    logger.info("Pre-market ML batch complete. Feature Store loaded.")
