import os
import random
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pymongo import MongoClient
import yfinance as ticker_engine
from groq import Groq

app = Flask(__name__, static_folder=".")
CORS(app)

# ==========================================
# 1. LIVE CONNECTIONS & SECURE ENVIROMENTS
# ==========================================
MONGO_URI = os.environ.get("MONGO_URI")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 

if not MONGO_URI:
    print("❌ SYSTEM ALERT: MONGO_URI environment variable is missing on Render!")
if not GROQ_API_KEY:
    print("❌ SYSTEM ALERT: GROQ_API_KEY is missing! AI parsing will fall back to basic matching.")

try:
    client = MongoClient(MONGO_URI)
    db = client["trading_simulator"]
    users_col = db["users"]
    history_col = db["history"]
    print("🚀 MongoDB Atlas Connectivity Established!")
except Exception as e:
    print(f"❌ Database connection failed: {e}")

# Initialize Groq client
ai_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ==========================================
# 2. AI TEXT PARSING LOGIC
# ==========================================
def extract_ticker_with_ai(user_query):
    """Uses Groq AI to convert plain English requests into a clean stock ticker symbol."""
    if not ai_client:
        clean = user_query.strip().upper()
        fallback_map = {"APPLE": "AAPL", "NVIDIA": "NVDA", "TESLA": "TSLA", "BITCOIN": "BTC-USD", "MICROSOFT": "MSFT"}
        return fallback_map.get(clean, clean)

    try:
        chat_completion = ai_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise financial asset extraction bot. Your job is to read a user's natural language query "
                        "and return ONLY the official stock ticker symbol or crypto symbol (e.g., AAPL, NVDA, TSLA, BTC-USD). "
                        "Do not include punctuation, do not include introductory words, do not explain anything. Just output the uppercase symbol."
                    )
                },
                {
                    "role": "user",
                    "content": f"Extract the stock ticker from this search query: '{user_query}'"
                }
            ],
            model="llama3-8b-8192", 
            temperature=0.0, 
            max_tokens=10
        )
        symbol = chat_completion.choices[0].message.content.strip().upper()
        symbol = symbol.replace('"', '').replace("'", "").replace(".", "")
        return symbol
    except Exception as e:
        print(f"🤖 AI Parsing error, using raw query: {e}")
        return user_query.strip().upper()

# ==========================================
# 3. APPLICATION API ROUTE MANAGEMENT
# ==========================================

# 🛠️ FIXED: Now correctly hunting down your index.html file!
@app.route("/")
def serve_dashboard():
    return send_from_directory(".", "index.html")

# --- AUTHENTICATION: REGISTER ---
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    display_name = data.get("displayName", "").strip()
    password = data.get("password", "")

    if not username or not display_name or not password:
        return jsonify({"error": "Please enter a username, display name, and password."}), 400

    if users_col.find_one({"username": username}):
        return jsonify({"error": "Username is already taken."}), 400

    new_user = {
        "username": username,
        "displayName": display_name,
        "password": password, 
        "cash": 10000.00,  
        "portfolio": {}    
    }
    users_col.insert_one(new_user)

    return jsonify({
        "username": username,
        "displayName": display_name,
        "cash": 10000.00
    }), 201

# --- AUTHENTICATION: LOGIN ---
@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")

    user = users_col.find_one({"username": username})
    if not user or user["password"] != password:
        return jsonify({"error": "Invalid username or password."}), 401

    global CURRENT_ACTIVE_USER
    CURRENT_ACTIVE_USER = username

    return jsonify({
        "username": user["username"],
        "displayName": user["displayName"],
        "cash": user.get("cash", 10000.00)
    }), 200

# 🛠️ ADDED: Missing logout pipeline path now added
@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    global CURRENT_ACTIVE_USER
    CURRENT_ACTIVE_USER = ""
    return jsonify({"message": "Logged out successfully!"}), 200

# --- NATURAL LANGUAGE / ASSET LOOKUP ENGINE ---
@app.route("/api/market/query", methods=["GET"])
def query_market():
    raw_query = request.args.get("query", "").strip()
    if not raw_query:
        return jsonify({"error": "Please provide an asset identifier."}), 400

    symbol = extract_ticker_with_ai(raw_query)
    print(f"🔮 AI transformed user query '{raw_query}' into ticker target: '{symbol}'")

    try:
        ticker = ticker_engine.Ticker(symbol)
        info = ticker.fast_info
        
        price = info.get('lastPrice')
        if price is None:
            return jsonify({"error": f"Could not find stock records matching symbol '{symbol}'."}), 404

        high = info.get('dayHigh', price)
        low = info.get('dayLow', price)
        prev_close = info.get('previousClose', price)
        
        change = price - prev_close
        pct = (change / prev_close) * 100 if prev_close else 0

        return jsonify({
            "symbol": symbol,
            "cleanName": symbol,
            "assetClassDescription": "AI-Parsed Global Market Node",
            "price": float(price),
            "change": float(change),
            "pct": float(pct),
            "high": float(high),
            "low": float(low)
        }), 200
    except Exception as e:
        return jsonify({"error": "Failed gathering market telemetry information."}), 500

