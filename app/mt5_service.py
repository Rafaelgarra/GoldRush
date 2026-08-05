"""
app/mt5_service.py
Serviço de integração com MetaTrader 5 para preços em tempo real da B3.

REQUISITOS:
  - MetaTrader 5 instalado no Windows
  - Terminal MT5 aberto e logado na corretora
  - pip install MetaTrader5

O serviço falha de forma GRACIOSA: se o MT5 não estiver disponível,
retorna None e o sistema cai para o próximo fallback (yfinance).
"""

from typing import Optional
import math

# MT5 só existe no Windows — importação protegida
try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    _MT5_AVAILABLE = False
    mt5 = None  # type: ignore

_mt5_initialized: bool = False


def _ensure_initialized() -> bool:
    """Inicializa MT5 uma única vez. Retorna True se OK."""
    global _mt5_initialized
    if not _MT5_AVAILABLE:
        return False
    if _mt5_initialized:
        return True
    if mt5.initialize():
        _mt5_initialized = True
        info = mt5.terminal_info()
        print(f"[MT5] Conectado: {info.name if info else 'OK'}")
        return True
    print(f"[MT5] Falha ao inicializar: {mt5.last_error()}")
    return False


def is_mt5_available() -> bool:
    """Verifica se MT5 está disponível e conectado."""
    return _ensure_initialized()


def _normalize_symbol_for_mt5(symbol: str) -> list[str]:
    """
    Gera candidatos de símbolo para tentar no MT5.
    Diferentes corretoras usam sufixos diferentes.

    Ex: "PETR4" → ["PETR4", "PETR4F", "PETR4.SA"]
        "MXRF11.SA" → ["MXRF11", "MXRF11F"]
        "BTC-USD" → ["BTCUSD", "BITCOIN"]
    """
    # Remove sufixo .SA se vier do yfinance
    base = symbol.upper().replace(".SA", "").replace("-", "")

    candidates = [
        base,          # PETR4
        base + "F",    # PETR4F (alguns brokers usam para fracionário)
        base + "$",    # para índices em alguns brokers
    ]

    # Cripto
    if "BTC" in base:
        candidates = ["BTCUSD", "BTC-USD", "BITCOIN"]
    elif "ETH" in base:
        candidates = ["ETHUSD", "ETH-USD"]

    return candidates


def get_mt5_price(symbol: str) -> Optional[float]:
    """
    Busca preço atual de 1 ativo via MT5.
    Retorna None se MT5 não estiver disponível ou símbolo não encontrado.
    """
    if not _ensure_initialized():
        return None

    candidates = _normalize_symbol_for_mt5(symbol)
    for candidate in candidates:
        try:
            tick = mt5.symbol_info_tick(candidate)
            if tick is not None and tick.last > 0:
                return round(float(tick.last), 4)

            # Fallback: usa bid se last for 0 (fora do pregão)
            if tick is not None and tick.bid > 0:
                return round(float(tick.bid), 4)
        except Exception as e:
            print(f"[MT5] Erro ao buscar {candidate}: {e}")
            continue

    return None


def get_mt5_prices_batch(symbols: list[str]) -> dict[str, float]:
    """
    Busca preços de múltiplos ativos via MT5 em sequência.
    MT5 não tem "batch" real, mas cada chamada é muito rápida (local).
    
    Retorna dict com os símbolos que tiveram sucesso.
    """
    if not _ensure_initialized():
        return {}

    results: dict[str, float] = {}
    for symbol in symbols:
        price = get_mt5_price(symbol)
        if price is not None:
            results[symbol] = price

    return results


def get_mt5_history(symbol: str, bars: int = 30) -> list[dict]:
    """
    Busca histórico de preços intraday via MT5 (candlesticks de 1 min).
    Útil para montar gráficos intraday.
    """
    if not _ensure_initialized():
        return []

    try:
        import pandas as pd
        candidates = _normalize_symbol_for_mt5(symbol)

        for candidate in candidates:
            rates = mt5.copy_rates_from_pos(candidate, mt5.TIMEFRAME_M1, 0, bars)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s")
                return df[["time", "open", "high", "low", "close", "tick_volume"]].to_dict("records")
    except Exception as e:
        print(f"[MT5] Erro ao buscar histórico de {symbol}: {e}")

    return []


def shutdown_mt5():
    """Encerra a conexão com MT5 (chamar no shutdown do FastAPI)."""
    global _mt5_initialized
    if _MT5_AVAILABLE and _mt5_initialized:
        mt5.shutdown()
        _mt5_initialized = False
        print("[MT5] Conexão encerrada.")
