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
# Aceita qualquer origem para não bloquear previews do Vercel ou outros domínios do usuário
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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
# 🌐 HEALTH CHECK (RENDER)
# =======================

@app.get("/")
@app.head("/")
def read_root():
    return {"status": "ok", "app": "GoldRush API"}

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
    if not items:
        return []
        
    symbols = list(set([i.symbol for i in items]))
    prices = fetch_prices(symbols, db)
    
    changes = {}
    try:
        import yfinance as yf
        df = yf.download(symbols, period="5d", auto_adjust=True, progress=False)
        closes = df["Close"] if len(symbols) > 1 else df[["Close"]]
        if len(symbols) == 1:
            closes.columns = symbols
            
        for sym in symbols:
            if sym in closes:
                col = closes[sym].dropna()
                if len(col) >= 2:
                    prev_close = float(col.iloc[-2])
                    curr_close = float(col.iloc[-1])
                    change_pct = ((curr_close - prev_close) / prev_close) * 100
                    changes[sym] = round(change_pct, 2)
    except Exception as e:
        print(f"Erro ao buscar changes na watchlist: {e}")

    result = []
    for item in items:
        resp = schemas.WatchlistResponse.model_validate(item)
        resp.price = prices.get(item.symbol)
        resp.changePercent = changes.get(item.symbol)
        result.append(resp)
        
    return result

