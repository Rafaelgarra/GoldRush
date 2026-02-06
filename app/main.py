from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
import yfinance as yf
import math
import os
from typing import List
from dotenv import load_dotenv
from pydantic import BaseModel

# Nossos módulos
from app import models, schemas, database, security

load_dotenv()

# --- CRIA O BANCO (Se não existir) ---
# IMPORTANTE: Se der erro, apague o arquivo 'sql_app.db' para ele recriar do zero!
models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="GoldRush API - Secure Edition")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "uma_chave_secreta_provisoria"))

# --- CONFIGURAÇÃO CORS ---
origins = [
    "http://localhost:3000",
    "https://goldrush-web.vercel.app",
    os.getenv("FRONTEND_URL", "http://localhost:3000")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURAÇÃO GOOGLE OAUTH ---
oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# --- DEPENDÊNCIAS ---
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Verifica quem é o usuário baseado no Token JWT
async def get_current_user(token: str = Depends(security.oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = security.jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
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
# 🔐 ROTAS DE AUTENTICAÇÃO
# =======================

# 1. Registro (Email/Senha)
@app.post("/api/register", response_model=schemas.Token)
def register(user: schemas.UserRegister, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Email já cadastrado")
    
    hashed_pwd = security.get_password_hash(user.password)
    # Criamos o usuário já ativo para facilitar, depois implementamos email confirm
    new_user = models.User(email=user.email, hashed_password=hashed_pwd, is_active=True, provider="email")
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    access_token = security.create_access_token(data={"sub": new_user.email})
    return {"access_token": access_token, "token_type": "bearer"}

# 2. Login (Email/Senha)
@app.post("/api/token", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if not user or not user.hashed_password or not security.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")
    
    access_token = security.create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

# 3. Login GOOGLE (Inicia)
@app.get("/auth/google")
async def login_google(request: Request):
    redirect_uri = request.url_for('auth_google_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri)

# 4. Callback GOOGLE (Retorno)
@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        email = user_info.email
        
        # Procura ou Cria usuário
        user = db.query(models.User).filter(models.User.email == email).first()
        if not user:
            user = models.User(
                email=email, 
                is_active=True, 
                provider="google",
                avatar_url=user_info.picture
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        
        # Gera token JWT
        access_token = security.create_access_token(data={"sub": user.email})
        
        # Redireciona pro Front com o token
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=f"{frontend_url}/auth/callback?token={access_token}")
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro Login Google: {e}")

# =======================
# 💰 ROTAS PORTFÓLIO (PROTEGIDAS)
# =======================

@app.get("/api/portfolio", response_model=List[schemas.AssetResponse])
def get_portfolio(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Pega só os ativos DESTE usuário
    assets_enc = db.query(models.Asset).filter(models.Asset.owner_id == current_user.id).all()
    
    results = []
    for asset in assets_enc:
        try:
            # DESCRIPTOGRAFA OS DADOS PARA ENVIAR PRO FRONT
            qtd = float(security.decrypt_data(asset.quantity_enc))
            price = float(security.decrypt_data(asset.price_paid_enc))
            
            results.append({
                "id": asset.id,
                "symbol": asset.symbol,
                "quantity": qtd,
                "price_paid": price,
                "asset_type": asset.asset_type,
                "currency": asset.currency,
                "purchase_date": asset.purchase_date
            })
        except Exception as e:
            print(f"Erro decriptação asset {asset.id}: {e}")
            continue
            
    return results

@app.post("/api/portfolio")
def add_asset(asset: schemas.AssetCreate, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # CRIPTOGRAFA OS DADOS ANTES DE SALVAR
    qtd_enc = security.encrypt_data(str(asset.quantity))
    price_enc = security.encrypt_data(str(asset.price_paid))
    
    new_asset = models.Asset(
        symbol=asset.symbol,
        quantity_enc=qtd_enc,
        price_paid_enc=price_enc,
        asset_type=asset.asset_type,
        currency=asset.currency,
        owner_id=current_user.id # Vincula ao usuário logado
    )
    db.add(new_asset)
    db.commit()
    return {"message": "Ativo protegido e salvo!"}

@app.delete("/api/portfolio/{asset_id}")
def delete_asset(asset_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    
    asset = db.query(models.Asset).filter(models.Asset.id == asset_id).first()
    
    if not asset:
        raise HTTPException(status_code=404, detail="Ativo não encontrado")
    
    if asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Você não tem permissão para deletar este ativo")
    
    db.delete(asset)
    db.commit()
    
    return {"message": "Ativo removido com sucesso"}

@app.put("/api/portfolio/{asset_id}")
def update_asset(asset_id: int, asset_update: schemas.AssetCreate, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 1. Busca
    asset = db.query(models.Asset).filter(models.Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Ativo não encontrado")
    if asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Sem permissão")

    # 2. Criptografa os novos valores
    qtd_enc = security.encrypt_data(str(asset_update.quantity))
    price_enc = security.encrypt_data(str(asset_update.price_paid))

    # 3. Atualiza
    asset.symbol = asset_update.symbol
    asset.quantity_enc = qtd_enc
    asset.price_paid_enc = price_enc
    asset.asset_type = asset_update.asset_type
    asset.currency = asset_update.currency
    
    db.commit()
    return {"message": "Ativo atualizado com sucesso"}

# =======================
# 🌍 ROTAS PÚBLICAS (PREÇOS)
# =======================

@app.get("/api/price/{ticker}")
def get_price(ticker: str):
    try:
        if ticker in ["BTC", "ETH"]:
             symbol = f"{ticker}-USD"
        else:
             symbol = ticker.upper()

        stock = yf.Ticker(symbol)
        data = stock.history(period="5d") 
        
        if data.empty and not symbol.endswith(".SA") and not symbol.endswith("-USD"):
            symbol_br = symbol + ".SA"
            stock = yf.Ticker(symbol_br)
            data = stock.history(period="5d")
            if not data.empty:
                symbol = symbol_br

        if data.empty:
            return {"error": "Ticker não encontrado", "symbol": ticker}
            
        price = data['Close'].iloc[-1]
        
        return {
            "symbol": symbol, 
            "current_price": round(price, 2)
        }
    except Exception as e:
        print(f"Erro ao buscar {ticker}: {e}")
        return {"error": str(e)}

@app.get("/api/market-summary")
def get_market_summary(currency: str = "BRL"):
    """
    Retorna cotações resumidas (Dólar, Euro, BTC, Yuan, Iene)
    """
    if currency == "BRL":
        tickers_map = { 
            "Dólar": "BRL=X", 
            "Euro": "EURBRL=X", 
            "Bitcoin": "BTC-USD", 
            "Yuan": "CNYBRL=X", 
            "Iene": "JPYBRL=X", 
            "Libra": "GBPBRL=X" 
        }
    else:
        # Caso queira ver em Dólar no futuro
        tickers_map = { 
            "Euro": "EURUSD=X", 
            "Bitcoin": "BTC-USD", 
            "Real": "BRL=X", 
            "Iene": "JPY=X", 
            "Yuan": "CNY=X", 
            "Libra": "GBPUSD=X" 
        }

    data = []
    try:
        # Baixa tudo de uma vez (download em lote é mais rápido)
        df = yf.download(list(tickers_map.values()), period="1d", progress=False)['Close']
        
        # Pega a última linha (preço mais atual)
        # Se veio mais de um ativo, é um DataFrame, senão é Series. Tratamos aqui:
        if len(tickers_map) > 1:
            last_prices = df.iloc[-1]
        else:
            # Caso raro de pedir só 1, o yfinance muda o formato
            last_prices = {list(tickers_map.values())[0]: df.iloc[-1]}

        for name, ticker in tickers_map.items():
            try:
                # Verifica se o ticker existe no retorno do Yahoo
                val = last_prices.get(ticker)
                
                # Se não encontrou ou é NaN (Not a Number), pula
                if val is None or math.isnan(val): 
                    continue
                
                price = float(val)

                # Conversão Especial: Bitcoin vem em Dólar, precisamos converter pra Real
                if name == "Bitcoin" and currency == "BRL":
                    dolar_ticker = "BRL=X"
                    # Tenta pegar o dólar do mesmo lote, ou usa 5.80 de fallback
                    dolar_val = last_prices.get(dolar_ticker)
                    dolar = float(dolar_val) if (dolar_val and not math.isnan(dolar_val)) else 5.80
                    price = price * dolar
                
                data.append({ 
                    "name": name, 
                    "ticker": ticker, 
                    "price": round(price, 4) if price < 1 else round(price, 2), # Iene tem valor baixo, precisa de mais casas
                    "currency": currency 
                })
            except Exception as e:
                print(f"Erro ao processar {name}: {e}")
                continue
    except Exception as e:
        print(f"Erro geral no market-summary: {e}")
        pass
        
    return data

# =======================
# 🔮 ROTAS DO SIMULADOR
# =======================

@app.post("/api/simulation")
def simulate_future(data: schemas.SimulationRequest):
    """
    Calcula a evolução do patrimônio mês a mês (Juros Compostos).
    """
    months = data.years * 12
    # Converte taxa anual para mensal
    monthly_rate = (1 + (data.interest_rate_yearly / 100))**(1/12) - 1
    
    current_total = data.initial_amount
    total_invested = data.initial_amount
    
    history = []
    
    # Mês 0 (Ponto de partida)
    history.append({
        "month": 0,
        "invested": round(total_invested, 2),
        "total": round(current_total, 2),
        "interest": 0
    })

    for m in range(1, months + 1):
        # 1. O dinheiro rende primeiro
        yield_amount = current_total * monthly_rate
        current_total += yield_amount
        
        # 2. Depois você aporta mais dinheiro
        current_total += data.monthly_contribution
        total_invested += data.monthly_contribution
        
        # 3. Salva no histórico para o gráfico
        # (Opcional: Se for muitos anos, pode salvar só a cada 6 meses pra economizar dados)
        history.append({
            "month": m,
            "invested": round(total_invested, 2),
            "total": round(current_total, 2),
            "interest": round(current_total - total_invested, 2)
        })
        
    return {
        "final_total": round(current_total, 2),
        "total_invested": round(total_invested, 2),
        "total_interest": round(current_total - total_invested, 2),
        "history": history
    }