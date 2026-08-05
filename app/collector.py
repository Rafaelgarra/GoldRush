"""
app/collector.py
Coletor de preços EOD (End-of-Day) — roda automaticamente após o fechamento
de cada bolsa via APScheduler integrado ao FastAPI.

Horários configurados (BRT / UTC-3):
  B3   → 18:00 seg–sex (fecha 17:55)
  NYSE → 22:30 seg–sex (fecha ~21:00 BRT no horário de verão americano)
  Cripto → a cada 4 horas (mercado 24/7)

O coletor:
  1. Coleta todos os símbolos únicos (portfólio + watchlist de todos os usuários)
  2. Faz download em LOTE via yfinance (1 request para N tickers)
  3. Salva OHLCV em price_history (histórico imutável)
  4. Atualiza price_cache (último preço conhecido)
"""

from datetime import datetime, date, timedelta
import math
import yfinance as yf

from app.database import SessionLocal
from app.models import Asset, Watchlist, PriceHistory, PriceCache


def _get_all_symbols(db, market: str = "BRL") -> list[str]:
    """
    Coleta todos os símbolos únicos de portfólio + watchlist de todos os usuários,
    filtrando pela moeda/mercado.
    """
    portfolio_syms = (
        db.query(Asset.symbol)
        .filter(Asset.currency == market)
        .distinct()
        .all()
    )
    watchlist_syms = (
        db.query(Watchlist.symbol)
        .filter(Watchlist.currency == market)
        .distinct()
        .all()
    )

    all_syms = list(set(
        [s[0] for s in portfolio_syms] + [s[0] for s in watchlist_syms]
    ))

    return [s for s in all_syms if s]  # remove vazios


def _save_eod(db, symbol: str, row: dict, today: date):
    """
    Salva ou atualiza registro EOD para um símbolo.
    Usa INSERT OR UPDATE (upsert via SQLAlchemy).
    """
    # Atualiza price_history (histórico imutável por dia)
    existing = (
        db.query(PriceHistory)
        .filter(PriceHistory.symbol == symbol, PriceHistory.date == today)
        .first()
    )

    if existing:
        existing.close = row.get("close", existing.close)
        existing.open = row.get("open")
        existing.high = row.get("high")
        existing.low = row.get("low")
        existing.volume = row.get("volume")
        existing.dividends = row.get("dividends", 0)
    else:
        db.add(PriceHistory(
            symbol=symbol,
            date=today,
            open=row.get("open"),
            high=row.get("high"),
            low=row.get("low"),
            close=row["close"],
            volume=row.get("volume"),
            dividends=row.get("dividends", 0),
        ))

    # Atualiza price_cache (preço mais recente)
    cache = db.query(PriceCache).filter(PriceCache.symbol == symbol).first()
    if cache:
        cache.price = row["close"]
        cache.source = "eod_collector"
        cache.updated_at = datetime.utcnow()
    else:
        db.add(PriceCache(
            symbol=symbol,
            price=row["close"],
            source="eod_collector",
            updated_at=datetime.utcnow(),
        ))


