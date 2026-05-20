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

GROQ_API_KEY    = os.environ.get("GROQ_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
MONGO_URI       = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

mongo_client = MongoClient(MONGO_URI)
db           = mongo_client["stocksim"]
users_col    = db["users"]
users_col.create_index("username", unique=True)

STARTING_CASH = 1_000_000.00   # USD base pool
INR_PER_USD   = 84.0           # Fallback rate; ideally fetch live

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def get_live_inr_rate() -> float:
    """Fetch live USD/INR rate from Yahoo Finance, fall back to constant."""
    try:
        t = yf.Ticker("INR=X")
        hist = t.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return INR_PER_USD

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":        return "NASDAQ"
    if ticker == "^NSEI":        return "NIFTY 50"
    if ticker == "^BSESN":       return "SENSEX"
    if ticker == "^DJI":         return "DJI"
    if ticker.endswith(".NS"):   return ticker.replace(".NS", "")
    if ticker.endswith(".BO"):   return ticker.replace(".BO", "")
    return ticker

def is_inr_asset(ticker: str) -> bool:
    return any(x in ticker for x in [".NS", ".BO", "^NSEI", "^BSESN"])

def interpret_asset_query(user_input: str) -> dict:
    system_instruction = """
You are a global financial data routing assistant. Take user inputs, fix typos, and return a standardised JSON object.

Rules:
1. US stocks/ETFs (TSLA, AAPL, NVDA, MSFT, AMZN, etc.): provider "finnhub", currency "USD".
2. Indian stocks on NSE: provider "yahoo", currency "INR", ticker appends ".NS" (e.g. RELIANCE.NS, TCS.NS, INFY.NS).
3. Nasdaq Composite index: ticker "^IXIC", provider "yahoo", currency "USD".
4. Nifty 50 index:        ticker "^NSEI", provider "yahoo", currency "INR".
5. BSE Sensex:            ticker "^BSESN", provider "yahoo", currency "INR".
6. Dow Jones:             ticker "^DJI",  provider "yahoo", currency "USD".

Respond ONLY with valid JSON:
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
        if any(cleaned.endswith(x) for x in [".NS", ".BO"]) or cleaned.startswith("^NSE") or cleaned.startswith("^BSE"):
            return {"ticker": cleaned, "provider": "yahoo", "currency": "INR",
                    "cleanName": cleaned, "description": "Indian Asset", "error": False}
        return {"ticker": cleaned, "provider": "finnhub", "currency": "USD",
                "cleanName": cleaned, "description": "US Asset", "error": False}

def fetch_live_quote(ticker: str, provider: str):
    if provider == "finnhub":
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
        try:
            r = requests.get(url, timeout=8).json()
            if not r.get("c"):
                return None
            return {
                "symbol":   ticker,
                "price":    float(r["c"]),
                "change":   round(r.get("d",  0), 4),
                "pct":      round(r.get("dp", 0), 4),
                "low":      float(r.get("l", r["c"])),
                "high":     float(r.get("h", r["c"])),
                "prev":     float(r.get("pc", r["c"])),
                "currency": "USD"
            }
        except Exception:
            return None
    else:  # yahoo
        try:
            t    = yf.Ticker(ticker)
            hist = t.history(period="2d")   # 2d gives us previous close too
            if hist.empty:
                return None
            price = float(hist["Close"].iloc[-1])
            prev  = float(hist["Close"].iloc[-2]) if len(hist) > 1 else float(hist["Open"].iloc[-1])
            low   = float(hist["Low"].iloc[-1])
            high  = float(hist["High"].iloc[-1])
            chg   = round(price - prev, 4)
            pct   = round((chg / prev) * 100, 4) if prev else 0
            currency = "INR" if is_inr_asset(ticker) else "USD"
            return {
                "symbol":   ticker,
                "price":    price,
                "change":   chg,
                "pct":      pct,
                "low":      low,
                "high":     high,
                "prev":     prev,
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

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth/register", methods=["POST"])
def register():
    data     = request.json or {}
    username = data.get("username", "").lower().strip()
    display  = data.get("displayName", "").strip()
    password = data.get("password", "").strip()
    if not username or not display or not password:
        return jsonify({"error": "Please fill in all fields."}), 400
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
    user     = get_user(username)
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
        return jsonify({"error": "Could not identify asset."}), 404
    quote = fetch_live_quote(ai["ticker"], ai["provider"])
    if not quote:
        return jsonify({"error": "Failed to fetch live price data."}), 404
    quote["cleanName"]             = ai.get("cleanName", ai["ticker"])
    quote["assetClassDescription"] = ai.get("description", "Market Security")
    quote["provider"]              = ai["provider"]
    # Also return live FX rate so frontend can convert on the fly
    quote["usdInrRate"]            = get_live_inr_rate()
    return jsonify(quote)

# ── Portfolio ─────────────────────────────────────────────────────────────────
@app.route("/api/user/portfolio")
def get_portfolio():
    if "user" not in session:
        return jsonify({"error": "Not logged in."}), 401
    user = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    inr_rate = get_live_inr_rate()   # 1 USD = X INR

    invested_usd   = 0.0
    positions_list = []

    for sym, holding in user.get("holdings", {}).items():
        prov  = "yahoo" if any(x in sym for x in [".NS", ".BO", "^"]) else "finnhub"
        quote = fetch_live_quote(sym, prov)

        current_price = quote["price"] if quote else holding["cost"]
        currency      = quote["currency"] if quote else ("INR" if is_inr_asset(sym) else "USD")

        mkt_val_local = holding["shares"] * current_price
        cost_local    = holding["shares"] * holding["cost"]
        gl_local      = mkt_val_local - cost_local

        # Convert everything to USD for portfolio math
        divisor       = inr_rate if currency == "INR" else 1.0
        mkt_val_usd   = mkt_val_local / divisor
        invested_usd += mkt_val_usd

        gl_pct = ((current_price - holding["cost"]) / holding["cost"]) * 100 if holding["cost"] else 0

        positions_list.append({
            "symbol":        get_clean_name_mapping(sym),
            "rawToken":      sym,
            "shares":        holding["shares"],
            "avgCost":       holding["cost"],         # in native currency
            "currentPrice":  current_price,           # in native currency
            "marketValue":   mkt_val_local,           # in native currency
            "marketValueUsd": mkt_val_usd,            # always USD
            "gainLoss":      gl_local,                # in native currency
            "gainLossUsd":   gl_local / divisor,      # always USD
            "gainLossPct":   gl_pct,
            "currency":      currency
        })

    net_val_usd = user["cash"] + invested_usd
    yield_pct   = ((net_val_usd - STARTING_CASH) / STARTING_CASH) * 100

    return jsonify({
        "cash":       user["cash"],
        "invested":   invested_usd,          # USD
        "netValue":   net_val_usd,            # USD
        "returns":    yield_pct,
        "usdInrRate": inr_rate,
        "positions":  positions_list,
        "history":    user.get("history", [])
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
    price  = float(data.get("price", 0))   # always in native currency of the asset

    if qty <= 0 or price <= 0:
        return jsonify({"error": "Invalid parameters."}), 400

    user     = get_user(session["user"])
    if not user:
        return jsonify({"error": "Account not found."}), 401

    holdings   = user.get("holdings", {})
    cash       = user["cash"]  # always USD

    # Convert trade cost to USD for cash debit/credit
    inr_rate   = get_live_inr_rate()
    divisor    = inr_rate if is_inr_asset(symbol) else 1.0
    cost_local = round(qty * price, 6)
    cost_usd   = round(cost_local / divisor, 6)
    clean_sym  = get_clean_name_mapping(symbol)

    if mode == "buy":
        if cash < cost_usd:
            return jsonify({"error": f"Insufficient funds. Need ${cost_usd:.2f} USD, have ${cash:.2f} USD."}), 400
        cash -= cost_usd
        if symbol not in holdings:
            holdings[symbol] = {"shares": 0.0, "cost": 0.0}
        prev_shares = holdings[symbol]["shares"]
        prev_cost   = holdings[symbol]["cost"]
        new_shares  = prev_shares + qty
        # avg cost stored in native currency
        holdings[symbol]["shares"] = new_shares
        holdings[symbol]["cost"]   = ((prev_shares * prev_cost) + cost_local) / new_shares
    else:
        owned = holdings.get(symbol, {}).get("shares", 0)
        if owned < qty - 1e-9:
            return jsonify({"error": "Insufficient shares."}), 400
        cash += cost_usd
        holdings[symbol]["shares"] -= qty
        if holdings[symbol]["shares"] <= 1e-9:
            del holdings[symbol]

    currency = "INR" if is_inr_asset(symbol) else "USD"
    history_entry = {
        "date":        now_str(),
        "type":        "BUY" if mode == "buy" else "SELL",
        "symbol":      symbol,
        "cleanSymbol": clean_sym,
        "shares":      qty,
        "price":       price,       # native currency
        "sum":         cost_local,  # native currency
        "sumUsd":      cost_usd,    # USD equivalent
        "currency":    currency
    }

    users_col.update_one(
        {"username": session["user"]},
        {
            "$set":  {"cash": round(cash, 6), "holdings": holdings},
            "$push": {"history": {"$each": [history_entry], "$position": 0}}
        }
    )
    return jsonify({"success": True, "newCash": round(cash, 2)})

# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
def leaderboard():
    inr_rate = get_live_inr_rate()
    all_users = list(users_col.find({}))
    board = []
    for u in all_users:
        invested_usd = 0.0
        for sym, h in u.get("holdings", {}).items():
            prov  = "yahoo" if any(x in sym for x in [".NS", ".BO", "^"]) else "finnhub"
            quote = fetch_live_quote(sym, prov)
            if quote:
                price_usd = quote["price"] / (inr_rate if quote["currency"] == "INR" else 1.0)
                invested_usd += h["shares"] * price_usd
            else:
                cost_usd = h["cost"] / (inr_rate if is_inr_asset(sym) else 1.0)
                invested_usd += h["shares"] * cost_usd
        net  = u["cash"] + invested_usd
        ret  = ((net - STARTING_CASH) / STARTING_CASH) * 100
        board.append({"name": u["displayName"], "handle": u["username"],
                      "cash": round(net, 2), "returns": round(ret, 2)})
    board.sort(key=lambda x: x["returns"], reverse=True)
    return jsonify(board)

if __name__ == "__main__":
    app.run(debug=True)
