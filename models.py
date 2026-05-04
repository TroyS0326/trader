from dataclasses import dataclass, asdict
from datetime import datetime
import base64
import hashlib
import os
from typing import Dict, Any, List, Optional

from cryptography.fernet import Fernet, InvalidToken
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _build_fernet() -> Fernet:
    secret_seed = os.getenv('TOKEN_ENCRYPTION_KEY') or os.getenv('SECRET_KEY', 'change-me')
    digest = hashlib.sha256(secret_seed.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


TOKEN_CIPHER = _build_fernet()

@dataclass
class ScoreTriplet:
    opportunity: int
    tradability: int
    entry_quality: int

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)

@dataclass
class SymbolMarketStats:
    symbol: str
    price: float
    daily_dollar_volume: float
    spread_pct: float

@dataclass
class ComponentScores:
    catalyst: int
    liquidity: int
    daily_chart_alignment: int
    sector_sympathy: int
    open_relative_strength: int
    vwap_hold_reclaim: int
    first_pullback: int
    entry_quality: int
    opening_range_confirmation: int

@dataclass
class WatchPanelDef:
    label: str
    buy_after: str
    buy_range: List[float]
    max_shares: int
    stop: float
    take_profit_range: List[float]
    max_dollar_loss: float
    opening_range: List[Optional[float]]
    vwap: Optional[float]
    status: str
    setup_grade: str

