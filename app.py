import os
import json
import requests
import yfinance as yf
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from pymongo import MongoClient
from bson import ObjectId

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_in_prod")

# ── Global Desk Config Settings ───────────────────────────────────────────────
# When True, all dollar-denominated metrics automatically convert to INR in real-time
CONVERT_ALL_TO_INR = True

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

STARTING_CASH = 10_000.00


# ── Helpers & Conversion Engines ──────────────────────────────────────────────
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":  return "NASDAQ"
    if ticker == "^NSEI":  return "NIFTY 50"
    if ticker.endswith(".NS"): return ticker.replace(".NS", "")
    return ticker

def get_live_usd_inr_rate() -> float:
    """Fetches high-availability realtime USD/INR exchange rate with solid default backup."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/INR=X?range=1d&interval=1m"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=4).json()
        rate = r["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(rate)
    except Exception:
        return 83.50  # Reliable market benchmark fallback

def interpret_asset_query(user_input: str) -> dict:
    # Completely removed any reference to AI in prompts and descriptions
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
        try:
            t = yf.Ticker(ticker)
            # Safe historical request handling: fall back to 2d range context if 1d segment dataset comes back empty
            hist = t.history(period="1d")
            if hist.empty:
                hist = t.history(period="2d")
                
            if hist.empty:
                return None
                
            price = float(hist['Close'].iloc[-1])
            low = float(hist['Low'].iloc[-1]) if 'Low' in hist else price
            high = float(hist['High'].iloc[-1]) if 'High' in hist else price
            prev = float(hist['Open'].iloc[-1]) if 'Open' in hist else price
            
            currency = "INR" if (".NS" in ticker or "NSEI" in ticker) else "USD"
            return {
                "symbol": ticker, 
                "price": price,
                "change": round(price - prev, 4), 
                "pct": round(((price - prev) / prev) * 100, 4) if prev else 0,
                "low": low, 
                "high": high, 
                "prev": prev,
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

    asset_meta = interpret_asset_query(q)
    if asset_meta.get("error") or not asset_meta.get("ticker"):
        return jsonify({"error": "Could not identify that stock or index."}), 404

    quote = fetch_live_quote(asset_meta["ticker"], asset_meta["provider"])
    if not quote:
        return jsonify({"error": "Failed to fetch live price. Try again."}), 404

    quote["cleanName"] = asset_meta.get("cleanName", asset_meta["ticker"])
    quote["assetClassDescription"] = asset_meta.get("description", "Market Asset")
    
    # Currency Transform Logic Layer
    if CONVERT_ALL_TO_INR:
        rate = get_live_usd_inr_rate()
        if quote["currency"] == "USD":
            quote["price"] *= rate
            quote["change"] *= rate
            quote["low"] *= rate
            quote["high"] *= rate
            quote["prev"] *= rate
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

    rate = get_live_usd_inr_rate() if CONVERT_ALL_TO_INR else 1.0
    currency_label = "INR" if CONVERT_ALL_TO_INR else "USD"

    # Convert cash balance base
    user_cash = user["cash"] * rate if CONVERT_ALL_TO_INR else user["cash"]
    
    invested_total = 0.0
    positions_list = []

    for sym, holding in user.get("holdings", {}).items():
        asset_meta = interpret_asset_query(sym)
        quote = fetch_live_quote(sym, asset_meta.get("provider", "finnhub"))

        # Base item cost basis is stored in USD matching execution history benchmarks
        current_price = quote["price"] if quote else holding["cost"]
        current_currency = quote["currency"] if quote else "USD"

        # Bring values into alignment context
        if CONVERT_ALL_TO_INR:
            if current_currency == "USD":
                current_price *= rate
            avg_cost_disp = holding["cost"] * rate
        else:
            if current_currency == "INR":
                current_price /= get_live_usd_inr_rate()
            avg_cost_disp = holding["cost"]

        mkt_val    = holding["shares"] * current_price
        cost_basis = holding["shares"] * avg_cost_disp
        gl         = mkt_val - cost_basis
        gl_pct     = ((current_price - avg_cost_disp) / avg_cost_disp) * 100 if avg_cost_disp else 0

        invested_total += mkt_val
        positions_list.append({
            "symbol":       asset_meta.get("cleanName", sym),
            "rawToken":     sym,
            "shares":       holding["shares"],
            "avgCost":      avg_cost_disp,
            "currentPrice": current_price,
            "marketValue":  mkt_val,
            "gainLoss":     gl,
            "gainLossPct":  gl_pct,
            "currency":     currency_label
        })

    net_val = user_cash + invested_total
    starting_benchmark = STARTING_CASH * rate if CONVERT_ALL_TO_INR else STARTING_CASH
    yield_pct = ((net_val - starting_benchmark) / starting_benchmark) * 100

    # Format history records matching conversion configuration layers
    formatted_history = []
    for log in user.get("history", []):
        item = dict(log)
        if CONVERT_ALL_TO_INR and item.get("currency") == "USD":
            item["price"] *= rate
            item["sum"] *= rate
            item["pl"] *= rate
            item["currency"] = "INR"
        elif not CONVERT_ALL_TO_INR and item.get("currency") == "INR":
            item["price"] /= rate
            item["sum"] /= rate
            item["pl"] /= rate
            item["currency"] = "USD"
        formatted_history.append(item)

    return jsonify({
        "cash":      user_cash,
        "invested":  invested_total,
        "netValue":  net_val,
        "returns":   yield_pct,
        "positions": positions_list,
        "history":   formatted_history
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
    price  = float(data.get("price", 0))  # Provided directly from active front UI currency state

    if qty <= 0 or price <= 0:
        return jsonify({"error": "Invalid quantity or price."}), 400

    user = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    holdings = user.get("holdings", {})
    cash     = user["cash"]
    
    rate = get_live_usd_inr_rate()
    
    # Standardize trade price down to base system storage units (USD)
    if CONVERT_ALL_TO_INR:
        price_usd = price / rate
    else:
        asset_meta = interpret_asset_query(symbol)
        quote = fetch_live_quote(symbol, asset_meta.get("provider", "finnhub"))
        native_curr = quote["currency"] if quote else "USD"
        price_usd = price / rate if native_curr == "INR" else price

    total_cost_usd = round(qty * price_usd, 6)
    clean_sym = get_clean_name_mapping(symbol)
    timestamp = now_str()
    pl_usd    = 0.0

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
        pl_usd       = round((price_usd - avg_cost_usd) * qty, 6)
        cash        += total_cost_usd
        holdings[symbol]["shares"] -= qty

        if holdings[symbol]["shares"] <= 1e-9:
            del holdings[symbol]

    currency_label = "INR" if CONVERT_ALL_TO_INR else ("INR" if (".NS" in symbol or "NSEI" in symbol) else "USD")
    display_price = price
    display_sum   = qty * price
    display_pl    = pl_usd * rate if CONVERT_ALL_TO_INR else pl_usd

    history_entry = {
        "date":        timestamp,
        "type":        "BUY" if mode == "buy" else "SELL",
        "symbol":      symbol,
        "cleanSymbol": clean_sym,
        "shares":      qty,
        "price":       display_price,
        "sum":         display_sum,
        "pl":          display_pl,
        "currency":    currency_label
    }

    users_col.update_one(
        {"username": session["user"]},
        {
            "$set":   {"cash": round(cash, 6), "holdings": holdings},
            "$push":  {"history": {"$each": [history_entry], "$position": 0}}
        }
    )

    # Returns structural validation confirmation fields to generate customized front-end modals securely
    return jsonify({
        "success": True, 
        "pl": display_pl, 
        "newCash": round(cash * rate if CONVERT_ALL_TO_INR else cash, 2),
        "currency": currency_label,
        "qty": qty,
        "mode": mode.upper(),
        "cleanSymbol": clean_sym
    })


# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
def leaderboard():
    board = []
    rate = get_live_usd_inr_rate() if CONVERT_ALL_TO_INR else 1.0
    currency_label = "INR" if CONVERT_ALL_TO_INR else "USD"
    
    for user in users_col.find({}, {"password": 0, "_id": 0}):
        # FIXED: Resolves historical holding flat-line bugs by computing live valuation metrics dynamically!
        live_invested_usd = 0.0
        for sym, holding in user.get("holdings", {}).items():
            asset_meta = interpret_asset_query(sym)
            quote = fetch_live_quote(sym, asset_meta.get("provider", "finnhub"))
            
            current_price = quote["price"] if quote else holding["cost"]
            current_currency = quote["currency"] if quote else "USD"
            
            if current_currency == "INR":
                current_price /= get_live_usd_inr_rate()
                
            live_invested_usd += holding["shares"] * current_price

        net_value_usd = user["cash"] + live_invested_usd
        returns_pct   = ((net_value_usd - STARTING_CASH) / STARTING_CASH) * 100
        trade_count   = len(user.get("history", []))
        
        display_net  = net_value_usd * rate
        display_cash = user["cash"] * rate

        board.append({
            "name":       user["displayName"],
            "handle":     user["username"],
            "cash":       display_cash,
            "netValue":   display_net,
            "returns":    returns_pct,
            "tradeCount": trade_count,
            "currency":   currency_label
        })

    board.sort(key=lambda x: x["returns"], reverse=True)
    return jsonify(board)


if __name__ == "__main__":
    app.run(debug=True)
