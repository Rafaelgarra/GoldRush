import yfinance as yf
import pandas as pd
from datetime import datetime
from app.schemas import SimulationRequest

def get_current_asset_price(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        price = ticker.fast_info.last_price
        if not price:
            history = ticker.history(period="1d")
            if not history.empty:
                price = history['Close'].iloc[-1]
            else:
                return {"error": "Preço não encontrado"}
        
        return {"symbol": symbol, "current_price": price}
    except Exception as e:
        return {"error": str(e)}

def calculate_simulation(request: SimulationRequest):
    try:
        ticker = yf.Ticker(request.symbol)
        history = ticker.history(start=request.start_date)
        
        if history.empty:
            return {"error": "Não há dados históricos para essa data."}

        history.index = pd.to_datetime(history.index).tz_localize(None)

        dividends = ticker.dividends
        dividends.index = pd.to_datetime(dividends.index).tz_localize(None)

        start_date_ts = pd.to_datetime(request.start_date)
        dividends = dividends[dividends.index >= start_date_ts]

        history['YearMonth'] = history.index.to_period('M')
        monthly_data = history.groupby('YearMonth').last()

        total_invested = 0.0
        current_shares = 0.0
        portfolio_value = 0.0
        
        simulation_history = []
        
        initial_made = False

        for period in monthly_data.index:
            date_obj = period.to_timestamp()
            price = monthly_data.loc[period]['Close']
            
            if pd.isna(price) or price <= 0:
                continue

            current_month_investment = 0.0

            if not initial_made:
                current_month_investment += request.initial_investment
                initial_made = True
            
            current_month_investment += request.monthly_contribution
            
            if request.reinvest_dividends:
                divs_in_month = dividends[dividends.index.to_period('M') == period]
                if not divs_in_month.empty:
                    total_divs = divs_in_month.sum() * current_shares
                    current_month_investment += total_divs

            new_shares = current_month_investment / price
            current_shares += new_shares
            total_invested += request.initial_investment if (current_month_investment > request.monthly_contribution and not initial_made) else (request.initial_investment if current_month_investment > request.monthly_contribution else 0) + request.monthly_contribution

            
        total_invested_cash = 0.0
        current_shares = 0.0
        
        simulation_history = []
        initial_made = False

        for period in monthly_data.index:
            date_obj = period.to_timestamp()
            price = monthly_data.loc[period]['Close']
            
            if pd.isna(price) or price <= 0: continue

            cash_to_buy = 0.0

            if not initial_made:
                cash_to_buy += request.initial_investment
                total_invested_cash += request.initial_investment
                initial_made = True
            
            cash_to_buy += request.monthly_contribution
            total_invested_cash += request.monthly_contribution

            if request.reinvest_dividends:
                divs_in_month = dividends[dividends.index.to_period('M') == period]
                if not divs_in_month.empty:
                    total_div_value = divs_in_month.sum() * current_shares
                    cash_to_buy += total_div_value

            new_shares = cash_to_buy / price
            current_shares += new_shares
            
            current_portfolio_value = current_shares * price

            simulation_history.append({
                "date": date_obj.strftime("%Y-%m-%d"),
                "portfolio_value": round(current_portfolio_value, 2),
                "total_invested": round(total_invested_cash, 2)
            })

        last_price = monthly_data.iloc[-1]['Close']
        final_value = current_shares * last_price
        roi = ((final_value - total_invested_cash) / total_invested_cash) * 100 if total_invested_cash > 0 else 0

        return {
            "symbol": request.symbol,
            "total_invested": round(total_invested_cash, 2),
            "final_portfolio_value": round(final_value, 2),
            "final_accumulated_shares": round(current_shares, 2),
            "final_unit_price": round(last_price, 2),
            "roi_percentage": round(roi, 2),
            "history": simulation_history
        }

    except Exception as e:
        print(f"Erro na simulação: {e}")
        return {"error": str(e)}