import logging
import queue
from typing import Any, Dict, Optional

import polars as pl
from tqdm import tqdm

logger = logging.getLogger(__name__)


# --- 1. EVENT DEFINITIONS ---
class Event:
    pass


class MarketEvent(Event):
    """Triggered when a new bar/tick of data arrives."""

    def __init__(self, symbol: str, timestamp: str, current_data: Dict[str, Any]):
        self.type = 'MARKET'
        self.symbol = symbol
        self.timestamp = timestamp
        self.current_data = current_data


class SignalEvent(Event):
    """Triggered when the XGBoost/Strategy identifies an ORB setup."""

    def __init__(self, symbol: str, p_success: float, entry: float, stop: float, target: float):
        self.type = 'SIGNAL'
        self.symbol = symbol
        self.p_success = p_success
        self.entry = entry
        self.stop = stop
        self.target = target


class OrderEvent(Event):
    """Triggered by the Portfolio Manager to size and send to the Broker."""

    def __init__(self, symbol: str, qty: int, order_type: str, limit_price: Optional[float] = None):
        self.type = 'ORDER'
        self.symbol = symbol
        self.qty = qty
        self.order_type = order_type
        self.limit_price = limit_price


class FillEvent(Event):
    """Triggered by the Broker when an order is filled (accounting for slippage)."""

    def __init__(self, symbol: str, qty: int, fill_price: float, commission: float):
        self.type = 'FILL'
        self.symbol = symbol
        self.qty = qty
        self.fill_price = fill_price
        self.commission = commission


class StrategyHandler:
    """Placeholder strategy handler that can emit SignalEvents."""

    def __init__(self, events_queue: queue.Queue):
        self.events = events_queue

    def on_market_data(self, event: MarketEvent):
        # Intentionally left as a no-op until strategy logic is added.
        _ = event


class PortfolioHandler:
    """Placeholder portfolio handler for signal sizing and fill accounting."""

    def __init__(self, events_queue: queue.Queue):
        self.events = events_queue

    def on_signal(self, event: SignalEvent):
        # Intentionally left as a no-op until Kelly sizing logic is added.
        _ = event

    def on_fill(self, event: FillEvent):
        # Intentionally left as a no-op until accounting logic is added.
        _ = event

    def print_summary(self):
        logger.info('Portfolio summary placeholder. Implement metrics/reporting here.')


# --- 3. MICROSTRUCTURE EXECUTION HANDLER ---
class ExecutionHandler:
    def __init__(self, events_queue: queue.Queue):
        self.events = events_queue
        self.latest_market_data: Dict[str, Dict[str, Any]] = {}

    def on_market_data(self, event: MarketEvent):
        """Keep track of the latest price and volume for slippage math."""
        self.latest_market_data[event.symbol] = event.current_data

    def on_order(self, event: OrderEvent):
        """Simulates fills, enforcing the 5% liquidity rule and adding slippage."""
        current_data = self.latest_market_data.get(event.symbol)
        if not current_data:
            return

        # Get the actual volume of the bar we are trying to trade into
        bar_volume = current_data.get('volume', 10000)
        current_price = current_data.get('close', event.limit_price)
        if current_price is None:
            return

        spread = 0.02  # Assumed base spread

        # 1. Microstructure Liquidity Cap Check
        participation_rate = event.qty / bar_volume if bar_volume > 0 else 1.0

        # 2. Calculate Slippage based on Market Impact
        if participation_rate <= 0.05:
            # We are less than 5% of volume. Slippage is just half the spread.
            slippage = spread / 2
        else:
            # We are eating the book. Exponential penalty.
            penalty = (participation_rate - 0.05) * 0.50
            slippage = (spread / 2) + penalty
            logger.debug(f'High market impact! Penalty: ${penalty:.3f} per share.')

        # Adjust fill price based on buy/sell
        if event.order_type == 'BUY':
            fill_price = current_price + slippage
        else:
            fill_price = current_price - slippage

        # Flat rate commission model (e.g., $0.005 per share)
        commission = event.qty * 0.005

        # Emit the Fill Event back to the queue
        fill_event = FillEvent(event.symbol, event.qty, fill_price, commission)
        self.events.put(fill_event)


# --- 2. THE CORE ENGINE ---
class EventDrivenBacktester:
    def __init__(self, data_file: str):
        self.events = queue.Queue()
        # Load data fast using Polars (Assume it's 1-minute OR tick data)
        self.data = pl.read_parquet(data_file) if data_file.endswith('.parquet') else pl.read_csv(data_file)
        self.current_positions: Dict[str, int] = {}
        self.equity_curve = []

        # Initialize Handlers
        self.strategy = StrategyHandler(self.events)
        self.portfolio = PortfolioHandler(self.events)
        self.broker = ExecutionHandler(self.events)

    def run(self):
        """The Drip-Feed Loop"""
        logger.info('Starting Event-Driven Backtest...')

        # Convert dataframe to dictionaries for fast iteration
        for row in tqdm(self.data.iter_rows(named=True), total=len(self.data)):
            # 1. Drip feed one bar/tick of data
            market_event = MarketEvent(row['symbol'], row['timestamp'], row)
            self.events.put(market_event)

            # 2. Process all cascading events triggered by this tick
            while True:
                try:
                    event = self.events.get(False)
                except queue.Empty:
                    break  # Queue is empty, move to next tick

                if event.type == 'MARKET':
                    # Strategy sees new price, decides to emit SIGNAL
                    self.strategy.on_market_data(event)
                    # Broker updates open limit/stop orders based on new price
                    self.broker.on_market_data(event)

                elif event.type == 'SIGNAL':
                    # Portfolio sees SIGNAL, applies Kelly sizing, emits ORDER
                    self.portfolio.on_signal(event)

                elif event.type == 'ORDER':
                    # Broker receives ORDER, checks liquidity, emits FILL
                    self.broker.on_order(event)

                elif event.type == 'FILL':
                    # Portfolio receives FILL, updates cash and positions
                    self.portfolio.on_fill(event)

        logger.info('Backtest Complete. Calculating Metrics.')
        self.portfolio.print_summary()
