from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import yfinance as yf
import math
import os
from typing import List
from datetime import date, timedelta
from dotenv import load_dotenv

from app import models, schemas, database, security
from app.price_service import fetch_prices, fetch_single_price, get_cache_info
from app.collector import collect_eod_prices, collect_crypto_prices
from app.mt5_service import is_mt5_available, shutdown_mt5

load_dotenv()

# --- Cria tabelas (se não existirem) ---
models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="GoldRush API")

# --- Session middleware (Google OAuth) ---
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "uma_chave_secreta_provisoria"),
)

# --- CORS ---
origins = [
    "http://localhost:3000",
    "https://goldrush-web.vercel.app",
    os.getenv("FRONTEND_URL", "http://localhost:3000"),
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Google OAuth ---
oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# --- APScheduler (cron de coleta EOD) ---
scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")

# B3: coleta após fechamento às 18:00 BRT (seg–sex)
scheduler.add_job(
    collect_eod_prices,
    trigger="cron",
    hour=18,
    minute=0,
    day_of_week="mon-fri",
    kwargs={"market": "BRL"},
    id="eod_brl",
    name="Coleta EOD B3",
    replace_existing=True,
)

# NYSE: coleta às 22:30 BRT (seg–sex)
scheduler.add_job(
    collect_eod_prices,
    trigger="cron",
    hour=22,
    minute=30,
    day_of_week="mon-fri",
    kwargs={"market": "USD"},
    id="eod_usd",
    name="Coleta EOD NYSE",
    replace_existing=True,
)

# Cripto: a cada 4 horas (mercado 24/7)
scheduler.add_job(
    collect_crypto_prices,
    trigger="interval",
    hours=4,
    id="crypto_update",
    name="Atualização Cripto",
    replace_existing=True,
)


@app.on_event("startup")
def startup():
    scheduler.start()
    mt5_status = "✅ conectado" if is_mt5_available() else "⚠️ não disponível (usando yfinance)"
    print(f"[GoldRush] API iniciada | MT5: {mt5_status} | Scheduler: ativo")


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)
    shutdown_mt5()
    print("[GoldRush] API encerrada.")


