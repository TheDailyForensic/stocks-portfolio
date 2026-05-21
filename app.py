import os
import json
import requests
import yfinance as yf
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor, as_completed

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

STARTING_CASH = 10_000.00
INR_PER_USD   = 84.0

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def get_live_inr_rate() -> float:
    try:
        t    = yf.Ticker("INR=X")
        hist = t.history(period="1d")
        if not hist.empty:
            hist = hist[~hist.index.duplicated(keep="first")]
            hist = hist.sort_index(ascending=True)
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return INR_PER_USD

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":      return "NASDAQ"
    if ticker == "^NSEI":      return "NIFTY 50"
    if ticker == "^BSESN":     return "SENSEX"
    if ticker == "^DJI":       return "DJI"
    if ticker.endswith(".NS"): return ticker.replace(".NS", "")
    if ticker.endswith(".BO"): return ticker.replace(".BO", "")
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
        if any(cleaned.endswith(x) for x in [".NS", ".BO"]) or "^NSE" in cleaned or "^BSE" in cleaned:
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
            hist = t.history(period="2d")
            if hist.empty:
                return None
            hist  = hist[~hist.index.duplicated(keep="first")]
            hist  = hist.sort_index(ascending=True)
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
    """Return a flat user object the frontend can assign directly to activeUser."""
    return {
        "username":    user["username"],
        "displayName": user["displayName"],
        "cash":        user["cash"]
    }

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth/register", methods=["POST"])
def register():
    data        = request.json or {}
    username    = data.get("username", "").lower().strip()
    display     = data.get("displayName", "").strip()
    password    = data.get("password", "")

    if not username or not display or len(password) < 4:
        return jsonify({"error": "Invalid fields or password too short (min 4 chars)"}), 400

    if get_user(username):
        return jsonify({"error": "Username already taken"}), 400

    user_doc = {
        "username":    username,
        "displayName": display,
        "password":    password,
        "cash":        STARTING_CASH,
        "holdings":    {},
        "history":     []
    }
    users_col.insert_one(user_doc)
    session["user"] = username
    # ✅ FIX: return flat user object so frontend activeUser.cash / .displayName work
    return jsonify(safe_user_view(user_doc))

@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = data.get("username", "").lower().strip()
    password = data.get("password", "")

    u = get_user(username)
    if not u or u["password"] != password:
        return jsonify({"error": "Invalid username or password"}), 401

    session["user"] = username
    # ✅ FIX: return flat user object
    return jsonify(safe_user_view(u))

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"success": True})

@app.route("/api/auth/session")
def get_session():
    if "user" in session:
        u = get_user(session["user"])
        if u:
            return jsonify({"authenticated": True, "user": safe_user_view(u)})
    return jsonify({"authenticated": False})

# ── Market Query  (GET /api/market/query?query=...) ───────────────────────────
# ✅ FIX: frontend calls GET /api/market/query — this route was missing entirely
@app.route("/api/market/query")
def market_query():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "Missing query parameter"}), 400

    routing = interpret_asset_query(query)
    if routing.get("error") or not routing.get("ticker"):
        return jsonify({"error": "Could not identify stock symbol"}), 404

    ticker   = routing["ticker"]
    provider = routing["provider"]
    quote    = fetch_live_quote(ticker, provider)

    if not quote:
        return jsonify({"error": f"Live price unavailable for '{ticker}'. Market may be closed or symbol not found."}), 404

    inr_rate = get_live_inr_rate()

    # Return shape the frontend expects on tradeAsset
    return jsonify({
        "symbol":               ticker,
        "cleanName":            routing.get("cleanName", get_clean_name_mapping(ticker)),
        "assetClassDescription": routing.get("description", ""),
        "price":                quote["price"],
        "change":               quote["change"],
        "pct":                  quote["pct"],
        "high":                 quote["high"],
        "low":                  quote["low"],
        "currency":             quote["currency"],
        "provider":             provider,
        "usdInrRate":           inr_rate
    })

