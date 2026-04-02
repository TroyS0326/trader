import threading
from typing import Dict, Any


class FeatureStore:
    """
    In-memory singleton to hold pre-computed ML features and XGBoost
    probabilities generated during the 8:00 AM - 9:25 AM pre-market window.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}

    def update_symbol_features(self, symbol: str, features: Dict[str, Any]):
        with self._lock:
            key = symbol.upper()
            if key not in self._store:
                self._store[key] = {}
            self._store[key].update(features)

    def get_symbol_features(self, symbol: str) -> Dict[str, Any]:
        with self._lock:
            return self._store.get(symbol.upper(), {})

    def clear(self):
        with self._lock:
            self._store.clear()


# Global singleton instance
store = FeatureStore()
