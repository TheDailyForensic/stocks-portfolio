import os
import json
import requests
import yfinance as yf
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI

app = Flask(__name__)
app.secret_key = "DEVELOPMENT_SECRET_KEY_KEEP_THIS_SAFE"

# =====================================================================
# 1. READ SECURE TOKENS FROM ENVIRONMENT (Set these in Render's Dashboard)
# =====================================================================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

# Simple Mock Database (In-Memory). Resets when the free server sleeps.
MOCK_USERS_DB = {}

# =====================================================================
# 2. INTENT PARSING ENGINE (Groq AI)
# =====================================================================
def interpret_asset_query(user_input: str) -> dict:
    system_instruction = """
    You are a financial data routing assistant. Your job is to take user inputs, 
    fix typos, figure out what financial asset they want, and return a standardized JSON object.
    
    Rules for routing:
    1. For US stocks/ETFs, set provider to 'finnhub' and ticker to standard uppercase (e.g., AAPL, TSLA, NVDA).
    2. For Indian stocks on the NSE, set provider to 'yahoo' and append '.NS' (e.g., RELIANCE.NS, SBIN.NS).
    3. For the Nasdaq Index, use '^IXIC' and provider 'yahoo'.
    4. For the Nifty 50 Index, use '^NSEI' and provider 'yahoo'.
    5. If you cannot identify a valid trading asset, set error to true.

    Respond ONLY with a valid JSON object matching this schema:
    {"ticker": "STRING", "provider": "finnhub" or "yahoo", "error": false}
    """
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Query: '{user_input}'"}
            ]
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {"ticker": "", "provider": "", "error": True}

# =====================================================================
# 3. SECURE DATA FETCHERS
# =====================================================================
def fetch_live_quote(ticker: str, provider: str):
    if provider == "finnhub":
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
        try:
            r = requests.get(url).json()
            if r.get('c', 0) == 0: return None
            return {
                "symbol": ticker, "price": r['c'], "change": r.get('d', 0),
                "pct": r.get('dp', 0), "low": r.get('l', r['c']),
                "high": r.get('h', r['c']), "prev": r.get('pc', r['c']), "currency": "USD"
            }
        except: return None
    else:
        try:
            t = yf.Ticker(ticker)
            price = t.fast_info['last_price']
            currency = "INR" if (".NS" in ticker or "NSEI" in ticker) else "USD/Points"
            return {
                "symbol": ticker, "price": price, "change": 0, "pct": 0,
                "low": price, "high": price, "prev": price, "currency": currency
            }
        except: return None

# =====================================================================
# 4. WEB ROUTING CONTROL CONTROLLERS
# =====================================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').lower().strip()
    display_name = data.get('displayName', '').strip()
    
    if not username or not display_name:
        return jsonify({"error": "All parameters must be provisioned."}), 400
    if username in MOCK_USERS_DB:
        return jsonify({"error": "Handle allocation unavailable."}), 400

    MOCK_USERS_DB[username] = {
        "username": username, "displayName": display_name,
        "cash": 10000.00, "holdings": {}, "history": []
    }
    session['user'] = username
    return jsonify(MOCK_USERS_DB[username])

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').lower().strip()
    
    if username in MOCK_USERS_DB:
        session['user'] = username
        return jsonify(MOCK_USERS_DB[username])
    return jsonify({"error": "Security signature tracking failed."}), 401

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('user', None)
    return jsonify({"success": True})

@app.route('/api/market/query', methods=['GET'])
def query_market():
    query_text = request.args.get('query', '')
    if not query_text:
        return jsonify({"error": "Empty search matrix."}), 400
        
    ai_decision = interpret_asset_query(query_text)
    if ai_decision.get('error') or not ai_decision.get('ticker'):
        return jsonify({"error": "AI could not verify asset class or symbol."}), 404

    quote = fetch_live_quote(ai_decision['ticker'], ai_decision['provider'])
    if not quote:
        return jsonify({"error": "Market clearing connection offline."}), 404
        
    return jsonify(quote)

@app.route('/api/user/portfolio', methods=['GET'])
def get_portfolio():
    if 'user' not in session or session['user'] not in MOCK_USERS_DB:
        return jsonify({"error": "Unauthorized session context."}), 401
    
    user = MOCK_USERS_DB[session['user']]
    invested = 0
    positions_list = []
    
    for sym, holding in list(user['holdings'].items()):
        ai_meta = interpret_asset_query(sym)
        quote = fetch_live_quote(sym, ai_meta.get('provider', 'finnhub'))
        current_price = quote['price'] if quote else holding['cost']
        
        mkt_val = holding['shares'] * current_price
        cost_basis = holding['shares'] * holding['cost']
        gl = mkt_val - cost_basis
        invested += mkt_val
        
        positions_list.append({
            "symbol": sym, "shares": holding['shares'], "avgCost": holding['cost'],
            "currentPrice": current_price, "marketValue": mkt_val, "gainLoss": gl
        })
        
    net_val = user['cash'] + invested
    yield_pct = ((net_val - 10000.00) / 10000.00) * 100
    
    return jsonify({
        "cash": user['cash'], "invested": invested, "netValue": net_val,
        "returns": yield_pct, "positions": positions_list, "history": user['history']
    })

@app.route('/api/trade/execute', methods=['POST'])
def execute_trade():
    if 'user' not in session or session['user'] not in MOCK_USERS_DB:
        return jsonify({"error": "Unauthorized session context"}), 401
        
    data = request.json
    symbol = data.get('symbol', '').upper()
    qty = int(data.get('qty', 0))
    mode = data.get('mode', 'buy')
    price = float(data.get('price', 0))
    
    if qty <= 0 or price <= 0:
        return jsonify({"error": "Invalid metrics matrix"}), 400
        
    user = MOCK_USERS_DB[session['user']]
    total_cost = qty * price
    
    if mode == 'buy':
        if user['cash'] < total_cost:
            return jsonify({"error": "Insufficient funds in liquidation profile."}), 400
        user['cash'] -= total_cost
        if symbol not in user['holdings']:
            user['holdings'][symbol] = {"shares": 0, "cost": 0.0}
            
        ex_qty = user['holdings'][symbol]['shares']
        ex_cost = user['holdings'][symbol]['cost']
        user['holdings'][symbol]['shares'] += qty
        user['holdings'][symbol]['cost'] = ((ex_qty * ex_cost) + total_cost) / user['holdings'][symbol]['shares']
        
        user['history'].insert(0, {"date": "Just Now", "type": "BUY", "symbol": symbol, "shares": qty, "price": price, "sum": total_cost, "pl": 0})
    else:
        if symbol not in user['holdings'] or user['holdings'][symbol]['shares'] < qty:
            return jsonify({"error": "Allocation limits exceeded."}), 400
            
        cost_sold = qty * user['holdings'][symbol]['cost']
        net_pl = total_cost - cost_sold
        user['cash'] += total_cost
        user['holdings'][symbol]['shares'] -= qty
        
        user['history'].insert(0, {"date": "Just Now", "type": "SELL", "symbol": symbol, "shares": qty, "price": price, "sum": total_cost, "pl": net_pl})
        if user['holdings'][symbol]['shares'] == 0:
            del user['holdings'][symbol]
            
    return jsonify({"success": True})

@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    board = []
    for k, v in MOCK_USERS_DB.items():
        board.append({"name": v['displayName'], "handle": k, "cash": v['cash'], "returns": 0.0})
    return jsonify(board)

if __name__ == '__main__':
    app.run(debug=True)