@app.get("/api/watchlist/news")
def get_watchlist_news(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna as últimas notícias dos ativos monitorados.
    """
    items = (
        db.query(models.Watchlist)
        .filter(models.Watchlist.user_id == current_user.id)
        .all()
    )
    if not items:
        return []
        
    symbols = list(set([i.symbol for i in items]))
    all_news = []
    import yfinance as yf
    from datetime import datetime, timedelta, timezone
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            news = ticker.news
            for n in news:
                content = n.get("content", {})
                if not content:
                    if "title" in n:
                        content = n
                    else:
                        continue
                        
                pub_date_str = content.get("pubDate", "")
                if pub_date_str:
                    try:
                        # Ex: 2026-08-10T20:40:36Z
                        pub_date = datetime.fromisoformat(pub_date_str.replace('Z', '+00:00'))
                        if pub_date < cutoff:
                            continue
                    except:
                        pass
                        
                # Tenta pegar link de onde estiver
                link = ""
                if content.get("clickThroughUrl") and isinstance(content["clickThroughUrl"], dict):
                    link = content["clickThroughUrl"].get("url", "")
                if not link and content.get("canonicalUrl") and isinstance(content["canonicalUrl"], dict):
                    link = content["canonicalUrl"].get("url", "")
                    
                provider = "Notícias"
                if content.get("provider") and isinstance(content["provider"], dict):
                    provider = content["provider"].get("displayName", "Notícias")

                all_news.append({
                    "symbol": sym,
                    "title": content.get("title", ""),
                    "summary": content.get("summary", ""),
                    "pubDate": pub_date_str,
                    "link": link,
                    "provider": provider,
                })
        except Exception as e:
            print(f"Erro buscar news para {sym}: {e}")
            
    # sort by pubDate desc
    all_news.sort(key=lambda x: x["pubDate"], reverse=True)
    
    # deduplicate by title
    seen = set()
    dedup = []
    for n in all_news:
        if n["title"] not in seen and n["title"]:
            seen.add(n["title"])
            dedup.append(n)
            
    return dedup[:15]



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


@app.get("/api/portfolio/summary")
def get_portfolio_summary(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna os ativos da carteira com preço atual e variação diária (para top gainers/losers).
    """
    assets = db.query(models.Asset).filter(models.Asset.owner_id == current_user.id).all()
    if not assets:
        return {"assets": [], "top_gainer": None, "top_loser": None}

    symbols = list(set([a.symbol for a in assets]))
    
    # Adicionando moedas para buscar as cotações
    currency_tickers = ["BRL=X", "EURBRL=X", "JPYBRL=X", "CNYBRL=X"]
    all_symbols_to_fetch = symbols + currency_tickers
    prices = fetch_prices(all_symbols_to_fetch, db)
    
    rates = {
        "USD": prices.get("BRL=X", 5.8),
        "EUR": prices.get("EURBRL=X", 6.2),
        "JPY": prices.get("JPYBRL=X", 0.04),
        "CNY": prices.get("CNYBRL=X", 0.8),
        "BRL": 1.0
    }
    
    changes = {}
    try:
        import yfinance as yf
        df = yf.download(symbols, period="5d", auto_adjust=True, progress=False)
        closes = df["Close"] if len(symbols) > 1 else df[["Close"]]
        if len(symbols) == 1:
            closes.columns = symbols
            
        for sym in symbols:
            if sym in closes:
                col = closes[sym].dropna()
                if len(col) >= 2:
                    prev_close = float(col.iloc[-2])
                    curr_close = float(col.iloc[-1])
                    change_pct = ((curr_close - prev_close) / prev_close) * 100
                    changes[sym] = round(change_pct, 2)
    except Exception as e:
        print(f"Erro ao buscar changes no summary: {e}")

    enriched = []
    for a in assets:
        try:
            qtd = float(security.decrypt_data(a.quantity_enc))
            price_paid = float(security.decrypt_data(a.price_paid_enc))
            enriched.append({
                "symbol": a.symbol,
                "quantity": qtd,
                "price_paid": price_paid,
                "current_price": float(prices.get(a.symbol, price_paid)),
                "change_percent": changes.get(a.symbol, 0.0),
            })
        except:
            pass
        
    valid_changes = [a for a in enriched if a["change_percent"] != 0.0]
    sorted_by_change = sorted(valid_changes, key=lambda x: x["change_percent"])
    
    top_loser = sorted_by_change[0] if sorted_by_change else None
    top_gainer = sorted_by_change[-1] if sorted_by_change else None
    if top_gainer and top_loser and top_gainer["symbol"] == top_loser["symbol"]:
        if top_gainer["change_percent"] > 0:
            top_loser = None
        else:
            top_gainer = None

    return {
        "assets": enriched,
        "top_gainer": top_gainer,
        "top_loser": top_loser,
        "rates": rates
    }

@app.get("/api/portfolio/history")
def get_portfolio_history(
    period: str = "1y",
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna a evolução patrimonial real dos ativos cadastrados.
    """
    from collections import defaultdict
    import pandas as pd
    import yfinance as yf
    
    assets = db.query(models.Asset).filter(models.Asset.owner_id == current_user.id).all()
    if not assets:
        return []

    # Decrypt assets beforehand to avoid doing it repeatedly in loops
    decrypted_assets = []
    for a in assets:
        try:
            qtd = float(security.decrypt_data(a.quantity_enc))
            price = float(security.decrypt_data(a.price_paid_enc))
            decrypted_assets.append({
                "symbol": a.symbol,
                "quantity": qtd,
                "price_paid": price,
                "currency": a.currency
            })
        except:
            pass

    monthly_data = defaultdict(float)
    total_invested_data = defaultdict(float)
    symbols = list(set([a["symbol"] for a in decrypted_assets]))
    benchmarks = ["^BVSP", "^GSPC", "^IXIC"]
    
    # Dicionários para benchmarks
    bench_raw = {b: defaultdict(float) for b in benchmarks}
    
    try:
        all_symbols = symbols + benchmarks
        if "BRL=X" not in all_symbols:
            all_symbols.append("BRL=X")
            
        df = yf.download(all_symbols, period=period, interval="1mo", auto_adjust=True, progress=False)
        closes = df["Close"] if len(all_symbols) > 1 else df[["Close"]]
        if len(all_symbols) == 1:
            closes.columns = all_symbols
            
        for date_idx, row in closes.iterrows():
            month_str = date_idx.strftime("%Y-%m")
            month_total = 0.0
            month_invested = 0.0
            
            usd_rate = row["BRL=X"] if "BRL=X" in row and not pd.isna(row["BRL=X"]) else 5.80
            
            for asset in decrypted_assets:
                price = row[asset["symbol"]] if asset["symbol"] in row else None
                if price and not pd.isna(price):
                    multiplier = usd_rate if asset.get("currency") == "USD" else 1.0
                    month_total += price * asset["quantity"] * multiplier
                    month_invested += asset["price_paid"] * asset["quantity"] * multiplier
                    
            if month_total > 0:
                monthly_data[month_str] = round(month_total, 2)
                total_invested_data[month_str] = round(month_invested, 2)
                
            # Guarda os preços crus dos benchmarks
            for b in benchmarks:
                b_price = row[b] if b in row else None
                if b_price and not pd.isna(b_price):
                    bench_raw[b][month_str] = float(b_price)
                
    except Exception as e:
        print(f"Error fetching portfolio history: {e}")
        return []

    result = []
    sorted_months = sorted(monthly_data.keys())
    
    # Calculando os multiplicadores para normalizar os benchmarks ao portfolio total inicial
    multipliers = {}
    if sorted_months:
        first_month = sorted_months[0]
        initial_portfolio_value = monthly_data[first_month]
        for b in benchmarks:
            initial_b_price = bench_raw[b].get(first_month)
            if initial_b_price and initial_b_price > 0:
                multipliers[b] = initial_portfolio_value / initial_b_price
            else:
                multipliers[b] = 0

    for m in sorted_months:
        point = {
            "month": m,
            "total": monthly_data[m],
            "invested": total_invested_data[m]
        }
        
        # Adiciona benchmarks normalizados
        if multipliers["^BVSP"] > 0 and m in bench_raw["^BVSP"]:
            point["ibov"] = round(bench_raw["^BVSP"][m] * multipliers["^BVSP"], 2)
        if multipliers["^GSPC"] > 0 and m in bench_raw["^GSPC"]:
            point["sp500"] = round(bench_raw["^GSPC"][m] * multipliers["^GSPC"], 2)
        if multipliers["^IXIC"] > 0 and m in bench_raw["^IXIC"]:
            point["nasdaq"] = round(bench_raw["^IXIC"][m] * multipliers["^IXIC"], 2)
            
        result.append(point)
        
    return result


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
        current_month_dividends = 0.0
        current_month_dps = 0.0
        history = []
        last_month_processed = -1
        
        last_date = None
        last_price = 0.0

        for date_idx, row in hist.iterrows():
            price = float(row["Close"])
            divs = float(row["Dividends"]) if "Dividends" in row else 0.0
            
            if date_idx.month != last_month_processed:
                if last_month_processed != -1 and last_date is not None:
                    # Salva o estado do final do mes anterior
                    history.append({
                        "month": last_date.strftime("%Y-%m"),
                        "invested": round(total_invested, 2),
                        "total": round((shares * last_price) + cash, 2),
                        "price": round(last_price, 2),
                        "accumulated_dividends": round(total_dividends, 2),
                        "monthly_dividends": round(current_month_dividends, 2),
                        "monthly_dividend_per_share": round(current_month_dps, 4),
                        "accumulated_shares": round(shares, 2)
                    })
                    current_month_dividends = 0.0
                    current_month_dps = 0.0
                    
                    cash += monthly_contribution
                    total_invested += monthly_contribution
                last_month_processed = date_idx.month

            if shares > 0 and divs > 0:
                dividend_payout = shares * divs
                total_dividends += dividend_payout
                current_month_dividends += dividend_payout
                current_month_dps += divs
                if data.reinvest_dividends:
                    cash += dividend_payout

            if cash >= price and price > 0:
                can_buy = int(cash // price)
                if can_buy > 0:
                    shares += can_buy
                    cash -= can_buy * price
                    
            last_date = date_idx
            last_price = price

        # Append o ultimo mes que ficou faltando
        if last_month_processed != -1 and last_date is not None:
            history.append({
                "month": last_date.strftime("%Y-%m"),
                "invested": round(total_invested, 2),
                "total": round((shares * last_price) + cash, 2),
                "price": round(last_price, 2),
                "accumulated_dividends": round(total_dividends, 2),
                "monthly_dividends": round(current_month_dividends, 2),
                "monthly_dividend_per_share": round(current_month_dps, 4),
                "accumulated_shares": round(shares, 2)
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

# =======================
# 🤖 INTELIGÊNCIA ARTIFICIAL (GEMINI)
# =======================

@app.get("/api/ai/analyze", response_model=schemas.AIAnalysisResponse)
def analyze_portfolio_ai(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import os
    import json
    import google.generativeai as genai

    # 1. Checa a chave da API
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Chave do Gemini não configurada no servidor.")
        
    genai.configure(api_key=api_key)

    # 2. Busca dados da carteira
    assets = db.query(models.Asset).filter(models.Asset.owner_id == current_user.id).all()
    if not assets:
        raise HTTPException(status_code=400, detail="Carteira vazia, nada para analisar.")
        
    decrypted_assets = []
    for a in assets:
        try:
            qtd = float(security.decrypt_data(a.quantity_enc))
            price = float(security.decrypt_data(a.price_paid_enc))
            decrypted_assets.append({
                "symbol": a.symbol,
                "quantity": qtd,
                "price_paid": price,
                "currency": a.currency
            })
        except:
            pass

    symbols = [a["symbol"] for a in decrypted_assets]
    prices = fetch_prices(symbols, db)
    
    # Busca summary real 
    total_invested = 0
    total_current = 0
    portfolio_details = []
    
    for a in decrypted_assets:
        sym = a["symbol"]
        curr_price = prices.get(sym) or a["price_paid"]
        
        invested = a["quantity"] * a["price_paid"]
        current = a["quantity"] * curr_price
        
        total_invested += invested
        total_current += current
        
        portfolio_details.append({
            "symbol": sym,
            "quantity": a["quantity"],
            "avg_price": a["price_paid"],
            "current_price": curr_price,
            "profit_pct": ((current - invested) / invested) * 100 if invested > 0 else 0
        })

    # Calcula peso de cada ativo
    for p in portfolio_details:
        p["weight_pct"] = ( (p["quantity"] * p["current_price"]) / total_current ) * 100 if total_current > 0 else 0

    # 3. Busca noticias recentes dos ativos
    import yfinance as yf
    from datetime import datetime, timedelta, timezone
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    news_dossier = {}
    
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            news_list = []
            for n in ticker.news:
                content = n.get("content", {})
                if not content:
                    content = n if "title" in n else {}
                if not content: continue
                
                pub_date_str = content.get("pubDate", "")
                if pub_date_str:
                    try:
                        pub_date = datetime.fromisoformat(pub_date_str.replace('Z', '+00:00'))
                        if pub_date < cutoff: continue
                    except:
                        pass
                news_list.append(content.get("title", ""))
            news_dossier[sym] = news_list[:3] # Top 3 noticias por ativo
        except:
            pass

    # 4. Constrói o Prompt
    portfolio_lines = ""
    for p in portfolio_details:
        portfolio_lines += f"- {p['symbol']}: Peso {p['weight_pct']:.1f}%, Preço médio R${p['avg_price']:.2f}, Preço atual R${p['current_price']:.2f}, Rentabilidade {p['profit_pct']:.1f}%\n"

    news_lines = ""
    for sym, news in news_dossier.items():
        if news:
            news_lines += f"- {sym}: " + " | ".join(news) + "\n"

    prompt = f"""Você é um analista financeiro sênior de nível institucional, especializado em Bolsa de Valores (B3, NYSE, NASDAQ), setores de Tecnologia, Agronegócio, Real Estate (FIIs), Energia e Utilities.
Você é objetivo, direto e baseia suas análises em dados fundamentalistas e cenário macroeconômico.

PERFIL DO INVESTIDOR:
- Dividendos: Conservador a Arrojado — busca dividendos seguros e pulverizados em nichos estáveis (ex: energia, logística, saneamento), com Dividend Yield ACIMA de 11%
- Trading/Especulação: Arrojado — aceita risco alto/muito alto se o potencial for de multiplicar o capital (3x ou mais). Quer saber as oportunidades mesmo que sejam apostas, para decidir por conta própria.
- Decisão final: É SEMPRE do investidor. Apresente os dois lados (prós e contras) de forma honesta.

CARTEIRA ATUAL (posições existentes para considerar no rebalanceamento):
Total Investido: R${total_invested:.2f}
Total Atual: R${total_current:.2f}
Resultado: {((total_current - total_invested) / total_invested)*100 if total_invested > 0 else 0:.2f}%

{portfolio_lines}

NOTÍCIAS RECENTES DOS ATIVOS (últimos 5 dias):
{news_lines if news_lines else "Nenhuma notícia relevante encontrada."}

TAREFA:
Retorne APENAS um JSON válido, sem markdown, sem texto extra, com exatamente esta estrutura:
{{
  "health_score": (int 0-100: avalia diversificação, qualidade dos ativos, nível de risco e rentabilidade),
  "market_comparison": (string: 1 parágrafo comparando a carteira com IBOV e S&P500 no cenário macro atual),
  "risk_assessment": (string: 1 parágrafo sobre concentração setorial, exposição cambial, correlações e riscos sistêmicos),
  "dividend_analysis": (string: 1 parágrafo analisando o potencial de dividendos dos ativos atuais),
  "assets_analysis": [
    {{
      "symbol": (string),
      "rating": (exatamente um de: "Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"),
      "alert_flag": (string com aviso de risco grave ou fraude, ou null),
      "reasoning": (string: justificativa direta e curta em 1 frase)
    }}
  ],
  "trading_opportunities": [
    {{
      "symbol": (string: ticker de oportunidade de preço — pode ser ação B3, BDR, ETF, cripto-ETF ou ação NYSE/NASDAQ com alto potencial especulativo),
      "sector": (string: setor da empresa),
      "current_price_ref": (string: preço de referência aproximado ex "R$12,50" ou "US$3.20"),
      "target_price": (string: preço alvo em 12-24 meses ex "R$40,00"),
      "upside_potential": (string: ex "220% de upside"),
      "risk_level": (string: exatamente "Alto" ou "Muito Alto"),
      "pros": (lista de 2 a 4 strings com os argumentos FAVORÁVEIS ao investimento),
      "cons": (lista de 2 a 4 strings com os argumentos CONTRÁRIOS — seja honesto e rigoroso),
      "thesis": (string: tese de investimento em 1 parágrafo explicando o racional de multiplicar o capital)
    }}
  ],
  "dividend_opportunities": [
    {{
      "symbol": (string: ticker — priorize FIIs de logística/shoppings, ações de utilities, energia, saneamento com DY acima de 11%),
      "sector": (string: setor/nicho),
      "estimated_dy": (string: ex "13.2%"),
      "safety_score": (string: exatamente "Alta", "Média-Alta" ou "Média"),
      "niche": (string: descrição do nicho ex "FII de Galpões Logísticos", "Distribuidora de Energia"),
      "reasoning": (string: por que este ativo é seguro e sustenta esse DY),
      "portfolio_fit": (string: como este ativo complementa e equilibra a carteira atual do investidor com base nas posições já existentes)
    }}
  ],
  "portfolio_balance": {{
    "assessment": (string: avaliação do equilíbrio atual — analise concentração, diversificação por setor e moeda),
    "rebalance_actions": (lista de 2-5 strings com ações concretas para rebalancear a carteira, considerando os ativos já existentes)
  }},
  "suggestions": [
    (string: 1 sugestão geral não coberta pelas outras seções),
    (string: outra sugestão)
  ]
}}
Retorne no mínimo 3 trading_opportunities e 4 dividend_opportunities.
"""

    try:
        model = genai.GenerativeModel("gemini-3.6-flash")
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json"
            )
        )
        data = json.loads(response.text)
        return data
    except Exception as e:
        print(f"Erro no Gemini: {e}")
        raise HTTPException(status_code=500, detail="Erro ao comunicar com a IA. Tente novamente mais tarde.")