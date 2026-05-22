import os
import json
import requests
import yfinance as yf
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_in_prod")

app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

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
INR_PER_USD   = 91.0

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ── Live FX Rates ─────────────────────────────────────────────────────────────
# Map of currency code -> Yahoo ticker
FX_TICKERS = {
    "INR": "INR=X",
    "EUR": "EURUSD=X",
    "GBP": "GBPUSD=X",
    "JPY": "JPY=X",
    "CNY": "CNY=X",
    "SGD": "SGD=X",
    "AUD": "AUD=X",
    "CAD": "CAD=X",
    "HKD": "HKD=X",
    "CHF": "CHF=X",
    "KRW": "KRW=X",
    "MXN": "MXN=X",
    "BRL": "BRL=X",
    "AED": "AED=X",
    "THB": "THB=X",
    "SEK": "SEK=X",
    "NOK": "NOK=X",
}

# Fallback rates (units per 1 USD)
FALLBACK_RATES = {
    "INR": 91.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "JPY": 149.0,
    "CNY": 7.24,
    "SGD": 1.34,
    "AUD": 1.53,
    "CAD": 1.36,
    "HKD": 7.82,
    "CHF": 0.90,
    "KRW": 1320.0,
    "MXN": 17.5,
    "BRL": 4.95,
    "AED": 3.67,
    "THB": 35.5,
    "SEK": 10.5,
    "NOK": 10.8,
}

_fx_cache = {}
_fx_cache_time = {}

def get_live_fx_rate(currency: str) -> float:
    """Get live USD->currency exchange rate, cached for 5 minutes."""
    import time
    now = time.time()
    if currency == "USD":
        return 1.0
    # Cache for 300 seconds
    if currency in _fx_cache and now - _fx_cache_time.get(currency, 0) < 300:
        return _fx_cache[currency]
    ticker_sym = FX_TICKERS.get(currency)
    if not ticker_sym:
        return FALLBACK_RATES.get(currency, 1.0)
    try:
        t = yf.Ticker(ticker_sym)
        hist = t.history(period="1d")
        if not hist.empty:
            hist = hist[~hist.index.duplicated(keep="first")].sort_index()
            rate = float(hist["Close"].iloc[-1])
            # Yahoo quote convention: EUR/GBP/AUD tickers give USD per foreign
            # but INR=X / JPY=X give foreign per USD — normalise to foreign-per-USD
            if currency in ("EUR", "GBP", "AUD", "CAD", "CHF"):
                # EURUSD=X gives USD per EUR, so invert for EUR per USD
                if rate < 2:  # sanity: EUR/USD ~ 0.9
                    rate = 1.0 / rate if rate != 0 else FALLBACK_RATES.get(currency, 1.0)
            _fx_cache[currency] = rate
            _fx_cache_time[currency] = now
            return rate
    except Exception:
        pass
    return FALLBACK_RATES.get(currency, 1.0)

def get_live_inr_rate() -> float:
    return get_live_fx_rate("INR")

def get_all_fx_rates() -> dict:
    """Return a dict of currency -> units-per-USD for all supported currencies."""
    rates = {"USD": 1.0}
    for cur in FALLBACK_RATES:
        rates[cur] = get_live_fx_rate(cur)
    return rates

def get_clean_name_mapping(ticker: str) -> str:
    if ticker == "^IXIC":      return "NASDAQ"
    if ticker == "^NSEI":      return "NIFTY 50"
    if ticker == "^BSESN":     return "SENSEX"
    if ticker == "^DJI":       return "DJI"
    if ticker == "^GSPC":      return "S&P 500"
    if ticker == "BTC-USD":    return "Bitcoin"
    if ticker == "ETH-USD":    return "Ethereum"
    if ticker == "SOL-USD":    return "Solana"
    if ticker == "BNB-USD":    return "BNB"
    if ticker == "GC=F":       return "Gold"
    if ticker == "SI=F":       return "Silver"
    if ticker == "CL=F":       return "Crude Oil"
    if ticker == "NG=F":       return "Natural Gas"
    if ticker.endswith(".NS"): return ticker.replace(".NS", "")
    if ticker.endswith(".BO"): return ticker.replace(".BO", "")
    if ticker.endswith(".L"):  return ticker.replace(".L", "")
    if ticker.endswith(".DE"): return ticker.replace(".DE", "")
    if ticker.endswith(".T"):  return ticker.replace(".T", "")
    if ticker.endswith(".HK"): return ticker.replace(".HK", "")
    if ticker.endswith(".AX"): return ticker.replace(".AX", "")
    if ticker.endswith(".TO"): return ticker.replace(".TO", "")
    return ticker

def is_inr_asset(ticker: str) -> bool:
    return any(x in ticker for x in [".NS", ".BO", "^NSEI", "^BSESN"])

