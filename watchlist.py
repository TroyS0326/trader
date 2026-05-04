import json
import threading
import time
from typing import Any, Dict, List

from config import WATCHLIST_PUSH_SECONDS
from scanner import get_latest_quotes, resolve_data_feed
from utils import safe_num


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

    def refresh(self, user: Any = None) -> List[Dict[str, Any]]:
        items = self.get_items()
        symbols = [i['symbol'] for i in items if i.get('symbol')]
        if not symbols:
            return []
        quotes = get_latest_quotes(symbols, feed=resolve_data_feed(user))
        refreshed = []
        for item in items:
            symbol = item['symbol']
            q = quotes.get(symbol, {})
            current = safe_num(q.get('ap')) or safe_num(q.get('bp')) or item.get('current_price') or item.get('entry_price')
            entry = safe_num(item.get('entry_price'))
            stop = safe_num(item.get('stop_price'))
            buy_upper = safe_num(item.get('buy_upper'))
            signal = 'WATCH'
            reason = 'Monitoring setup.'
            if not q:
                signal, reason = 'NO QUOTE', 'No quote returned by selected data feed.'
            elif stop is not None and current < stop:
                signal, reason = 'SETUP BROKEN: BELOW STOP', 'Below calculated stop. Setup invalidated, not app failure.'
            elif item.get('vwap') and current < safe_num(item.get('vwap')):
                signal, reason = 'SETUP BROKEN: VWAP FAILURE', 'Price failed to hold VWAP.'
            elif not item.get('buy_window_open', True):
                signal, reason = 'WAIT: BUY WINDOW CLOSED', 'Buy window is closed by risk policy.'
            elif buy_upper is not None and current > buy_upper:
                signal, reason = 'EXTENDED / WAIT FOR PULLBACK', 'Price is extended beyond allowed buy range.'
            elif item.get('breakout_confirmed') and entry is not None and buy_upper is not None and current >= entry and current <= buy_upper:
                signal, reason = 'TRIGGERED', 'Breakout confirmed inside buy zone.'
            elif entry is not None and current >= entry * 0.995:
                signal, reason = 'NEAR ENTRY', 'Price is approaching planned entry.'
            item['current_price'] = round(float(current), 2)
            item['live_signal'] = signal
            item['live_signal_reason'] = reason
            item['live_signal_data'] = {'current': round(float(current),2), 'stop': stop, 'entry': entry, 'buy_upper': buy_upper, 'timestamp': q.get('t')}
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