def collect_eod_prices(market: str = "BRL"):
    """
    Job principal de coleta EOD.
    
    Args:
        market: "BRL" para B3, "USD" para NYSE/NASDAQ
    """
    db = SessionLocal()
    today = date.today()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n[Collector] ▶ Iniciando coleta EOD {market} — {now_str}")

    try:
        symbols = _get_all_symbols(db, market)

        if not symbols:
            print(f"[Collector] Nenhum símbolo para mercado {market}. Pulando.")
            return

        print(f"[Collector] Coletando {len(symbols)} símbolos: {symbols}")

        # Download em LOTE — 1 request para todos os tickers
        chunk_size = 50  # Yahoo Finance lida bem com até 50 por vez
        saved = 0
        errors = 0

        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]

            try:
                if len(chunk) == 1:
                    ticker = yf.Ticker(chunk[0])
                    hist = ticker.history(period="2d", auto_adjust=True)
                    if hist.empty:
                        errors += 1
                        continue

                    row = {
                        "close": float(hist["Close"].iloc[-1]),
                        "open": float(hist["Open"].iloc[-1]),
                        "high": float(hist["High"].iloc[-1]),
                        "low": float(hist["Low"].iloc[-1]),
                        "volume": int(hist["Volume"].iloc[-1]),
                        "dividends": float(hist["Dividends"].iloc[-1]) if "Dividends" in hist else 0,
                    }

                    if not math.isnan(row["close"]):
                        _save_eod(db, chunk[0], row, today)
                        saved += 1

                else:
                    df = yf.download(
                        tickers=chunk,
                        period="2d",
                        progress=False,
                        group_by="ticker",
                        auto_adjust=True,
                    )

                    for sym in chunk:
                        try:
                            sym_df = df[sym] if sym in df else None
                            if sym_df is None or sym_df.empty:
                                errors += 1
                                continue

                            last = sym_df.dropna().iloc[-1]
                            close = float(last["Close"])

                            if math.isnan(close):
                                errors += 1
                                continue

                            row = {
                                "close": close,
                                "open": float(last["Open"]) if "Open" in last else None,
                                "high": float(last["High"]) if "High" in last else None,
                                "low": float(last["Low"]) if "Low" in last else None,
                                "volume": int(last["Volume"]) if "Volume" in last else None,
                                "dividends": 0,
                            }
                            _save_eod(db, sym, row, today)
                            saved += 1

                        except Exception as e:
                            print(f"[Collector] Erro em {sym}: {e}")
                            errors += 1

            except Exception as e:
                print(f"[Collector] Erro no chunk {chunk}: {e}")
                errors += len(chunk)

        db.commit()
        print(f"[Collector] ✅ Concluído — {saved} salvos, {errors} erros.")

    except Exception as e:
        print(f"[Collector] ❌ Erro geral: {e}")
        db.rollback()
    finally:
        db.close()


def collect_crypto_prices():
    """Job separado para cripto — roda a cada 4h pois o mercado é 24/7."""
    db = SessionLocal()
    today = date.today()

    try:
        crypto_symbols = (
            db.query(Asset.symbol)
            .filter(Asset.asset_type == "crypto")
            .distinct()
            .all()
        )
        watch_crypto = (
            db.query(Watchlist.symbol)
            .filter(Watchlist.asset_type == "crypto")
            .distinct()
            .all()
        )

        symbols = list(set(
            [s[0] for s in crypto_symbols] + [s[0] for s in watch_crypto]
        ))

        if not symbols:
            return

        print(f"[Collector] ▶ Coleta cripto: {symbols}")

        df = yf.download(symbols if len(symbols) > 1 else symbols[0],
                         period="1d", progress=False, auto_adjust=True)

        for sym in symbols:
            try:
                if len(symbols) == 1:
                    price = float(df["Close"].dropna().iloc[-1])
                else:
                    price = float(df[sym]["Close"].dropna().iloc[-1])

                if not math.isnan(price):
                    _save_eod(db, sym, {"close": price}, today)
            except Exception as e:
                print(f"[Collector] Erro cripto {sym}: {e}")

        db.commit()
        print("[Collector] ✅ Cripto concluído.")

    except Exception as e:
        print(f"[Collector] ❌ Erro cripto: {e}")
        db.rollback()
    finally:
        db.close()


def backfill_history(symbol: str, years: int = 5):
    """
    Utilitário para popular histórico retroativo de um símbolo.
    Chamar manualmente via CLI quando adicionar um ativo novo.
    
    Ex: python -c "from app.collector import backfill_history; backfill_history('PETR4.SA')"
    """
    db = SessionLocal()
    try:
        print(f"[Collector] ▶ Backfill {symbol} — {years} anos")
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{years}y", auto_adjust=True)

        if hist.empty:
            print(f"[Collector] Sem dados para {symbol}")
            return

        count = 0
        for dt, row in hist.iterrows():
            day = dt.date()
            close = float(row["Close"])
            if math.isnan(close):
                continue

            existing = (
                db.query(PriceHistory)
                .filter(PriceHistory.symbol == symbol, PriceHistory.date == day)
                .first()
            )
            if not existing:
                db.add(PriceHistory(
                    symbol=symbol,
                    date=day,
                    open=float(row["Open"]) if "Open" in row else None,
                    high=float(row["High"]) if "High" in row else None,
                    low=float(row["Low"]) if "Low" in row else None,
                    close=close,
                    volume=int(row["Volume"]) if "Volume" in row else None,
                    dividends=float(row["Dividends"]) if "Dividends" in row else 0,
                ))
                count += 1

        db.commit()
        print(f"[Collector] ✅ Backfill {symbol} — {count} registros inseridos.")
    except Exception as e:
        print(f"[Collector] ❌ Erro backfill {symbol}: {e}")
        db.rollback()
    finally:
        db.close()