def is_yahoo_asset(ticker: str) -> bool:
    t = ticker.upper()
    return (t.endswith(".NS") or t.endswith(".BO") or t.endswith(".L") or
            t.endswith(".DE") or t.endswith(".T") or t.endswith(".HK") or
            t.endswith(".AX") or t.endswith(".TO") or t.endswith(".PA") or
            t.endswith(".MI") or t.endswith("-USD") or t.endswith("=F") or
            t.endswith("=X") or t.startswith("^"))

def get_asset_currency(ticker: str) -> str:
    t = ticker.upper()
    if is_inr_asset(ticker):      return "INR"
    if t.endswith(".L"):          return "GBP"
    if t.endswith(".DE") or t.endswith(".PA") or t.endswith(".MI"): return "EUR"
    if t.endswith(".T"):          return "JPY"
    if t.endswith(".HK"):         return "HKD"
    if t.endswith(".AX"):         return "AUD"
    if t.endswith(".TO"):         return "CAD"
    return "USD"

def get_currency_divisor(currency: str, inr_rate: float) -> float:
    """Returns units of foreign currency per 1 USD."""
    if currency == "USD":
        return 1.0
    if currency == "INR":
        return inr_rate
    return get_live_fx_rate(currency)

def interpret_asset_query(user_input: str) -> dict:
    system_instruction = """
You are a global financial data routing assistant. Fix typos and return standardised JSON.

Rules:
1. US stocks/ETFs (TSLA, AAPL, NVDA, MSFT, GOOGL, etc.): provider "finnhub", currency "USD".
2. Indian NSE stocks: provider "yahoo", currency "INR", append ".NS" (RELIANCE.NS, TCS.NS).
3. Bitcoin/BTC: ticker "BTC-USD", provider "yahoo", currency "USD".
4. Ethereum/ETH: ticker "ETH-USD", provider "yahoo", currency "USD".
5. Other crypto (Solana, BNB, etc.): ticker "SOL-USD"/"BNB-USD" etc, provider "yahoo", currency "USD".
6. Gold: ticker "GC=F", provider "yahoo", currency "USD".
7. Silver: ticker "SI=F", provider "yahoo", currency "USD".
8. Crude Oil/WTI: ticker "CL=F", provider "yahoo", currency "USD".
9. Natural Gas: ticker "NG=F", provider "yahoo", currency "USD".
10. Nifty 50: ticker "^NSEI", provider "yahoo", currency "INR".
11. Sensex: ticker "^BSESN", provider "yahoo", currency "INR".
12. Nasdaq: ticker "^IXIC", provider "yahoo", currency "USD".
13. Dow Jones: ticker "^DJI", provider "yahoo", currency "USD".
14. S&P 500: ticker "^GSPC", provider "yahoo", currency "USD".
15. UK/LSE stocks: append ".L" (HSBA.L), provider "yahoo", currency "GBP".
16. German stocks: append ".DE" (SAP.DE), provider "yahoo", currency "EUR".
17. Japanese stocks: append ".T" (7203.T), provider "yahoo", currency "JPY".
18. Hong Kong stocks: append ".HK", provider "yahoo", currency "HKD".
19. Australian stocks: append ".AX", provider "yahoo", currency "AUD".
20. Canadian stocks: append ".TO", provider "yahoo", currency "CAD".
21. French stocks: append ".PA", provider "yahoo", currency "EUR".

Respond ONLY with valid JSON:
{"ticker":"STRING","provider":"finnhub or yahoo","currency":"USD/INR/GBP/EUR/JPY/etc","cleanName":"STRING","description":"STRING","error":false}
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
        if is_yahoo_asset(cleaned):
            currency = get_asset_currency(cleaned)
            return {"ticker": cleaned, "provider": "yahoo", "currency": currency,
                    "cleanName": get_clean_name_mapping(cleaned), "description": "", "error": False}
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
            currency = get_asset_currency(ticker)
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
    return {
        "username":    user["username"],
        "displayName": user["displayName"],
        "cash":        user["cash"],
        "hideFromLeaderboard": user.get("hideFromLeaderboard", False)
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
        "history":     [],
        "hideFromLeaderboard": False
    }
    users_col.insert_one(user_doc)
    session.permanent = True
    session["user"] = username
    return jsonify(safe_user_view(user_doc))

@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = data.get("username", "").lower().strip()
    password = data.get("password", "")

    u = get_user(username)
    if not u or u["password"] != password:
        return jsonify({"error": "Invalid username or password"}), 401
    
    session.permanent = True
    session["user"] = username
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

@app.route("/api/auth/preferences", methods=["POST"])
def update_preferences():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    update = {}
    if "hideFromLeaderboard" in data:
        update["hideFromLeaderboard"] = bool(data["hideFromLeaderboard"])
    if update:
        users_col.update_one({"username": session["user"]}, {"$set": update})
    return jsonify({"success": True})

# ── FX Rates ──────────────────────────────────────────────────────────────────
@app.route("/api/fx/inr")
def fx_inr():
    return jsonify({"usdInrRate": get_live_inr_rate()})

@app.route("/api/fx/rates")
def fx_rates():
    """Return all supported FX rates."""
    rates = get_all_fx_rates()
    return jsonify(rates)

# ── Market Query ──────────────────────────────────────────────────────────────
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

    return jsonify({
        "symbol":                ticker,
        "cleanName":             routing.get("cleanName", get_clean_name_mapping(ticker)),
        "assetClassDescription": routing.get("description", ""),
        "price":                 quote["price"],
        "change":                quote["change"],
        "pct":                   quote["pct"],
        "high":                  quote["high"],
        "low":                   quote["low"],
        "currency":              quote["currency"],
        "provider":              provider,
        "usdInrRate":            inr_rate
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
        "User-Agent": "Mozilla/5.0 (compatible; StockenShares/1.0)",
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

# ── Tape prices ───────────────────────────────────────────────────────────────
@app.route("/api/tape")
def tape_data():
    tape_syms = [
        ("AAPL","finnhub"),("TSLA","finnhub"),("NVDA","finnhub"),
        ("MSFT","finnhub"),("AMZN","finnhub"),
        ("RELIANCE.NS","yahoo"),("TCS.NS","yahoo"),("INFY.NS","yahoo"),
        ("^NSEI","yahoo"),("^BSESN","yahoo"),
        ("BTC-USD","yahoo"),("ETH-USD","yahoo"),
        ("SOL-USD","yahoo"),("GC=F","yahoo"),
    ]
    results = {}
    def _fetch(sym, prov):
        q = fetch_live_quote(sym, prov)
        return sym, q
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch, s, p): s for s, p in tape_syms}
            for f in as_completed(futs, timeout=10):
                try:
                    sym, q = f.result()
                    if q:
                        results[sym] = {"price": q["price"], "pct": q["pct"], "currency": q["currency"]}
                except Exception:
                    pass
    except Exception:
        pass
    return jsonify(results)

# ── Portfolio ─────────────────────────────────────────────────────────────────
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

    positions    = []
    invested_usd = 0.0

    for sym, h in holdings.items():
        prov  = "yahoo" if is_yahoo_asset(sym) else "finnhub"
        quote = fetch_live_quote(sym, prov)

        shares         = h["shares"]
        avg_cost_local = h["cost"]

        if quote:
            curr_price_local = quote["price"]
            asset_currency   = quote["currency"]
        else:
            curr_price_local = avg_cost_local
            asset_currency   = get_asset_currency(sym)

        divisor = get_currency_divisor(asset_currency, inr_rate)

        avg_cost_usd     = avg_cost_local / divisor
        curr_price_usd   = curr_price_local / divisor
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

    # Update snapshot for leaderboard accuracy
    users_col.update_one(
        {"username": session["user"]},
        {"$set": {"snapshot": {"net": round(net_value_usd, 2), "ret": round(total_returns, 4)}}}
    )

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

    asset_currency = get_asset_currency(symbol)
    divisor = get_currency_divisor(asset_currency, inr_rate)

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
        "sumUsd":      cost_usd,
        "currency":    asset_currency
    }

    # Recalculate full valuation snapshots
    snap_invested = 0.0
    for s, h in holdings.items():
        sc = get_asset_currency(s)
        sd = get_currency_divisor(sc, inr_rate)
        snap_invested += h["shares"] * h["cost"] / sd

    snap_net = round(cash + snap_invested, 2)
    snap_ret = round(((snap_net - STARTING_CASH) / STARTING_CASH) * 100.0, 4)

    users_col.update_one(
        {"username": session["user"]},
        {
            "$set": {
                "cash": round(cash, 6),
                "holdings": holdings,
                "snapshot": {"net": snap_net, "ret": snap_ret}
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
    inr_rate = get_live_inr_rate()

    for u in all_users:
        # Skip users who opted out
        if u.get("hideFromLeaderboard", False):
            continue

        # ALWAYS compute fresh net value from holdings + cash
        holdings = u.get("holdings", {})
        cash = u.get("cash", 0)
        invested_usd = 0.0

        for sym, h in holdings.items():
            prov  = "yahoo" if is_yahoo_asset(sym) else "finnhub"
            quote = fetch_live_quote(sym, prov)
            sc    = get_asset_currency(sym)
            sd    = get_currency_divisor(sc, inr_rate)
            if quote:
                invested_usd += h["shares"] * quote["price"] / sd
            else:
                invested_usd += h["shares"] * h["cost"] / sd

        net   = cash + invested_usd
        ret   = ((net - STARTING_CASH) / STARTING_CASH) * 100.0

        # Update snapshot while we're at it
        users_col.update_one(
            {"username": u["username"]},
            {"$set": {"snapshot": {"net": round(net, 2), "ret": round(ret, 4)}}}
        )

        board.append({
            "name":     u["displayName"],
            "handle":   u["username"],
            "cash":     cash,
            "netValue": round(net, 2),
            "returns":  round(ret, 4)
        })

    board.sort(key=lambda x: x["netValue"], reverse=True)
    return jsonify(board)

if __name__ == "__main__":
    app.run(debug=True)
