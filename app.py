import os
import json
import requests
import yfinance as yf
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_in_prod")

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
users_col.create_index("username", unique=True)

STARTING_CASH = 1000000.00  # Elevated pool to accommodate multi-currency trading

# ── Helpers ───────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":  return "NASDAQ"
    if ticker == "^NSEI":  return "NIFTY 50"
    if ticker.endswith(".NS"): return ticker.replace(".NS", "")
    if ticker.endswith(".BO"): return ticker.replace(".BO", "")
    return ticker

def interpret_asset_query(user_input: str) -> dict:
    system_instruction = """
    You are a global financial data routing assistant. Take user inputs, fix typos, and return a standardized JSON object.

    Rules:
    1. For US stocks/ETFs (e.g. TSLA, AAPL, NVDA), provider is 'finnhub', currency is 'USD', ticker is standard uppercase.
    2. For Indian stocks on NSE, provider is 'yahoo', currency is 'INR', ticker appends '.NS' (e.g. RELIANCE.NS, TCS.NS).
    3. For Nasdaq Index: ticker '^IXIC', provider 'yahoo', currency 'USD'.
    4. For Nifty 50:     ticker '^NSEI', provider 'yahoo', currency 'INR'.

    Respond ONLY with valid JSON matching this exact schema:
    {"ticker":"STRING","provider":"finnhub or yahoo","currency":"USD or INR","cleanName":"STRING","description":"STRING","error":false}
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
        cleaned = user_input.strip().upper()
        if any(cleaned.endswith(x) for x in [".NS", ".BO"]) or cleaned.startswith("^NSE"):
            return {"ticker": cleaned, "provider": "yahoo", "currency": "INR", "cleanName": cleaned, "description": "Indian Asset", "error": False}
        return {"ticker": cleaned, "provider": "finnhub", "currency": "USD", "cleanName": cleaned, "description": "US Asset", "error": False}

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
                "low":    float(r.get("l", r["c"])),
                "high":   float(r.get("h", r["c"])),
                "prev":   float(r.get("pc", r["c"])),
                "currency": "USD"
            }
        except Exception:
            return None
    else:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="1d")
            if hist.empty: return None
            price = float(hist['Close'].iloc[-1])
            low = float(hist['Low'].iloc[-1]) if 'Low' in hist else price
            high = float(hist['High'].iloc[-1]) if 'High' in hist else price
            prev = float(hist['Open'].iloc[-1]) if 'Open' in hist else price
            currency = "INR" if (".NS" in ticker or "NSEI" in ticker or ".BO" in ticker or "BSESN" in ticker) else "USD"
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
    data = request.json or {}
    username = data.get("username", "").lower().strip()
    display = data.get("displayName", "").strip()
    password = data.get("password", "").strip()

    if not username or not display or not password:
        return jsonify({"error": "Please fill in all fields."}), 400

    new_user = {
        "username": username,
        "displayName": display,
        "password": password,
        "cash": STARTING_CASH,
        "holdings": {},
        "history": [],
        "created_at": now_str()
    }
    try:
        users_col.insert_one(new_user)
    except Exception:
        return jsonify({"error": "Username is already taken."}), 400

    session["user"] = username
    return jsonify(safe_user_view(new_user))

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json or {}
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
    if not q: return jsonify({"error": "No query provided."}), 400

    ai = interpret_asset_query(q)
    if ai.get("error") or not ai.get("ticker"):
        return jsonify({"error": "Could not identify asset query."}), 404

    quote = fetch_live_quote(ai["ticker"], ai["provider"])
    if not quote:
        return jsonify({"error": "Failed to fetch live tracking metrics."}), 404

    quote["cleanName"] = ai.get("cleanName", ai["ticker"])
    quote["assetClassDescription"] = ai.get("description", "Market Security")
    quote["provider"] = ai["provider"]
    return jsonify(quote)

# ── Portfolio ─────────────────────────────────────────────────────────────────
@app.route("/api/user/portfolio")
def get_portfolio():
    if "user" not in session: return jsonify({"error": "Not logged in."}), 401
    user = get_user(session["user"])
    if not user: return jsonify({"error": "Account not found."}), 401

    invested = 0.0
    positions_list = []

    for sym, holding in user.get("holdings", {}).items():
        # Identify whether ticker uses finnhub or yahoo based on format patterns
        prov = "yahoo" if any(x in sym for x in [".NS", ".BO", "^"]) else "finnhub"
        quote = fetch_live_quote(sym, prov)

        current_price = quote["price"] if quote else holding["cost"]
        mkt_val       = holding["shares"] * current_price
        cost_basis    = holding["shares"] * holding["cost"]
        gl            = mkt_val - cost_basis
        gl_pct        = ((current_price - holding["cost"]) / holding["cost"]) * 100 if holding["cost"] else 0

        invested += mkt_val
        positions_list.append({
            "symbol":       get_clean_name_mapping(sym),
            "rawToken":     sym,
            "shares":       holding["shares"],
            "avgCost":      holding["cost"],
            "currentPrice": current_price,
            "marketValue":  mkt_val,
            "gainLoss":     gl,
            "gainLossPct":  gl_pct,
            "currency":     quote["currency"] if quote else "USD"
        })

    net_val = user["cash"] + invested
    yield_pct = ((net_val - STARTING_CASH) / STARTING_CASH) * 100

    return jsonify({
        "cash":      user["cash"],
        "invested":  invested,
        "netValue":  net_val,
        "returns":   yield_pct,
        "positions": positions_list,
        "history":   user.get("history", [])
    })

# ── Trading ───────────────────────────────────────────────────────────────────
@app.route("/api/trade/execute", methods=["POST"])
def execute_trade():
    if "user" not in session: return jsonify({"error": "Not logged in."}), 401
    data = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    qty = float(data.get("qty", 0))
    mode = data.get("mode", "buy")
    price = float(data.get("price", 0))

    if qty <= 0 or price <= 0: return jsonify({"error": "Invalid metrics parameters."}), 400
    user = get_user(session["user"])
    if not user: return jsonify({"error": "Account structurally detached."}), 401

    holdings = user.get("holdings", {})
    cash = user["cash"]
    total_cost = round(qty * price, 6)
    clean_sym = get_clean_name_mapping(symbol)
    
    if mode == "buy":
        if cash < total_cost: return jsonify({"error": "Insufficient account funds."}), 400
        cash -= total_cost
        if symbol not in holdings: holdings[symbol] = {"shares": 0.0, "cost": 0.0}
        prev_shares = holdings[symbol]["shares"]
        prev_cost = holdings[symbol]["cost"]
        new_shares = prev_shares + qty
        holdings[symbol]["shares"] = new_shares
        holdings[symbol]["cost"] = ((prev_shares * prev_cost) + total_cost) / new_shares
    else:
        owned = holdings.get(symbol, {}).get("shares", 0)
        if owned < qty - 1e-9: return jsonify({"error": f"Insufficient shares available."}), 400
        cash += total_cost
        holdings[symbol]["shares"] -= qty
        if holdings[symbol]["shares"] <= 1e-9: del holdings[symbol]

    currency = "INR" if any(x in symbol for x in [".NS", ".BO", "^NSE", "^BSE"]) else "USD"
    history_entry = {
        "date": now_str(),
        "type": "BUY" if mode == "buy" else "SELL",
        "symbol": symbol,
        "cleanSymbol": clean_sym,
        "shares": qty,
        "price": price,
        "sum": total_cost,
        "currency": currency
    }

    users_col.update_one(
        {"username": session["user"]},
        {"$set": {"cash": round(cash, 6), "holdings": holdings}, "$push": {"history": {"$each": [history_entry], "$position": 0}}}
    )
    return jsonify({"success": True, "newCash": round(cash, 2)})

if __name__ == "__main__":
    app.run(debug=True)
