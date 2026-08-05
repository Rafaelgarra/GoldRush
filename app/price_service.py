"""
app/price_service.py
Cache de preços em 3 camadas para evitar rate limiting.

Hierarquia de fontes:
  1. Cache in-memory (TTL dinâmico: 10s pregão / 8h fora)
  2. PriceCache no banco (persiste entre reinicializações do servidor)
  3. MT5 (tempo real, sem limite — requer terminal aberto)
  4. yfinance em LOTE (1 request HTTP para N tickers)
"""

from datetime import datetime, timedelta
from typing import Optional
import math
import yfinance as yf

from app.mt5_service import get_mt5_prices_batch, is_mt5_available

# ─────────────────────────────────────────────────────────────
# CAMADA 1 — Cache in-memory com TTL dinâmico
# ─────────────────────────────────────────────────────────────

_memory_cache: dict[str, dict] = {}
# Formato: { "PETR4.SA": { "price": 38.42, "ts": datetime, "source": "mt5" } }


def is_market_open_b3() -> bool:
    """B3: seg–sex 10:00–17:55 BRT (UTC-3)."""
    now = datetime.utcnow() - timedelta(hours=3)
    return (
        now.weekday() < 5
        and 10 <= now.hour < 18
        and not (now.hour == 17 and now.minute >= 55)
    )


def is_market_open_nyse() -> bool:
    """NYSE: seg–sex 09:30–16:00 ET (UTC-4 verão / UTC-5 inverno)."""
    # Usamos UTC-4 como aproximação (horário de verão americano)
    now = datetime.utcnow() - timedelta(hours=4)
    return (
        now.weekday() < 5
        and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
        and now.hour < 16
    )


def get_cache_ttl(symbol: str) -> int:
    """
    TTL dinâmico em segundos baseado no tipo de ativo e horário.
    - Durante pregão: 10s (atualização frequente)
    - Fora do pregão: 8 horas (preço não muda)
    """
    is_us = symbol.endswith("-USD") or (
        not symbol.endswith(".SA") and len(symbol) <= 5 and symbol.isalpha()
    )
    is_crypto = "BTC" in symbol or "ETH" in symbol or symbol.endswith("-USD")

    if is_crypto:
        return 15  # Cripto não fecha — 15s sempre

    market_open = is_market_open_nyse() if is_us else is_market_open_b3()
    return 10 if market_open else 28800  # 10s pregão / 8h fora


def get_cached_price(symbol: str) -> Optional[float]:
    """Retorna preço do cache in-memory se ainda válido."""
    entry = _memory_cache.get(symbol)
    if not entry:
        return None
    age = (datetime.utcnow() - entry["ts"]).total_seconds()
    if age > get_cache_ttl(symbol):
        return None
    return entry["price"]


def set_cached_price(symbol: str, price: float, source: str = "yfinance"):
    """Salva preço no cache in-memory."""
    _memory_cache[symbol] = {
        "price": price,
        "ts": datetime.utcnow(),
        "source": source,
    }


def get_cache_info() -> dict:
    """Retorna estado atual do cache (para debug/monitoramento)."""
    return {
        sym: {
            "price": v["price"],
            "source": v["source"],
            "age_seconds": round((datetime.utcnow() - v["ts"]).total_seconds()),
            "ttl_seconds": get_cache_ttl(sym),
        }
        for sym, v in _memory_cache.items()
    }


# ─────────────────────────────────────────────────────────────
# CAMADA 2 — Banco de dados (PriceCache)
# ─────────────────────────────────────────────────────────────

def get_db_cached_price(symbol: str, db) -> Optional[float]:
    """
    Busca preço do banco (PriceCache).
    Aceita dados com até 1 hora de idade durante pregão,
    ou 24 horas fora do pregão.
    """
    from app.models import PriceCache

    cache = db.query(PriceCache).filter(PriceCache.symbol == symbol).first()
    if not cache:
        return None

    age = (datetime.utcnow() - cache.updated_at).total_seconds()
    max_age = 3600 if is_market_open_b3() or is_market_open_nyse() else 86400

    if age > max_age:
        return None
    return cache.price