# ── Chart Proxy ───────────────────────────────────────────────────────────────
@app.route("/api/chart/yahoo")
def chart_yahoo():
    symbol   = request.args.get("symbol", "").strip()
    interval = request.args.get("interval", "1d")
    period   = request.args.get("period",   "max")

    if not symbol:
        return jsonify({"error": "No symbol"}), 400

    safe_intervals = {"1m","2m","5m","15m","30m","60m","90m","1h","1d","5d","1wk","1mo","3mo"}
    safe_periods   = {"1d","5d","1mo","3mo","6mo","1y","2y","5y","10y","ytd","max"}
    if interval not in safe_intervals: interval = "1d"
    if period   not in safe_periods:   period   = "max"

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={period}&events=div%2Csplit")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StockSim/1.0)",
        "Accept":     "application/json"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if not resp.ok:
            url2 = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
                    f"?interval={interval}&range={period}")
            resp = requests.get(url2, headers=headers, timeout=15)
        data = resp.json()

        try:
            if data.get("chart", {}).get("result"):
                res_obj    = data["chart"]["result"][0]
                timestamps = res_obj.get("timestamp", [])
                if timestamps:
                    ind     = res_obj["indicators"]["quote"][0]
                    zipped  = []
                    for i, t in enumerate(timestamps):
                        if t is None: continue
                        zipped.append({
                            "t": t,
                            "o": ind.get("open",   [None]*len(timestamps))[i],
                            "h": ind.get("high",   [None]*len(timestamps))[i],
                            "l": ind.get("low",    [None]*len(timestamps))[i],
                            "c": ind.get("close",  [None]*len(timestamps))[i],
                            "v": ind.get("volume", [None]*len(timestamps))[i],
                        })
                    zipped.sort(key=lambda x: x["t"])
                    seen, clean = set(), []
                    for item in zipped:
                        if item["t"] not in seen:
                            seen.add(item["t"])
                            clean.append(item)
                    res_obj["timestamp"]                          = [x["t"] for x in clean]
                    res_obj["indicators"]["quote"][0]["open"]     = [x["o"] for x in clean]
                    res_obj["indicators"]["quote"][0]["high"]     = [x["h"] for x in clean]
                    res_obj["indicators"]["quote"][0]["low"]      = [x["l"] for x in clean]
                    res_obj["indicators"]["quote"][0]["close"]    = [x["c"] for x in clean]
                    res_obj["indicators"]["quote"][0]["volume"]   = [x["v"] for x in clean]
        except Exception:
            pass

        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Portfolio ─────────────────────────────────────────────────────────────────
# ✅ FIX: frontend calls GET /api/user/portfolio — renamed from /api/portfolio/sync
@app.route("/api/user/portfolio")
def user_portfolio():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    u = get_user(session["user"])
    if not u:
        return jsonify({"error": "User not found"}), 404

    inr_rate = get_live_inr_rate()
    holdings = u.get("holdings", {})
    history  = u.get("history", [])
    cash     = u["cash"]

    positions      = []
    invested_usd   = 0.0

    for sym, h in holdings.items():
        prov  = "yahoo" if is_inr_asset(sym) else "finnhub"
        quote = fetch_live_quote(sym, prov)

        shares         = h["shares"]
        avg_cost_local = h["cost"]

        if quote:
            curr_price_local = quote["price"]
            asset_currency   = quote["currency"]
        else:
            curr_price_local = avg_cost_local
            asset_currency   = "INR" if is_inr_asset(sym) else "USD"

        divisor = inr_rate if asset_currency == "INR" else 1.0

        avg_cost_usd    = avg_cost_local / divisor
        curr_price_usd  = curr_price_local / divisor
        market_value_usd = shares * curr_price_usd
        cost_basis_usd   = shares * avg_cost_usd
        gain_loss_usd    = market_value_usd - cost_basis_usd
        gain_loss_pct    = (gain_loss_usd / cost_basis_usd * 100.0) if cost_basis_usd else 0.0
        invested_usd    += market_value_usd

        positions.append({
            "rawToken":     sym,
            "symbol":       get_clean_name_mapping(sym),
            "shares":       shares,
            "avgCost":      avg_cost_local,
            "currentPrice": curr_price_local,
            "marketValue":  market_value_usd * divisor,
            "gainLoss":     gain_loss_usd * divisor,
            "gainLossPct":  gain_loss_pct,
            "currency":     asset_currency
        })

    net_value_usd   = cash + invested_usd
    total_returns   = ((net_value_usd - STARTING_CASH) / STARTING_CASH) * 100.0

    # ✅ FIX: include usdInrRate so frontend currency converter stays accurate
    return jsonify({
        "cash":       cash,
        "invested":   invested_usd,
        "netValue":   net_value_usd,
        "returns":    total_returns,
        "usdInrRate": inr_rate,
        "positions":  positions,
        "history":    history[:40]
    })

