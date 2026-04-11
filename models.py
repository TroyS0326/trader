from dataclasses import dataclass, asdict
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
    bankroll = db.Column(db.Float, nullable=False, default=0.0)
    risk_pct = db.Column(db.Float, nullable=False, default=1.0)
    _alpaca_access_token = db.Column('alpaca_access_token', db.Text, nullable=True)
    alpaca_account_id = db.Column(db.String(100), nullable=True)

    @property
    def alpaca_access_token(self) -> Optional[str]:
        if not self._alpaca_access_token:
            return None
        try:
            return TOKEN_CIPHER.decrypt(self._alpaca_access_token.encode('utf-8')).decode('utf-8')
        except (InvalidToken, ValueError, TypeError):
            return self._alpaca_access_token

    @alpaca_access_token.setter
    def alpaca_access_token(self, token: Optional[str]) -> None:
        if not token:
            self._alpaca_access_token = None
            return
        self._alpaca_access_token = TOKEN_CIPHER.encrypt(token.encode('utf-8')).decode('utf-8')