@dataclass
class SymbolAnalysisResult:
    symbol: str
    score_total: int
    decision: str
    current_price: float
    buy_lower: float
    buy_upper: float
    entry_price: float
    stop_price: float
    target_1: float
    target_2: float
    qty: int
    risk_per_share: float
    max_dollar_loss: float
    buying_power_used: float
    rr_ratio_1: float
    rr_ratio_2: float
    score_models: Dict[str, int]
    scores: ComponentScores
    details: Dict[str, Any]
    setup_grade: str
    watch_panel: WatchPanelDef
    buy_window_open: bool
    opening_range_complete: bool
    breakout_confirmed: bool

    def to_dict(self) -> Dict[str, Any]:
        """Converts the dataclass back to a dict for Flask jsonify and SQLite storage."""
        return asdict(self)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(50), nullable=True)
    zip_code = db.Column(db.String(20), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    subscription_status = db.Column(db.String(50), nullable=False, default='free')
    stripe_customer_id = db.Column(db.String(255), nullable=True, index=True)
    stripe_subscription_id = db.Column(db.String(255), nullable=True, index=True)
    stripe_price_id = db.Column(db.String(255), nullable=True)
    subscription_plan = db.Column(db.String(50), nullable=True)
    subscription_current_period_end = db.Column(db.DateTime, nullable=True)
    subscription_cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    trading_mode = db.Column(db.String(20), nullable=False, default='paper')
    bankroll = db.Column(db.Float, nullable=False, default=0.0)
    risk_pct = db.Column(db.Float, nullable=False, default=1.0)
    refresh_interval = db.Column(db.Integer, nullable=False, default=30000)
    show_news = db.Column(db.Boolean, nullable=False, default=True)
    show_watchlist = db.Column(db.Boolean, nullable=False, default=True)
    show_terminal = db.Column(db.Boolean, nullable=False, default=True)
    esg_fossil_fuels = db.Column(db.Boolean, nullable=False, default=False)
    esg_weapons = db.Column(db.Boolean, nullable=False, default=False)
    esg_tobacco = db.Column(db.Boolean, nullable=False, default=False)
    exclude_penny_stocks = db.Column(db.Boolean, nullable=False, default=True)
    exclude_biotech = db.Column(db.Boolean, nullable=False, default=False)
    # Legacy fields kept for backward compatibility
    _alpaca_access_token = db.Column('alpaca_access_token', db.Text, nullable=True)
    alpaca_account_id = db.Column(db.String(100), nullable=True)

    # Separate Alpaca connections
    _alpaca_paper_access_token = db.Column('alpaca_paper_access_token', db.Text, nullable=True)
    _alpaca_live_access_token = db.Column('alpaca_live_access_token', db.Text, nullable=True)

    alpaca_paper_account_id = db.Column(db.String(100), nullable=True)
    alpaca_live_account_id = db.Column(db.String(100), nullable=True)

    paper_bankroll = db.Column(db.Float, nullable=False, default=0.0)
    live_bankroll = db.Column(db.Float, nullable=False, default=0.0)

    alpaca_data_feed = db.Column(db.String(10), nullable=False, default='iex')

    onboarding_completed = db.Column(db.Boolean, nullable=False, default=False)
    paper_bankroll_set = db.Column(db.Boolean, nullable=False, default=False)
    first_scan_completed = db.Column(db.Boolean, nullable=False, default=False)
    scan_preview_completed = db.Column(db.Boolean, nullable=False, default=False)
    playbook_reviewed = db.Column(db.Boolean, nullable=False, default=False)
    transparency_reviewed = db.Column(db.Boolean, nullable=False, default=False)
    broker_connection_started = db.Column(db.Boolean, nullable=False, default=False)

    def _decrypt_token_value(self, encrypted_value: Optional[str]) -> Optional[str]:
        if not encrypted_value:
            return None
        try:
            return TOKEN_CIPHER.decrypt(encrypted_value.encode('utf-8')).decode('utf-8')
        except (InvalidToken, ValueError, TypeError):
            return None


    def _encrypt_token_value(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        return TOKEN_CIPHER.encrypt(token.encode('utf-8')).decode('utf-8')


    @property
    def alpaca_paper_access_token(self) -> Optional[str]:
        return self._decrypt_token_value(self._alpaca_paper_access_token)


    @alpaca_paper_access_token.setter
    def alpaca_paper_access_token(self, token: Optional[str]) -> None:
        self._alpaca_paper_access_token = self._encrypt_token_value(token)


    @property
    def alpaca_live_access_token(self) -> Optional[str]:
        return self._decrypt_token_value(self._alpaca_live_access_token)


    @alpaca_live_access_token.setter
    def alpaca_live_access_token(self, token: Optional[str]) -> None:
        self._alpaca_live_access_token = self._encrypt_token_value(token)


    @property
    def alpaca_access_token(self) -> Optional[str]:
        """
        Active token based on selected trading mode.

        Live mode strictly uses the live token; paper mode strictly uses the paper token.
        """
        if getattr(self, 'trading_mode', 'paper') == 'live':
            return self.alpaca_live_access_token

        return self.alpaca_paper_access_token


    @alpaca_access_token.setter
    def alpaca_access_token(self, token: Optional[str]) -> None:
        """
        Legacy setter.

        If old code writes current_user.alpaca_access_token, save it to the active mode.
        """
        if getattr(self, 'trading_mode', 'paper') == 'live':
            self.alpaca_live_access_token = token
        else:
            self.alpaca_paper_access_token = token

        # Keep legacy field populated for backward compatibility.
        self._alpaca_access_token = self._encrypt_token_value(token)


    @property
    def active_alpaca_account_id(self) -> Optional[str]:
        if getattr(self, 'trading_mode', 'paper') == 'live':
            return self.alpaca_live_account_id or self.alpaca_account_id
        return self.alpaca_paper_account_id or self.alpaca_account_id


    @property
    def active_bankroll(self) -> float:
        if getattr(self, 'trading_mode', 'paper') == 'live':
            return float(self.live_bankroll or 0.0)
        return float(self.paper_bankroll or 0.0)


    def sync_legacy_bankroll_from_active_mode(self) -> None:
        """
        Keeps current_user.bankroll compatible with old templates/calculations.
        """
        self.bankroll = self.active_bankroll


class Waitlist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_early_bird = db.Column(db.Boolean, default=False)


class Trade(db.Model):
    __tablename__ = 'trades'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    scan_id = db.Column(db.Integer, nullable=True)

    symbol = db.Column(db.String(10), nullable=False, index=True)
    side = db.Column(db.String(10), nullable=False, default='buy')
    decision = db.Column(db.String(20), nullable=False, default='BUY NOW')
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)

    score_total = db.Column(db.Integer, nullable=True)
    current_price = db.Column(db.Float, nullable=True)
    entry_price = db.Column(db.Float, nullable=False)
    buy_lower = db.Column(db.Float, nullable=True)
    buy_upper = db.Column(db.Float, nullable=True)
    stop_price = db.Column(db.Float, nullable=False)
    target_1 = db.Column(db.Float, nullable=False)
    target_2 = db.Column(db.Float, nullable=False)
    qty = db.Column(db.Integer, nullable=True)

    risk_per_share = db.Column(db.Float, nullable=True)
    reward_to_target_1 = db.Column(db.Float, nullable=True)
    reward_to_target_2 = db.Column(db.Float, nullable=True)
    rr_ratio_1 = db.Column(db.Float, nullable=True)
    rr_ratio_2 = db.Column(db.Float, nullable=True)

    order_id = db.Column(db.String(128), nullable=True, index=True)
    order_status = db.Column(db.String(32), nullable=True)
    filled_avg_price = db.Column(db.Float, nullable=True)
    filled_qty = db.Column(db.Float, nullable=True)

    # Realized performance tracking
    exit_price = db.Column(db.Float, nullable=True)
    pnl = db.Column(db.Float, nullable=True)
    pnl_source = db.Column(db.String(64), nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)

    outcome = db.Column(db.String(32), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)

    user = db.relationship('User', backref=db.backref('trades', lazy=True))


class Scan(db.Model):
    __tablename__ = 'scans'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    market_day = db.Column(db.String(20), nullable=True)
    best_symbol = db.Column(db.String(10), nullable=True)
    best_decision = db.Column(db.String(20), nullable=True)
    best_score = db.Column(db.Integer, nullable=True)
    payload_json = db.Column(db.Text, nullable=False)


class MarketRegime(db.Model):
    __tablename__ = 'market_regimes'

    id = db.Column(db.Integer, primary_key=True)
    vix_value = db.Column(db.Float, nullable=True)
    spy_trend = db.Column(db.String(50), nullable=True)
    regime_status = db.Column(db.String(50), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class UserEvent(db.Model):
    __tablename__ = 'user_events'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    event_name = db.Column(db.String(80), nullable=False, index=True)
    event_context = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