def upsert_db_cache(symbol: str, price: float, source: str, db):
    """Atualiza ou cria entrada no PriceCache do banco."""
    from app.models import PriceCache

    entry = db.query(PriceCache).filter(PriceCache.symbol == symbol).first()
    if entry:
        entry.price = price
        entry.source = source
        entry.updated_at = datetime.utcnow()
    else:
        db.add(PriceCache(
            symbol=symbol,
            price=price,
            source=source,
            updated_at=datetime.utcnow(),
        ))


# ─────────────────────────────────────────────────────────────
# CAMADA 3 — Fetch externo em LOTE (MT5 → yfinance)
# ─────────────────────────────────────────────────────────────

def _fetch_yfinance_batch(symbols: list[str]) -> dict[str, float]:
    """
    Baixa preços de múltiplos tickers em UMA única chamada ao Yahoo Finance.
    Muito mais eficiente que N chamadas individuais.
    """
    if not symbols:
        return {}

    results: dict[str, float] = {}

    try:
        if len(symbols) == 1:
            ticker = yf.Ticker(symbols[0])
            data = ticker.history(period="2d")
            if not data.empty:
                price = float(data["Close"].dropna().iloc[-1])
                if not math.isnan(price):
                    results[symbols[0]] = round(price, 4)
        else:
            # Batch — 1 chamada HTTP para N tickers
            df = yf.download(
                tickers=symbols,
                period="2d",
                progress=False,
                group_by="ticker",
                auto_adjust=True,
            )
            for sym in symbols:
                try:
                    if len(symbols) > 1:
                        col = df[sym]["Close"] if sym in df else None
                    else:
                        col = df["Close"]

                    if col is not None:
                        price = float(col.dropna().iloc[-1])
                        if not math.isnan(price):
                            results[sym] = round(price, 4)
                except Exception:
                    pass
    except Exception as e:
        print(f"[PriceService] Erro yfinance batch {symbols}: {e}")

    return results


# ─────────────────────────────────────────────────────────────
# API PÚBLICA — Ponto de entrada único para preços
# ─────────────────────────────────────────────────────────────

def fetch_prices(symbols: list[str], db) -> dict[str, float]:
    """
    Busca preços para uma lista de símbolos.
    Respeita a hierarquia: cache → banco → MT5 → yfinance batch.

    Args:
        symbols: lista de tickers (ex: ["PETR4.SA", "MXRF11.SA", "BTC-USD"])
        db:      sessão SQLAlchemy ativa

    Returns:
        dict { symbol → price } com todos os que tiveram sucesso
    """
    results: dict[str, float] = {}
    to_fetch: list[str] = []

    # ── Passo 1: verifica cache in-memory ──────────────────
    for sym in symbols:
        cached = get_cached_price(sym)
        if cached is not None:
            results[sym] = cached
        else:
            to_fetch.append(sym)

    if not to_fetch:
        return results  # 100% cache hit — zero chamadas externas!

    # ── Passo 2: verifica banco ────────────────────────────
    still_missing: list[str] = []
    for sym in to_fetch:
        db_price = get_db_cached_price(sym, db)
        if db_price is not None:
            results[sym] = db_price
            set_cached_price(sym, db_price, source="db_cache")
        else:
            still_missing.append(sym)

    if not still_missing:
        return results

    # ── Passo 3: tenta MT5 (tempo real, sem limite) ────────
    fetched_via_mt5: list[str] = []
    if is_mt5_available():
        mt5_results = get_mt5_prices_batch(still_missing)
        for sym, price in mt5_results.items():
            results[sym] = price
            set_cached_price(sym, price, source="mt5")
            upsert_db_cache(sym, price, "mt5", db)
            fetched_via_mt5.append(sym)

    need_yfinance = [s for s in still_missing if s not in fetched_via_mt5]

    # ── Passo 4: yfinance em LOTE (fallback) ──────────────
    if need_yfinance:
        yf_results = _fetch_yfinance_batch(need_yfinance)
        for sym, price in yf_results.items():
            results[sym] = price
            set_cached_price(sym, price, source="yfinance")
            upsert_db_cache(sym, price, "yfinance", db)

    try:
        db.commit()
    except Exception:
        db.rollback()

    return results


def fetch_single_price(symbol: str, db) -> Optional[float]:
    """Conveniência: busca preço de 1 único símbolo."""
    results = fetch_prices([symbol], db)
    return results.get(symbol)
