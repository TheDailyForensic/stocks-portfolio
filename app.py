import os
import json
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from pymongo import MongoClient
from bson import ObjectId

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_in_prod")

# Global Configuration Setting for Currency Defaulting
# Set to True to automatically convert all prices, portfolios, histories, and quotes to INR in real-time
CONVERT_TO_INR_SETTING = True 

# ── API keys ──────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
MONGO_URI       = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

# ── MongoDB setup ─────────────────────────────────────────────────────────────
mongo_client = MongoClient(MONGO_URI)
db           = mongo_client["stocksim"]
users_col    = db["users"]

# Ensure username is unique
users_col.create_index("username", unique=True)

STARTING_CASH = 10_000.00  # Stored internally in native USD currency baseline


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":  return "NASDAQ"
    if ticker == "^NSEI":  return "NIFTY 50"
    if ticker.endswith(".NS"): return ticker.replace(".NS", "")
    return ticker

def get_usd_inr_rate() -> float:
    """Fetches real-time USD to INR exchange rate with absolute fallbacks."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/INR=X?range=1d&interval=1m"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        r = requests.get(url, headers=headers, timeout=5).json()
        meta = r.get("chart", {}).get("result", [{}])[0].get("meta", {})
        rate = meta.get("regularMarketPrice")
        if rate:
            return float(rate)
    except Exception:
        pass
    return 83.50  # Stable trailing fallback baseline rate if connection fails

def convert_price(amount: float, from_currency: str, target_currency: str, rate: float) -> float:
    if from_currency == target_currency:
        return amount
    if from_currency == "USD" and target_currency == "INR":
        return amount * rate
    if from_currency == "INR" and target_currency == "USD":
        return amount / rate
    return amount

def interpret_asset_query(user_input: str) -> dict:
    # Removed any visible traces or statements referencing "AI"
    system_instruction = """
    You are a financial data routing assistant. Take user inputs, fix typos, and return a standardized JSON object.

    Rules:
    1. For US stocks/ETFs, provider is 'finnhub', ticker is standard uppercase (e.g. AAPL, NVDA).
    2. For Indian stocks on NSE, provider is 'yahoo', ticker appends '.NS' (e.g. RELIANCE.NS).
    3. For Nasdaq Index: ticker '^IXIC', provider 'yahoo', cleanName 'NASDAQ Composite'.
    4. For Nifty 50:     ticker '^NSEI', provider 'yahoo', cleanName 'NIFTY 50'.

    Respond ONLY with valid JSON:
    {"ticker":"STRING","provider":"finnhub or yahoo","cleanName":"STRING","description":"STRING","error":false}
    """
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user",   "content": f"Query: '{user_input}'"}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {"ticker": "", "provider": "", "cleanName": "Unknown",
                "description": "Asset", "error": True}

def fetch_live_quote(ticker: str, provider: str):
    if provider == "finnhub":
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
        try:
            r = requests.get(url, timeout=8).json()
            if not r.get("c"): return None
            return {
                "symbol": ticker,
                "price":  float(r["c"]),
                "change": round(r.get("d",  0), 4),
                "pct":    round(r.get("dp", 0), 4),
                "low":    r.get("l", r["c"]),
                "high":   r.get("h", r["c"]),
                "prev":   r.get("pc", r["c"]),
                "currency": "USD"
            }
        except Exception:
            return None
    else:
        # Replaced yfinance with high-availability Direct Yahoo Chart API Endpoint v8 to completely fix Indian stock drops
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2d&interval=1d"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        try:
            r = requests.get(url, headers=headers, timeout=8).json()
            result = r.get("chart", {}).get("result", [])
            if not result:
                return None
                
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose")
            
            if price is None:
                # Fallback to indicators array structure if regularMarketPrice is missing during market adjustments
                indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
                close_list = [c for c in indicators.get("close", []) if c is not None]
                if close_list:
                    price = close_list[-1]
                else:
                    return None
                    
            if prev is None:
                prev = price

            low = price
            high = price
            indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
            low_list = [l for l in indicators.get("low", []) if l is not None]
            high_list = [h for h in indicators.get("high", []) if h is not None]
            if low_list: low = low_list[-1]
            if high_list: high = high_list[-1]

            currency = "INR" if (".NS" in ticker or "NSEI" in ticker) else "USD"
            return {
                "symbol": ticker, 
                "price": float(price),
                "change": round(price - prev, 4), 
                "pct": round(((price - prev) / prev) * 100, 4) if prev else 0,
                "low": float(low), 
                "high": float(high), 
                "prev": float(prev),
                "currency": currency
            }
        except Exception:
            return None

def get_user(username: str):
    return users_col.find_one({"username": username})

def safe_user_view(user: dict) -> dict:
    u = dict(user)
    u.pop("password", None)
    u.pop("_id", None)
    return u


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth/register", methods=["POST"])
def register():
    data        = request.json or {}
    username    = data.get("username", "").lower().strip()
    display     = data.get("displayName", "").strip()
    password    = data.get("password", "").strip()

    if not username or not display or not password:
        return jsonify({"error": "Please fill in all fields."}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters."}), 400
    if len(username) < 2:
        return jsonify({"error": "Username must be at least 2 characters."}), 400

    new_user = {
        "username":    username,
        "displayName": display,
        "password":    password,
        "cash":        STARTING_CASH,
        "holdings":    {},
        "history":     [],
        "created_at":  now_str()
    }
    try:
        users_col.insert_one(new_user)
    except Exception:
        return jsonify({"error": "Username is already taken."}), 400

    session["user"] = username
    return jsonify(safe_user_view(new_user))

@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = data.get("username", "").lower().strip()
    password = data.get("password", "").strip()

    user = get_user(username)
    if not user or user["password"] != password:
        return jsonify({"error": "Incorrect username or password."}), 401

    session["user"] = username
    return jsonify(safe_user_view(user))

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"success": True})


# ── Market data ───────────────────────────────────────────────────────────────
@app.route("/api/market/query")
def query_market():
    q = request.args.get("query", "").strip()
    if not q:
        return jsonify({"error": "No query provided."}), 400

    # Internal routing object used implicitly (Removed AI label traces)
    routing_data = interpret_asset_query(q)
    if routing_data.get("error") or not routing_data.get("ticker"):
        return jsonify({"error": "Could not identify that stock or index."}), 404

    quote = fetch_live_quote(routing_data["ticker"], routing_data["provider"])
    if not quote:
        return jsonify({"error": "Failed to fetch live price. Try again."}), 404

    quote["cleanName"]           = routing_data.get("cleanName", routing_data["ticker"])
    quote["assetClassDescription"] = routing_data.get("description", "Market Asset")
    
    # Global Currency conversion context logic to INR
    if CONVERT_TO_INR_SETTING and quote["currency"] != "INR":
        rate = get_usd_inr_rate()
        quote["price"] = round(convert_price(quote["price"], quote["currency"], "INR", rate), 2)
        quote["low"] = round(convert_price(quote["low"], quote["currency"], "INR", rate), 2)
        quote["high"] = round(convert_price(quote["high"], quote["currency"], "INR", rate), 2)
        quote["prev"] = round(convert_price(quote["prev"], quote["currency"], "INR", rate), 2)
        quote["change"] = round(quote["price"] - quote["prev"], 2)
        quote["currency"] = "INR"

    return jsonify(quote)


# ── Portfolio ─────────────────────────────────────────────────────────────────
@app.route("/api/user/portfolio")
def get_portfolio():
    if "user" not in session:
        return jsonify({"error": "Not logged in."}), 401

    user = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    rate = get_usd_inr_rate()
    target_curr = "INR" if CONVERT_TO_INR_SETTING else "USD"

    # User cash balance is fundamentally tracked against baseline USD values inside DB
    display_cash = convert_price(user["cash"], "USD", target_curr, rate)
    
    invested_total = 0.0
    positions_list = []

    for sym, holding in user.get("holdings", {}).items():
        routing_obj = interpret_asset_query(sym)
        quote = fetch_live_quote(sym, routing_obj.get("provider", "finnhub"))

        native_price = quote["price"] if quote else holding["cost"]
        native_currency = quote["currency"] if quote else "USD"

        # Evaluate values converted into runtime configurations
        current_price_converted = convert_price(native_price, native_currency, target_curr, rate)
        avg_cost_converted = convert_price(holding["cost"], "USD", target_curr, rate)

        mkt_val = holding["shares"] * current_price_converted
        cost_basis = holding["shares"] * avg_cost_converted
        gl = mkt_val - cost_basis
        gl_pct = ((current_price_converted - avg_cost_converted) / avg_cost_converted) * 100 if avg_cost_converted else 0

        invested_total += mkt_val
        positions_list.append({
            "symbol":       routing_obj.get("cleanName", sym),
            "rawToken":     sym,
            "shares":       holding["shares"],
            "avgCost":      round(avg_cost_converted, 2),
            "currentPrice": round(current_price_converted, 2),
            "marketValue":  round(mkt_val, 2),
            "gainLoss":     round(gl, 2),
            "gainLossPct":  round(gl_pct, 4),
            "currency":     target_curr
        })

    net_val = display_cash + invested_total
    
    # Calculate starting metrics converted to sync dynamic returns properly
    converted_start = convert_price(STARTING_CASH, "USD", target_curr, rate)
    yield_pct = ((net_val - converted_start) / converted_start) * 100

    # Map conversion dynamically over execution logs
    processed_history = []
    for entry in user.get("history", []):
        ent = dict(entry)
        if CONVERT_TO_INR_SETTING and ent["currency"] != "INR":
            ent["price"] = round(convert_price(ent["price"], ent["currency"], "INR", rate), 2)
            ent["sum"] = round(convert_price(ent["sum"], ent["currency"], "INR", rate), 2)
            ent["pl"] = round(convert_price(ent["pl"], ent["currency"], "INR", rate), 2)
            ent["currency"] = "INR"
        processed_history.append(ent)

    return jsonify({
        "cash":      round(display_cash, 2),
        "invested":  round(invested_total, 2),
        "netValue":  round(net_val, 2),
        "returns":   round(yield_pct, 4),
        "positions": positions_list,
        "history":   processed_history
    })


# ── Trading ───────────────────────────────────────────────────────────────────
@app.route("/api/trade/execute", methods=["POST"])
def execute_trade():
    if "user" not in session:
        return jsonify({"error": "Not logged in."}), 401

    data   = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    qty    = float(data.get("qty", 0))
    mode   = data.get("mode", "buy")
    price  = float(data.get("price", 0)) # Received in standard converted interface unit values

    if qty <= 0 or price <= 0:
        return jsonify({"error": "Invalid quantity or price."}), 400

    user = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    routing_obj = interpret_asset_query(symbol)
    quote = fetch_live_quote(symbol, routing_obj.get("provider", "finnhub"))
    native_currency = quote["currency"] if quote else ("INR" if (".NS" in symbol or "NSEI" in symbol) else "USD")
    
    rate = get_usd_inr_rate()
    
    # Normalize transactional execution context value down into internal system USD metrics
    price_in_usd = price
    if CONVERT_TO_INR_SETTING:
        price_in_usd = convert_price(price, "INR", "USD", rate)
    elif native_currency == "INR":
        price_in_usd = convert_price(price, "INR", "USD", rate)

    holdings   = user.get("holdings", {})
    cash       = user["cash"]
    total_cost_usd = round(qty * price_in_usd, 6)
    clean_sym  = get_clean_name_mapping(symbol)
    timestamp  = now_str()
    pl_usd     = 0.0

    if mode == "buy":
        if cash < total_cost_usd:
            return jsonify({"error": "Insufficient funds for this order."}), 400

        cash -= total_cost_usd
        if symbol not in holdings:
            holdings[symbol] = {"shares": 0.0, "cost": 0.0}

        prev_shares = holdings[symbol]["shares"]
        prev_cost   = holdings[symbol]["cost"]
        new_shares  = prev_shares + qty
        holdings[symbol]["shares"] = new_shares
        holdings[symbol]["cost"]   = ((prev_shares * prev_cost) + total_cost_usd) / new_shares

    else:  # sell
        owned = holdings.get(symbol, {}).get("shares", 0)
        if owned < qty - 1e-9:
            return jsonify({"error": f"You only own {owned:.4f} shares of {clean_sym}."}), 400

        avg_cost_usd = holdings[symbol]["cost"]
        pl_usd       = round((price_in_usd - avg_cost_usd) * qty, 6)
        cash        += total_cost_usd
        holdings[symbol]["shares"] -= qty

        if holdings[symbol]["shares"] <= 1e-9:
            del holdings[symbol]

    # History entries are saved natively based on trade occurrences
    history_entry = {
        "date":        timestamp,
        "type":        "BUY" if mode == "buy" else "SELL",
        "symbol":      symbol,
        "cleanSymbol": clean_sym,
        "shares":      qty,
        "price":       round(price, 4),
        "sum":         round(qty * price, 4),
        "pl":          round(convert_price(pl_usd, "USD", "INR", rate), 4) if CONVERT_TO_INR_SETTING else round(pl_usd, 4),
        "currency":    "INR" if CONVERT_TO_INR_SETTING else native_currency
    }

    users_col.update_one(
        {"username": session["user"]},
        {
            "$set":   {"cash": round(cash, 6), "holdings": holdings},
            "$push":  {"history": {"$each": [history_entry], "$position": 0}}
        }
    )

    # Returns a structured confirmation JSON object context. Use this payload on your front-end template
    # to render a beautiful HTML overlay/modal instead of a standard `alert()` window.
    return jsonify({
        "success": True, 
        "show_custom_modal": True, 
        "modal_title": "Transaction Successful",
        "modal_message": f"Successfully processed {mode.upper()} order for {qty} shares of {clean_sym}.",
        "pl": history_entry["pl"], 
        "newCash": round(convert_price(cash, "USD", "INR" if CONVERT_TO_INR_SETTING else "USD", rate), 2),
        "currency": "INR" if CONVERT_TO_INR_SETTING else "USD"
    })


# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
def leaderboard():
    board = []
    rate = get_usd_inr_rate()
    target_curr = "INR" if CONVERT_TO_INR_SETTING else "USD"
    
    for user in users_col.find({}, {"password": 0, "_id": 0}):
        # FIXED: Look up actual live stock valuation prices to reflect continuous dynamic growth accurately!
        current_invested_usd = 0.0
        for sym, holding in user.get("holdings", {}).items():
            routing_obj = interpret_asset_query(sym)
            quote = fetch_live_quote(sym, routing_obj.get("provider", "finnhub"))
            live_price = quote["price"] if quote else holding["cost"]
            live_currency = quote["currency"] if quote else "USD"
            
            # Convert live value metric down to core baseline USD valuation checks
            live_price_usd = convert_price(live_price, live_currency, "USD", rate)
            current_invested_usd += holding["shares"] * live_price_usd
            
        net_value_usd = user["cash"] + current_invested_usd
        returns_pct = ((net_value_usd - STARTING_CASH) / STARTING_CASH) * 100
        trade_count = len(user.get("history", []))
        
        # Output final numbers converted on demand for UI settings uniformity
        display_net_value = convert_price(net_value_usd, "USD", target_curr, rate)
        display_cash = convert_price(user["cash"], "USD", target_curr, rate)

        board.append({
            "name":       user["displayName"],
            "handle":     user["username"],
            "cash":       round(display_cash, 2),
            "netValue":   round(display_net_value, 2),
            "returns":    round(returns_pct, 4),
            "tradeCount": trade_count,
            "currency":   target_curr
        })

    board.sort(key=lambda x: x["returns"], reverse=True)
    return jsonify(board)


if __name__ == "__main__":
    app.run(debug=True)
