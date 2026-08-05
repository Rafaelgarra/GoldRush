from pydantic import BaseModel, Field, EmailStr
from datetime import date, datetime
from typing import List, Optional


# ─── Auth ────────────────────────────────────────────────────

class UserRegister(BaseModel):
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


# ─── Assets / Portfolio ──────────────────────────────────────

class AssetCreate(BaseModel):
    symbol: str
    quantity: float
    price_paid: float
    asset_type: str
    currency: str = "BRL"


class AssetResponse(AssetCreate):
    id: int
    purchase_date: datetime

    class Config:
        from_attributes = True


# ─── Watchlist ───────────────────────────────────────────────

class WatchlistCreate(BaseModel):
    symbol: str
    asset_type: str = "stock"
    currency: str = "BRL"


class WatchlistResponse(WatchlistCreate):
    id: int
    added_at: datetime

    class Config:
        from_attributes = True


# ─── Price History ───────────────────────────────────────────

class PriceHistoryPoint(BaseModel):
    date: str
    close: float
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[int] = None


# ─── Simulation ──────────────────────────────────────────────

class SimulationRequest(BaseModel):
    """
    Backtest real: simula compra periódica de um ativo desde start_date.
    """
    symbol: Optional[str] = None
    initial_investment: Optional[float] = None
    monthly_contribution: float = 0
    start_date: Optional[str] = None
    reinvest_dividends: bool = True
    currency: str = "BRL"


class SimulationHistoryPoint(BaseModel):
    month: str
    invested: float
    total: float
    price: float


class SimulationResponse(BaseModel):
    symbol: str
    total_invested: float
    final_portfolio_value: float
    final_accumulated_shares: float
    final_unit_price: float
    total_dividends: float = 0.0
    history: List[SimulationHistoryPoint]


# ─── Misc (mantido por compatibilidade) ─────────────────────

class TransactionCreate(BaseModel):
    symbol: str
    quantity: float = Field(..., gt=0)
    price_paid: float = Field(..., gt=0)
    purchase_date: date
    asset_type: str = "Ação"
    currency: str = "BRL"


class PriceSuggestion(BaseModel):
    symbol: str
    current_price: float
    currency: Optional[str] = "BRL"
    timestamp: Optional[datetime] = None