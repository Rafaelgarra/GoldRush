from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime
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