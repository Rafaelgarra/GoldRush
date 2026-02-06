from pydantic import BaseModel, Field, EmailStr
from datetime import date, datetime
from typing import List, Optional

class TransactionCreate(BaseModel):
    symbol: str
    quantity: float = Field(..., gt=0)
    price_paid: float = Field(..., gt=0)
    purchase_date: date
    asset_type: str = "Ação"
    currency: str = "BRL"

class TransactionResponse(TransactionCreate):
    id: int
    created_at: datetime
    
    class Config:
        from_attributes = True

class PriceSuggestion(BaseModel):
    symbol: str
    current_price: float
    currency: Optional[str] = "BRL" 
    timestamp: Optional[datetime] = None

class SimulationRequest(BaseModel):
    symbol: str
    initial_investment: float = Field(..., gt=0)
    monthly_contribution: float = Field(..., ge=0)
    start_date: date
    reinvest_dividends: bool = True
    currency: str = "BRL"

class SimulationHistoryPoint(BaseModel):
    date: str
    portfolio_value: float
    total_invested: float

class SimulationResponse(BaseModel):
    symbol: str
    total_invested: float
    final_portfolio_value: float
    final_accumulated_shares: float
    final_unit_price: float
    roi_percentage: float
    history: List[SimulationHistoryPoint]

class UserRegister(BaseModel):
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

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

class SimulationRequest(BaseModel):
    # --- MODO 1: Simulação Matemática Simples ---
    initial_amount: Optional[float] = None
    interest_rate_yearly: Optional[float] = None
    years: Optional[int] = None

    # --- MODO 2: Backtest Real (O que deu erro) ---
    symbol: Optional[str] = None
    initial_investment: Optional[float] = None
    monthly_contribution: float = 0
    start_date: Optional[str] = None
    reinvest_dividends: bool = True
    currency: str = "BRL"