# ── Trade Execute ─────────────────────────────────────────────────────────────
@app.route("/api/trade/execute", methods=["POST"])
def trade_execute():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    u = get_user(session["user"])
    if not u:
        return jsonify({"error": "User not found"}), 404

    data     = request.json or {}
    symbol   = data.get("symbol", "").strip().upper()
    mode     = data.get("mode", "buy").lower()
    qty      = float(data.get("qty", 0))
    price    = float(data.get("price", 0))

    if qty <= 0 or price <= 0 or mode not in ["buy", "sell"]:
        return jsonify({"error": "Invalid transaction values"}), 400

    holdings  = u.get("holdings", {})
    cash      = u["cash"]
    inr_rate  = get_live_inr_rate()
    divisor   = inr_rate if is_inr_asset(symbol) else 1.0

    cost_local = round(qty * price, 6)
    cost_usd   = round(cost_local / divisor, 6)
    clean_sym  = get_clean_name_mapping(symbol)

    if mode == "buy":
        if cash < cost_usd:
            return jsonify({"error": f"Insufficient funds. Need ${cost_usd:.2f}, have ${cash:.2f}."}), 400
        cash -= cost_usd
        if symbol not in holdings:
            holdings[symbol] = {"shares": qty, "cost": price}
        else:
            old_shares = holdings[symbol]["shares"]
            old_cost   = holdings[symbol]["cost"]
            new_shares = old_shares + qty
            new_cost   = ((old_shares * old_cost) + cost_local) / new_shares
            holdings[symbol] = {"shares": new_shares, "cost": round(new_cost, 6)}
    else:
        if symbol not in holdings or holdings[symbol]["shares"] < qty:
            return jsonify({"error": "Insufficient shares to sell."}), 400
        cash += cost_usd
        holdings[symbol]["shares"] = round(holdings[symbol]["shares"] - qty, 8)
        if holdings[symbol]["shares"] <= 0.0001:
            holdings.pop(symbol, None)

    history_entry = {
        "date":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "type":        mode.upper(),
        "cleanSymbol": clean_sym,
        "shares":      qty,
        "price":       price,
        "sum":         cost_local,
        # ✅ FIX: include sumUsd so history table USD column renders correctly
        "sumUsd":      cost_usd,
        "currency":    "INR" if is_inr_asset(symbol) else "USD"
    }
    # Snapshot for leaderboard — updates on every trade, no live API calls needed
snap_invested = 0.0
for s, h in holdings.items():
    snap_invested += h["shares"] * h["cost"] / (inr_rate if is_inr_asset(s) else 1.0)
snap_net = round(cash + snap_invested, 2)
snap_ret = round(((snap_net - STARTING_CASH) / STARTING_CASH) * 100.0, 4)
    users_col.update_one(
        {"username": session["user"]},
        {
            "$set": {
    "cash": round(cash, 6),
    "holdings": holdings,
    "snapshot": {"net": snap_net, "ret": snap_ret}   # ← add this line
},
            "$push": {"history": {"$each": [history_entry], "$position": 0}}
        }
    )
    return jsonify({"success": True, "newCash": round(cash, 2)})

# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
def leaderboard():
    all_users = list(users_col.find({}))
    board = []
    for u in all_users:
        snap = u.get("snapshot", {})
        net  = snap.get("net", u["cash"])
        ret  = snap.get("ret", ((u["cash"] - STARTING_CASH) / STARTING_CASH) * 100.0)
        board.append({
            "name": u["displayName"], "handle": u["username"],
            "cash": u["cash"], "netValue": net, "returns": ret
        })
    board.sort(key=lambda x: x["netValue"], reverse=True)
    return jsonify(board)
