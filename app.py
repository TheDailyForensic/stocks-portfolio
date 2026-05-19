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
                "price":  r["c"],
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
            price = t.fast_info["last_price"]
            currency = "INR" if (".NS" in ticker or "NSEI" in ticker) else "USD"
            return {
                "symbol": ticker, "price": price,
                "change": 0, "pct": 0,
                "low": price, "high": price, "prev": price,
                "currency": currency
            }
        except Exception:
            return None

def get_user(username: str):
    return users_col.find_one({"username": username})

def safe_user_view(user: dict) -> dict:
    """Return user dict without password or Mongo _id."""
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
        "password":    password,   # NOTE: hash this in a real production app
        "cash":        STARTING_CASH,
        "holdings":    {},         # { "AAPL": {"shares": 5, "cost": 182.0} }
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

    ai = interpret_asset_query(q)
    if ai.get("error") or not ai.get("ticker"):
        return jsonify({"error": "Could not identify that stock or index."}), 404

    quote = fetch_live_quote(ai["ticker"], ai["provider"])
    if not quote:
        return jsonify({"error": "Failed to fetch live price. Try again."}), 404

    quote["cleanName"]           = ai.get("cleanName", ai["ticker"])
    quote["assetClassDescription"] = ai.get("description", "Market Asset")
    return jsonify(quote)


# ── Portfolio ─────────────────────────────────────────────────────────────────
@app.route("/api/user/portfolio")
def get_portfolio():
    if "user" not in session:
        return jsonify({"error": "Not logged in."}), 401

    user = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    invested       = 0.0
    positions_list = []

    for sym, holding in user.get("holdings", {}).items():
        # PERFORMANCE FIX: Inferred provider statically instead of looping LLM queries
        provider = "yahoo" if (".NS" in sym or "^" in sym) else "finnhub"
        quote = fetch_live_quote(sym, provider)

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
            "gainLossPct":  gl_pct
        })

    net_val    = user["cash"] + invested
    yield_pct  = ((net_val - STARTING_CASH) / STARTING_CASH) * 100

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
    if "user" not in session:
        return jsonify({"error": "Not logged in."}), 401

    data   = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    qty    = float(data.get("qty", 0))
    mode   = data.get("mode", "buy")
    price  = float(data.get("price", 0))

    if qty <= 0 or price <= 0:
        return jsonify({"error": "Invalid quantity or price."}), 400

    user = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    holdings   = user.get("holdings", {})
    cash       = user["cash"]
    total_cost = round(qty * price, 6)
    clean_sym  = get_clean_name_mapping(symbol)
    timestamp  = now_str()
    pl         = 0.0

    if mode == "buy":
        if cash < total_cost:
            return jsonify({"error": "Insufficient funds for this order."}), 400

        cash -= total_cost
        if symbol not in holdings:
            holdings[symbol] = {"shares": 0.0, "cost": 0.0}

        prev_shares = holdings[symbol]["shares"]
        prev_cost   = holdings[symbol]["cost"]
        new_shares  = prev_shares + qty
        # Weighted average cost basis
        holdings[symbol]["shares"] = new_shares
        holdings[symbol]["cost"]   = ((prev_shares * prev_cost) + total_cost) / new_shares

    else:  # sell
        owned = holdings.get(symbol, {}).get("shares", 0)
        if owned < qty - 1e-9:
            return jsonify({"error": f"You only own {owned:.4f} shares of {clean_sym}."}), 400

        avg_cost   = holdings[symbol]["cost"]
        pl         = round((price - avg_cost) * qty, 4)   # realised P/L
        cash      += total_cost
        holdings[symbol]["shares"] -= qty

        if holdings[symbol]["shares"] <= 1e-9:
            del holdings[symbol]

    history_entry = {
        "date":        timestamp,
        "type":        "BUY" if mode == "buy" else "SELL",
        "symbol":      symbol,
        "cleanSymbol": clean_sym,
        "shares":      qty,
        "price":       price,
        "sum":         total_cost,
        "pl":          pl
    }

    # Persist to MongoDB
    users_col.update_one(
        {"username": session["user"]},
        {
            "$set":   {"cash": round(cash, 6), "holdings": holdings},
            "$push":  {"history": {"$each": [history_entry], "$position": 0}}
        }
    )

    return jsonify({"success": True, "pl": pl, "newCash": round(cash, 2)})


# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
def leaderboard():
    board = []
    for user in users_col.find({}, {"password": 0, "_id": 0}):
        # Fixed loop math lookup safely using current fallback logic
        invested = sum(
            h["shares"] * h["cost"]
            for h in user.get("holdings", {}).values()
        )
        net_value   = user["cash"] + invested
        returns_pct = ((net_value - STARTING_CASH) / STARTING_CASH) * 100
        trade_count = len(user.get("history", []))
        board.append({
            "name":       user["displayName"],
            "handle":     user["username"],
            "cash":       user["cash"],
            "netValue":   net_value,
            "returns":    returns_pct,
            "tradeCount": trade_count
        })

    board.sort(key=lambda x: x["returns"], reverse=True)
    return jsonify(board)


if __name__ == "__main__":
    app.run(debug=True)
