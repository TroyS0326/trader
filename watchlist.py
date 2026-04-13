import json
import threading
import time
from typing import Any, Dict, List

from config import WATCHLIST_PUSH_SECONDS
from scanner import get_latest_quotes, safe_num


class WatchlistManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []
        self._clients = set()

    def set_items(self, items: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._items = [dict(item) for item in items]

    def get_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items]

    def refresh(self) -> List[Dict[str, Any]]:
        items = self.get_items()
        symbols = [i['symbol'] for i in items if i.get('symbol')]
        if not symbols:
            return []
        quotes = get_latest_quotes(symbols)
        refreshed = []
        for item in items:
            symbol = item['symbol']
            q = quotes.get(symbol, {})
            current = safe_num(q.get('ap')) or safe_num(q.get('bp')) or item.get('current_price') or item.get('entry_price')
            entry = safe_num(item.get('entry_price'))
            stop = safe_num(item.get('stop_price'))
            buy_upper = safe_num(item.get('buy_upper'))
            signal = 'WATCH'
            if not item.get('buy_window_open', True):
                signal = 'WAIT'
            elif current < stop:
                signal = 'BROKEN'
            elif item.get('breakout_confirmed') and current >= entry and current <= buy_upper:
                signal = 'TRIGGERED'
            elif current >= entry * 0.995:
                signal = 'NEAR ENTRY'
            item['current_price'] = round(current, 2)
            item['live_signal'] = signal
            refreshed.append(item)
        self.set_items(refreshed)
        return refreshed

    def stream(self, ws) -> None:
        with self._lock:
            self._clients.add(ws)
        try:
            while True:
                payload = {
                    'type': 'watchlist',
                    'items': self.refresh(),
                    'ts': time.time(),
                }
                ws.send(json.dumps(payload))
                time.sleep(WATCHLIST_PUSH_SECONDS)
        finally:
            with self._lock:
                self._clients.discard(ws)

    def broadcast_all(self, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        dead_clients = []
        for ws in clients:
            try:
                ws.send(payload)
            except Exception:
                dead_clients.append(ws)
        if dead_clients:
            with self._lock:
                for ws in dead_clients:
                    self._clients.discard(ws)


watchlist_manager = WatchlistManager()
