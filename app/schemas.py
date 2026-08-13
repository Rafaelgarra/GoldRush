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


# ─── Sell / Realized P&L ─────────────────────────────────────

class SellRequest(BaseModel):
    quantity_sold: float = Field(..., gt=0)
    sell_price: float = Field(..., gt=0)


class SoldPositionResponse(BaseModel):
    id: int
    symbol: str
    asset_type: Optional[str] = None
    currency: str
    quantity_sold: float
    avg_price_paid: float
    sell_price: float
    realized_profit: float
    sell_date: datetime

    class Config:
        from_attributes = True


class CurrencyPnL(BaseModel):
    realized_profit: float
    capital_returned: float

class RealizedPnLResponse(BaseModel):
    total_sales: int
    winning_trades: int
    losing_trades: int
    by_currency: Dict[str, CurrencyPnL]


# ─── Watchlist ───────────────────────────────────────────────

class WatchlistCreate(BaseModel):
    symbol: str
    asset_type: str = "stock"
    currency: str = "BRL"


class WatchlistResponse(WatchlistCreate):
    id: int
    added_at: datetime
    price: Optional[float] = None
    changePercent: Optional[float] = None

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
    accumulated_dividends: float = 0.0
    monthly_dividends: float = 0.0
    monthly_dividend_per_share: float = 0.0
    accumulated_shares: float = 0.0


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


# ─── AI Advisor ──────────────────────────────────────────────

class AssetAnalysis(BaseModel):
    symbol: str
    rating: str
    alert_flag: Optional[str] = None
    reasoning: str

class TradingOpportunity(BaseModel):
    symbol: str
    sector: str
    current_price_ref: str
    target_price: str
    upside_potential: str
    risk_level: str   # "Alto", "Muito Alto"
    pros: List[str]
    cons: List[str]
    thesis: str       # tese de investimento em 1 parágrafo

class DividendOpportunity(BaseModel):
    symbol: str
    sector: str
    estimated_dy: str   # ex: "13.5%"
    safety_score: str   # "Alta", "Média-Alta"
    niche: str          # ex: "Energia Elétrica", "FII Logística"
    reasoning: str
    portfolio_fit: str  # como equilibra com o portfólio atual

class PortfolioBalance(BaseModel):
    assessment: str     # análise do equilíbrio atual
    rebalance_actions: List[str]  # ações sugeridas pra rebalancear

class AIAnalysisResponse(BaseModel):
    health_score: int
    market_comparison: str
    risk_assessment: str
    dividend_analysis: str
    assets_analysis: List[AssetAnalysis]
    trading_opportunities: List[TradingOpportunity]
    dividend_opportunities: List[DividendOpportunity]
    portfolio_balance: PortfolioBalance
    suggestions: List[str]