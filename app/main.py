from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List

from app import models, schemas, services
from app.database import engine, get_db

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="GoldRush API - Neon Edition")

origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "online", "db": "Postgres (Neon)"}

@app.get("/api/price/{symbol}", response_model=schemas.PriceSuggestion)
def get_price(symbol: str):
    result = services.get_current_asset_price(symbol)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

@app.post("/api/portfolio/add", response_model=schemas.TransactionResponse)
def add_asset(transaction: schemas.TransactionCreate, db: Session = Depends(get_db)):
    db_transaction = models.Transaction(**transaction.dict())
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    return db_transaction

@app.get("/api/portfolio", response_model=List[schemas.TransactionResponse])
def list_assets(db: Session = Depends(get_db)):
    return db.query(models.Transaction).all()

@app.post("/api/simulation", response_model=schemas.SimulationResponse)
def simulate_growth(request: schemas.SimulationRequest):
    result = services.calculate_simulation(request)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@app.delete("/api/portfolio/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(models.Transaction).filter(models.Transaction.id == asset_id).first()
    
    if not asset:
        raise HTTPException(status_code=404, detail="Ativo não encontrado")
    
    db.delete(asset)
    db.commit()
    return {"message": "Ativo deletado com sucesso"}