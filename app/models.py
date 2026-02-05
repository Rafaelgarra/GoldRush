from sqlalchemy import Column, Integer, String, Float, Date, DateTime
from datetime import datetime
from app.database import Base

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    quantity = Column(Float)
    price_paid = Column(Float)
    purchase_date = Column(Date)
    currency = Column(String, default="BRL")
    asset_type = Column(String, default="Ação")
    created_at = Column(DateTime, default=datetime.utcnow)