import os
import json
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_in_prod")
app.permanent_session_lifetime = timedelta(days=30) # Keep users logged in for 30 days

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
STARTING_CASH = 10_000.00

# ── Helpers ───────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":  return "NASDAQ"
    if ticker == "^NSEI":  return "NIFTY 50"
    if ticker.endswith(".NS"): return ticker.replace(".NS", "")
    return ticker

def interpret_asset_query(user_input: str) -> dict:
    system_instruction = """
    You are a financial data routing assistant. Take user inputs, fix typos, and return a standardized JSON object.
    Rules:
    1. For US stocks/ETFs, provider is 'finnhub', ticker is standard uppercase (e.g. AAPL, NVDA).
    2. For Indian stocks on NSE, provider is 'yahoo', ticker appends '.NS' (e.g. RELIANCE.NS).
    3. For Nasdaq Index: ticker '^IXIC', provider 'yahoo'.
    4. For Crypto: provider 'yahoo', ticker appends '-USD' (e.g. BTC-USD).
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
        return {"ticker": "", "provider": "", "cleanName": "Unknown", "description": "Asset", "error": True}

def fetch_live_quote(ticker: str, provider: str):
    if provider == "finnhub":
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
        try:
            r = requests.get(url, timeout=8).json()
            if not r.get("c"): return None
            current_hour = datetime.now(timezone.utc).hour
            current_day = datetime.now(timezone.utc).weekday()
            is_open = (current_day < 5) and (13 <= current_hour <= 20)
            
            return {
                "symbol": ticker, "price": r["c"], "change": round(r.get("d", 0), 4),
                "pct": round(r.get("dp", 0), 4), "low": r.get("l", r["c"]), "high": r.get("h", r["c"]),
                "currency": "USD", "marketState": "OPEN" if is_open else "CLOSED"
            }
        except Exception:
            return None
    else:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            price = info.get("regularMarketPrice") or t.fast_info["last_price"]
            change = info.get("regularMarketChange") or 0.0
            pct = info.get("regularMarketChangePercent") or 0.0
            low = info.get("regularMarketDayLow") or price
            high = info.get("regularMarketDayHigh") or price
            
            raw_state = info.get("marketState", "CLOSED")
            market_state = "OPEN" if raw_state in ["REGULAR", "OPEN"] else "CLOSED"
            # Crypto is always open
            if "-USD" in ticker: market_state = "OPEN (24/7)"
            
            return {
                "symbol": ticker, "price": price, "change": round(change, 4), 
                "pct": round(pct, 4), "low": low, "high": high, 
                "currency": "USD" if "-USD" in ticker else "INR" if ".NS" in ticker else "USD",
                "marketState": market_state
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


# ── Auth & Account Routes ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth/me")
def auth_me():
    # Auto-login endpoint
    if "user" in session:
        user = get_user(session["user"])
        if user:
            return jsonify(safe_user_view(user))
    return jsonify({"error": "Not authenticated"}), 401

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.json or {}
    username = data.get("username", "").lower().strip()
    display = data.get("displayName", "").strip()
    password = data.get("password", "").strip()

    if not username or not display or not password: return jsonify({"error": "Please fill in all fields."}), 400
    if len(password) < 4: return jsonify({"error": "Password must be at least 4 characters."}), 400

    new_user = {
        "username": username, "displayName": display, "password": password,   
        "cash": STARTING_CASH, "holdings": {}, "history": [],
        "settings": {"leaderboard": True, "public_portfolio": False, "theme": "dark", "currency": "USD"},
        "created_at": now_str()
    }
    try:
        users_col.insert_one(new_user)
    except Exception:
        return jsonify({"error": "Username is already taken."}), 400

    session.permanent = True
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

    session.permanent = True
    session["user"] = username
    return jsonify(safe_user_view(user))

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"success": True})

# ── Settings Management ───────────────────────────────────────────────────────
@app.route("/api/user/settings", methods=["POST"])
def update_settings():
    if "user" not in session: return jsonify({"error": "Not logged in"}), 401
    data = request.json or {}
    
    update_fields = {}
    if "displayName" in data: update_fields["displayName"] = data["displayName"]
    if "password" in data and len(data["password"]) >= 4: update_fields["password"] = data["password"]
    
    # Update nested settings block
    settings_updates = {}
    if "leaderboard" in data: settings_updates["settings.leaderboard"] = data["leaderboard"]
    if "theme" in data: settings_updates["settings.theme"] = data["theme"]
    
    if settings_updates: update_fields.update(settings_updates)
    
    if update_fields:
        users_col.update_one({"username": session["user"]}, {"$set": update_fields})
        
    return jsonify({"success": True})

@app.route("/api/user/delete", methods=["POST"])
def delete_account():
    if "user" not in session: return jsonify({"error": "Not logged in"}), 401
    users_col.delete_one({"username": session["user"]})
    session.pop("user", None)
    return jsonify({"success": True})


# ── Market & Portfolio Data ───────────────────────────────────────────────────
@app.route("/api/market/query")
def query_market():
    q = request.args.get("query", "").strip()
    ai = interpret_asset_query(q)
    if ai.get("error") or not ai.get("ticker"): return jsonify({"error": "Asset not found."}), 404

    quote = fetch_live_quote(ai["ticker"], ai["provider"])
    if not quote: return jsonify({"error": "Failed to fetch price."}), 404

    quote["cleanName"] = ai.get("cleanName", ai["ticker"])
    quote["assetClassDescription"] = ai.get("description", "Market Asset")
    return jsonify(quote)

@app.route("/api/user/portfolio")
def get_portfolio():
    if "user" not in session: return jsonify({"error": "Not logged in"}), 401
    user = get_user(session["user"])
    
    invested, positions_list = 0.0, []
    for sym, holding in user.get("holdings", {}).items():
        ai = interpret_asset_query(sym)
        quote = fetch_live_quote(sym, ai.get("provider", "finnhub"))
        current_price = quote["price"] if quote else holding["cost"]
        mkt_val = holding["shares"] * current_price
        gl = mkt_val - (holding["shares"] * holding["cost"])
        
        invested += mkt_val
        positions_list.append({
            "symbol": ai.get("cleanName", sym), "rawToken": sym, "shares": holding["shares"],
            "avgCost": holding["cost"], "currentPrice": current_price, "marketValue": mkt_val, "gainLoss": gl
        })

    net_val = user["cash"] + invested
    return jsonify({
        "cash": user["cash"], "invested": invested, "netValue": net_val,
        "returns": ((net_val - STARTING_CASH) / STARTING_CASH) * 100,
        "positions": positions_list, "history": user.get("history", []),
        "settings": user.get("settings", {})
    })

@app.route("/api/trade/execute", methods=["POST"])
def execute_trade():
    if "user" not in session: return jsonify({"error": "Not logged in"}), 401
    data = request.json or {}
    symbol, qty, mode, price = data.get("symbol", "").upper(), float(data.get("qty", 0)), data.get("mode", "buy"), float(data.get("price", 0))

    if qty <= 0 or price <= 0: return jsonify({"error": "Invalid trade details."}), 400
    user = get_user(session["user"])
    holdings, cash, total_cost, clean_sym, pl = user.get("holdings", {}), user["cash"], round(qty * price, 6), get_clean_name_mapping(symbol), 0.0

    if mode == "buy":
        if cash < total_cost: return jsonify({"error": "Insufficient funds."}), 400
        cash -= total_cost
        if symbol not in holdings: holdings[symbol] = {"shares": 0.0, "cost": 0.0}
        prev_s, prev_c = holdings[symbol]["shares"], holdings[symbol]["cost"]
        holdings[symbol]["shares"] = prev_s + qty
        holdings[symbol]["cost"] = ((prev_s * prev_c) + total_cost) / (prev_s + qty)
    else:
        owned = holdings.get(symbol, {}).get("shares", 0)
        if owned < qty - 1e-9: return jsonify({"error": f"You only own {owned:.4f} shares."}), 400
        pl = round((price - holdings[symbol]["cost"]) * qty, 4)   
        cash += total_cost
        holdings[symbol]["shares"] -= qty
        if holdings[symbol]["shares"] <= 1e-9: del holdings[symbol]

    users_col.update_one(
        {"username": session["user"]},
        {"$set": {"cash": round(cash, 6), "holdings": holdings},
         "$push": {"history": {"$each": [{"date": now_str(), "type": mode.upper(), "symbol": symbol, "cleanSymbol": clean_sym, "shares": qty, "price": price, "sum": total_cost, "pl": pl}], "$position": 0}}}
    )
    return jsonify({"success": True})

@app.route("/api/leaderboard")
def leaderboard():
    board = []
    for user in users_col.find({}, {"password": 0, "_id": 0}):
        # Respect privacy setting
        if not user.get("settings", {}).get("leaderboard", True): continue
        
        invested = sum(h["shares"] * h["cost"] for h in user.get("holdings", {}).values())
        net_value = user["cash"] + invested
        board.append({
            "name": user["displayName"], "handle": user["username"], "cash": user["cash"],
            "netValue": net_value, "returns": ((net_value - STARTING_CASH) / STARTING_CASH) * 100
        })
    board.sort(key=lambda x: x["returns"], reverse=True)
    return jsonify(board)

if __name__ == "__main__":
    app.run(debug=True)