# ─── DEPENDÊNCIAS ────────────────────────────────────────────

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_current_user(
    token: str = Depends(security.oauth2_scheme),
    db: Session = Depends(get_db),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = security.jwt.decode(
            token, security.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except security.JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


# =======================
# 🔐 AUTENTICAÇÃO
# =======================

@app.post("/api/register", response_model=schemas.Token)
def register(user: schemas.UserRegister, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Email já cadastrado")

    hashed_pwd = security.get_password_hash(user.password)
    new_user = models.User(
        email=user.email,
        hashed_password=hashed_pwd,
        is_active=True,
        provider="email",
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    access_token = security.create_access_token(data={"sub": new_user.email})
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/api/token", response_model=schemas.Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if not user or not user.hashed_password or not security.verify_password(
        form_data.password, user.hashed_password
    ):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")

    access_token = security.create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/auth/google")
async def login_google(request: Request):
    redirect_uri = request.url_for("auth_google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo")
        email = user_info.email

        user = db.query(models.User).filter(models.User.email == email).first()
        if not user:
            user = models.User(
                email=email,
                is_active=True,
                provider="google",
                avatar_url=user_info.picture,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        access_token = security.create_access_token(data={"sub": user.email})
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=f"{frontend_url}/auth/callback?token={access_token}")

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro Login Google: {e}")


# =======================
# 💰 PORTFÓLIO (PROTEGIDO)
# =======================

@app.get("/api/portfolio", response_model=List[schemas.AssetResponse])
def get_portfolio(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assets_enc = (
        db.query(models.Asset)
        .filter(models.Asset.owner_id == current_user.id)
        .all()
    )

    results = []
    for asset in assets_enc:
        try:
            qtd = float(security.decrypt_data(asset.quantity_enc))
            price = float(security.decrypt_data(asset.price_paid_enc))
            results.append({
                "id": asset.id,
                "symbol": asset.symbol,
                "quantity": qtd,
                "price_paid": price,
                "asset_type": asset.asset_type,
                "currency": asset.currency,
                "purchase_date": asset.purchase_date,
            })
        except Exception as e:
            print(f"Erro decriptação asset {asset.id}: {e}")
            continue

    return results


@app.post("/api/portfolio")
def add_asset(
    asset: schemas.AssetCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    qtd_enc = security.encrypt_data(str(asset.quantity))
    price_enc = security.encrypt_data(str(asset.price_paid))

    new_asset = models.Asset(
        symbol=asset.symbol,
        quantity_enc=qtd_enc,
        price_paid_enc=price_enc,
        asset_type=asset.asset_type,
        currency=asset.currency,
        owner_id=current_user.id,
    )
    db.add(new_asset)
    db.commit()
    return {"message": "Ativo protegido e salvo!"}


@app.delete("/api/portfolio/{asset_id}")
def delete_asset(
    asset_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    asset = db.query(models.Asset).filter(models.Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Ativo não encontrado")
    if asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Sem permissão")

    db.delete(asset)
    db.commit()
    return {"message": "Ativo removido com sucesso"}


@app.put("/api/portfolio/{asset_id}")
def update_asset(
    asset_id: int,
    asset_update: schemas.AssetCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    asset = db.query(models.Asset).filter(models.Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Ativo não encontrado")
    if asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Sem permissão")

    asset.symbol = asset_update.symbol
    asset.quantity_enc = security.encrypt_data(str(asset_update.quantity))
    asset.price_paid_enc = security.encrypt_data(str(asset_update.price_paid))
    asset.asset_type = asset_update.asset_type
    asset.currency = asset_update.currency

    db.commit()
    return {"message": "Ativo atualizado com sucesso"}


# =======================
# 📋 WATCHLIST (PROTEGIDO)
# =======================

@app.get("/api/watchlist", response_model=List[schemas.WatchlistResponse])
def get_watchlist(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(models.Watchlist)
        .filter(models.Watchlist.user_id == current_user.id)
        .all()
    )
    return items


@app.post("/api/watchlist", response_model=schemas.WatchlistResponse)
def add_to_watchlist(
    item: schemas.WatchlistCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(models.Watchlist)
        .filter(
            models.Watchlist.user_id == current_user.id,
            models.Watchlist.symbol == item.symbol.upper(),
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="Ativo já está na watchlist")

    new_item = models.Watchlist(
        user_id=current_user.id,
        symbol=item.symbol.upper(),
        asset_type=item.asset_type,
        currency=item.currency,
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return new_item


@app.delete("/api/watchlist/{item_id}")
def remove_from_watchlist(
    item_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.query(models.Watchlist).filter(models.Watchlist.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado")
    if item.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Sem permissão")

    db.delete(item)
    db.commit()
    return {"message": "Removido da watchlist"}


# =======================
# 🌍 PREÇOS (PÚBLICOS)
# =======================

@app.get("/api/price/{ticker}")
def get_price(ticker: str, db: Session = Depends(get_db)):
    """
    Preço de um único ativo.
    Usa hierarquia: cache → banco → MT5 → yfinance.
    """
    # Trata ticker para o formato correto
    symbol = ticker.upper()
    if symbol in ["BTC", "ETH"]:
        symbol = f"{symbol}-USD"
    elif not symbol.endswith(".SA") and not "-" in symbol and "BRL=X" not in symbol:
        # Tenta com .SA primeiro se não for explicitamente internacional
        pass

    price = fetch_single_price(symbol, db)

    # Fallback: tenta com .SA para ações BR
    if price is None and not symbol.endswith(".SA") and "-" not in symbol:
        symbol_br = symbol + ".SA"
        price = fetch_single_price(symbol_br, db)
        if price:
            symbol = symbol_br

    if price is None:
        return {"error": "Ticker não encontrado", "symbol": ticker}

    return {"symbol": symbol, "current_price": round(price, 4)}


@app.get("/api/portfolio/prices")
def get_portfolio_prices(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna preços atuais de TODOS os ativos do portfólio + watchlist em 1 chamada.
    O frontend deve usar este endpoint em vez de N chamadas individuais.
    """
    assets = (
        db.query(models.Asset)
        .filter(models.Asset.owner_id == current_user.id)
        .all()
    )
    watchlist = (
        db.query(models.Watchlist)
        .filter(models.Watchlist.user_id == current_user.id)
        .all()
    )

    symbols = list(set(
        [a.symbol for a in assets] + [w.symbol for w in watchlist]
    ))

    if not symbols:
        return {}

    prices = fetch_prices(symbols, db)
    return prices


@app.get("/api/price/{ticker}/history")
def get_price_history(
    ticker: str,
    period: str = "1y",
    db: Session = Depends(get_db),
):
    """
    Histórico EOD do banco local.
    period: 1m | 3m | 6m | 1y | 2y | 5y
    """
    days_map = {
        "1m": 30, "3m": 90, "6m": 180,
        "1y": 365, "2y": 730, "5y": 1825,
    }
    days = days_map.get(period, 365)
    start = date.today() - timedelta(days=days)
    symbol = ticker.upper()

    history = (
        db.query(models.PriceHistory)
        .filter(
            models.PriceHistory.symbol == symbol,
            models.PriceHistory.date >= start,
        )
        .order_by(models.PriceHistory.date.asc())
        .all()
    )

    # Se não tem no banco, busca do yfinance e salva
    if not history:
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period=period, auto_adjust=True)
            if not hist.empty:
                return [
                    {
                        "date": str(dt.date()),
                        "close": round(float(row["Close"]), 4),
                        "open": round(float(row["Open"]), 4) if "Open" in row else None,
                        "high": round(float(row["High"]), 4) if "High" in row else None,
                        "low": round(float(row["Low"]), 4) if "Low" in row else None,
                    }
                    for dt, row in hist.iterrows()
                    if not math.isnan(float(row["Close"]))
                ]
        except Exception:
            pass
        return []

    return [
        {
            "date": str(h.date),
            "close": h.close,
            "open": h.open,
            "high": h.high,
            "low": h.low,
            "volume": h.volume,
        }
        for h in history
    ]


@app.get("/api/market-summary")
def get_market_summary(currency: str = "BRL", db: Session = Depends(get_db)):
    """Cotações resumidas (Dólar, Euro, BTC, Yuan, Iene, Libra)."""
    if currency == "BRL":
        tickers_map = {
            "Dólar": "BRL=X",
            "Euro": "EURBRL=X",
            "Bitcoin": "BTC-USD",
            "Yuan": "CNYBRL=X",
            "Iene": "JPYBRL=X",
            "Libra": "GBPBRL=X",
        }
    else:
        tickers_map = {
            "Euro": "EURUSD=X",
            "Bitcoin": "BTC-USD",
            "Real": "BRL=X",
            "Iene": "JPY=X",
            "Yuan": "CNY=X",
            "Libra": "GBPUSD=X",
        }

    symbols = list(tickers_map.values())
    prices = fetch_prices(symbols, db)

    data = []
    for name, ticker in tickers_map.items():
        price = prices.get(ticker)
        if price is None:
            continue

        # Bitcoin vem em USD → converte para BRL
        if name == "Bitcoin" and currency == "BRL":
            dolar = prices.get("BRL=X", 5.80)
            price = price * dolar

        data.append({
            "name": name,
            "ticker": ticker,
            "price": round(price, 4) if price < 1 else round(price, 2),
            "currency": currency,
        })

    return data


# =======================
# 🔮 SIMULADOR
# =======================

@app.post("/api/simulation")
def simulate_future(data: schemas.SimulationRequest):
    if not data.symbol:
        return {"error": "Símbolo obrigatório"}

    try:
        ticker = data.symbol.upper().strip()
        if data.currency == "BRL" and not ticker.endswith(".SA") and "-" not in ticker:
            ticker += ".SA"

        start_date = data.start_date if data.start_date else "2020-01-01"
        stock = yf.Ticker(ticker)
        hist = stock.history(start=start_date, auto_adjust=False)

        if hist.empty:
            hist = stock.history(period="5y", auto_adjust=False)
            if hist.empty:
                raise HTTPException(status_code=404, detail=f"Sem dados para {ticker}")

        shares = 0
        cash = float(data.initial_investment or 0)
        total_invested = float(data.initial_investment or 0)
        monthly_contribution = float(data.monthly_contribution or 0)
        total_dividends = 0.0
        history = []
        last_month_processed = -1

        for date_idx, row in hist.iterrows():
            price = float(row["Close"])
            divs = float(row["Dividends"]) if "Dividends" in row else 0.0

            if shares > 0 and divs > 0:
                dividend_payout = shares * divs
                total_dividends += dividend_payout
                if data.reinvest_dividends:
                    cash += dividend_payout

            if date_idx.month != last_month_processed:
                if last_month_processed != -1:
                    cash += monthly_contribution
                    total_invested += monthly_contribution
                last_month_processed = date_idx.month

            if cash >= price and price > 0:
                can_buy = int(cash // price)
                if can_buy > 0:
                    shares += can_buy
                    cash -= can_buy * price

            if date_idx.is_month_end:
                total_equity = (shares * price) + cash
                history.append({
                    "month": date_idx.strftime("%Y-%m"),
                    "invested": round(total_invested, 2),
                    "total": round(total_equity, 2),
                    "price": round(price, 2),
                })

        final_price = float(hist["Close"].iloc[-1])
        final_equity = (shares * final_price) + cash

        return {
            "symbol": ticker,
            "total_invested": round(total_invested, 2),
            "final_portfolio_value": round(final_equity, 2),
            "final_unit_price": round(final_price, 2),
            "final_accumulated_shares": round(shares, 2),
            "total_dividends": round(total_dividends, 2),
            "history": history,
        }

    except Exception as e:
        print(f"Erro Backtest: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# =======================
# 🛠️ UTILITÁRIOS
# =======================

@app.get("/api/status")
def api_status():
    """Health check + status dos serviços."""
    return {
        "status": "ok",
        "mt5_available": is_mt5_available(),
        "scheduler_running": scheduler.running,
        "scheduled_jobs": [
            {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
            for j in scheduler.get_jobs()
        ],
    }


@app.get("/api/cache/info")
def cache_info(current_user: models.User = Depends(get_current_user)):
    """Retorna estado atual do cache in-memory (debug)."""
    return get_cache_info()