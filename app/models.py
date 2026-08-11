from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Float, BigInteger, Date, UniqueConstraint, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    provider = Column(String, default="email")
    avatar_url = Column(String, nullable=True)

    assets = relationship("Asset", back_populates="owner")
    watchlist = relationship("Watchlist", back_populates="owner")


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)

    quantity_enc = Column(String)
    price_paid_enc = Column(String)

    asset_type = Column(String)
    currency = Column(String, default="BRL")
    purchase_date = Column(DateTime, default=datetime.utcnow)

    owner_id = Column(Integer, ForeignKey("users.id"))
    owner = relationship("User", back_populates="assets")


class Watchlist(Base):
    """Ativos que o usuário quer monitorar sem necessariamente possuir."""
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String, index=True, nullable=False)
    asset_type = Column(String, default="stock")
    currency = Column(String, default="BRL")
    added_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="watchlist")

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),
    )


class PriceHistory(Base):
    """
    Histórico de preços EOD (End-of-Day) por símbolo.
    Populado pelo collector.py após o fechamento de cada bolsa.
    Acumula indefinidamente — base para cálculos históricos futuros.
    """
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)

    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=False)
    volume = Column(BigInteger, nullable=True)
    dividends = Column(Float, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_price_history_symbol_date"),
    )


class PriceCache(Base):
    """
    Cache persistente do último preço conhecido de cada símbolo.
    Serve de fallback quando o cache in-memory é perdido (restart do servidor).
    """
    __tablename__ = "price_cache"

    symbol = Column(String, primary_key=True)
    price = Column(Float, nullable=False)
    source = Column(String, default="yfinance")  # mt5 | yfinance | eod_collector
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AIAnalysisCache(Base):
    """
    Persiste a última análise gerada pelo Gemini para cada usuário.
    A análise é válida apenas no dia de criação (date_generated == hoje).
    """
    __tablename__ = "ai_analysis_cache"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    analysis_json = Column(Text, nullable=False)
    date_generated = Column(Date, nullable=False, default=datetime.utcnow)