# --- PORTFOLIO & SUMMARY ENGINE ---
@app.route("/api/user/portfolio", methods=["GET"])
def get_user_portfolio():
    username = globals().get("CURRENT_ACTIVE_USER", "")
    user = users_col.find_one({"username": username})
    
    if not user:
        return jsonify({"error": "Session unauthorized or profile missing."}), 401

    cash = float(user.get("cash", 10000.00))
    user_portfolio_data = user.get("portfolio", {})
    
    positions_array = []
    total_invested_value = 0.0

    for symbol, asset_info in user_portfolio_data.items():
        shares = float(asset_info.get("shares", 0))
        if shares <= 0:
            continue
            
        total_cost = float(asset_info.get("total_cost", 0))
        avg_cost = total_cost / shares if shares > 0 else 0
        
        current_price = avg_cost 
        try:
            tk = ticker_engine.Ticker(symbol)
            current_price = float(tk.fast_info.get('lastPrice', avg_cost))
        except:
            pass

        market_value = shares * current_price
        total_invested_value += market_value
        gain_loss = market_value - total_cost

        positions_array.append({
            "symbol": symbol,
            "rawToken": symbol,
            "shares": shares,
            "avgCost": avg_cost,
            "currentPrice": current_price,
            "marketValue": market_value,
            "gainLoss": gain_loss
        })

    net_value = cash + total_invested_value
    returns_pct = ((net_value - 10000.0) / 10000.0) * 100.0

    user_history_cursor = history_col.find({"username": username}).sort("timestamp", -1)
    history_logs = []
    for h in user_history_cursor:
        history_logs.append({
            "date": h.get("date"),
            "type": h.get("type"),
            "cleanSymbol": h.get("symbol"),
            "shares": float(h.get("qty", 0)),
            "price": float(h.get("price", 0)),
            "sum": float(h.get("sum", 0)),
            "pl": float(h.get("pl", 0))
        })

    return jsonify({
        "cash": cash,
        "netValue": net_value,
        "invested": total_invested_value,
        "returns": returns_pct,
        "positions": positions_array,
        "history": history_logs
    }), 200

# --- TRADE ORDER EXECUTION ---
@app.route("/api/trade/execute", methods=["POST"])
def execute_trade():
    username = globals().get("CURRENT_ACTIVE_USER", "")
    user = users_col.find_one({"username": username})
    if not user:
        return jsonify({"error": "Unauthorized session profile."}), 401

    data = request.json or {}
    symbol = data.get("symbol", "").upper()
    qty = float(data.get("qty", 0))
    mode = data.get("mode", "buy").lower()
    execution_price = float(data.get("price", 0))

    if qty <= 0 or execution_price <= 0:
        return jsonify({"error": "Invalid order volume dimensions."}), 400

    current_cash = float(user.get("cash", 10000.00))
    portfolio = user.get("portfolio", {})
    total_cost = qty * execution_price
    realized_pl = 0.0

    if mode == "buy":
        if current_cash < total_cost:
            return jsonify({"error": "Insufficient portfolio funds available."}), 400
        
        current_cash -= total_cost
        if symbol not in portfolio:
            portfolio[symbol] = {"shares": 0.0, "total_cost": 0.0}
        
        portfolio[symbol]["shares"] += qty
        portfolio[symbol]["total_cost"] += total_cost
    elif mode == "sell":
        if symbol not in portfolio or portfolio[symbol]["shares"] < qty:
            return jsonify({"error": "Insufficient shares available to sell."}), 400
        
        current_owned_shares = portfolio[symbol]["shares"]
        current_total_cost = portfolio[symbol]["total_cost"]
        avg_cost_paid = current_total_cost / current_owned_shares
        
        realized_pl = (execution_price - avg_cost_paid) * qty
        current_cash += total_cost
        
        portfolio[symbol]["shares"] -= qty
        portfolio[symbol]["total_cost"] -= (avg_cost_paid * qty)
        
        if portfolio[symbol]["shares"] <= 0:
            del portfolio[symbol]

    users_col.update_one(
        {"username": username},
        {"$set": {"cash": current_cash, "portfolio": portfolio}}
    )

    history_col.insert_one({
        "username": username,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": mode.upper(),
        "symbol": symbol,
        "qty": qty,
        "price": execution_price,
        "sum": total_cost,
        "pl": realized_pl,
        "timestamp": datetime.now()
    })

    return jsonify({"message": "Order processed successfully!"}), 200

# --- LEADERBOARD ---
@app.route("/api/leaderboard", methods=["GET"])
def get_leaderboard():
    all_users = users_col.find()
    leaderboard_data = []

    for user in all_users:
        cash = float(user.get("cash", 10000.00))
        portfolio = user.get("portfolio", {})
        
        current_holdings_value = 0.0
        for symbol, asset in portfolio.items():
            try:
                tk = ticker_engine.Ticker(symbol)
                current_holdings_value += float(asset.get("shares", 0)) * float(tk.fast_info.get('lastPrice', 0))
            except:
                pass
        
        net_worth = cash + current_holdings_value
        returns_pct = ((net_worth - 10000.0) / 10000.0) * 100.0
        
        leaderboard_data.append({
            "name": user.get("displayName", "Anonymous Player"),
            "handle": user.get("username", "player"),
            "cash": cash,
            "returns": returns_pct
        })

    leaderboard_data.sort(key=lambda x: x["returns"], reverse=True)
    return jsonify(leaderboard_data[:10]), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
