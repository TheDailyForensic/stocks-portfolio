// ==========================================
// 1. CONFIGURATION & DEPENDENCIES
// ==========================================
const express = require('express');
const mongoose = require('mongoose');
const cors = require('cors');
const path = require('path');

const app = express();
app.use(express.json());
app.use(cors());

// Serve your HTML file automatically when someone visits your website URL
app.use(express.static(__dirname));

// ==========================================
// 2. PERMANENT DATABASE CONNECTION
// ==========================================
const mongoURI = process.env.MONGO_URI;
if (!mongoURI) {
  console.error("❌ ERROR: MONGO_URI environment variable is missing on Render!");
  process.exit(1);
}

mongoose.connect(mongoURI)
  .then(() => console.log("🚀 Success: Connected to MongoDB Atlas Cloud Vault!"))
  .catch(err => console.error("❌ Database connection error:", err));

// ==========================================
// 3. DATABASE SCHEMAS & MODELS
// ==========================================
const userSchema = new mongoose.Schema({
  username: { type: String, required: true, unique: true },
  display_name: { type: String, required: true },
  password: { type: String, required: true }, 
  balance: { type: Number, default: 100000 },  
  portfolio: {
    type: Map,
    of: Number,
    default: {} // Stores format: { "AAPL": 10, "TSLA": 5 }
  }
});

const User = mongoose.model('User', userSchema);

// ==========================================
// 4. LIVE SIMULATED MARKET ENGINE (Fluctuates every 4s)
// ==========================================
let stockMarket = {
  AAPL: 175.00,
  TSLA: 180.00,
  NVDA: 850.00,
  BTC: 65000.00
};

setInterval(() => {
  for (let symbol in stockMarket) {
    const changePercent = (Math.random() * 0.03) - 0.015; // -1.5% to +1.5%
    stockMarket[symbol] = parseFloat((stockMarket[symbol] * (1 + changePercent)).toFixed(2));
  }
}, 4000);

// ==========================================
// 5. API ENDPOINTS
// ==========================================

// Send live changing stock prices to your HTML
app.get('/api/stocks', (req, res) => {
  res.json(stockMarket);
});

// Authentication: Register Profile
app.post('/api/auth/register', async (req, res) => {
  try {
    const { username, displayName, password } = req.body;
    
    if (!username || !displayName || !password) {
      return res.status(400).json({ error: "Please enter a username, display name, and password." });
    }

    const existingUser = await User.findOne({ username: username.toLowerCase() });
    if (existingUser) {
      return res.status(400).json({ error: "Username is already taken." });
    }

    const newUser = new User({ 
      username: username.toLowerCase(), 
      display_name: displayName, 
      password, 
      portfolio: {} 
    });
    await newUser.save();

    res.status(201).json({ message: "Success!", user: { username: newUser.display_name, rawUser: newUser.username, balance: newUser.balance, portfolio: Object.fromEntries(newUser.portfolio) } });
  } catch (error) {
    res.status(500).json({ error: "Internal server error during registration." });
  }
});

// Authentication: Login
app.post('/api/auth/login', async (req, res) => {
  try {
    const { username, password } = req.body;
    const user = await User.findOne({ username: username.toLowerCase() });

    if (!user || user.password !== password) {
      return res.status(401).json({ error: "Invalid username or password." });
    }

    res.json({ message: "Success!", user: { username: user.display_name, rawUser: user.username, balance: user.balance, portfolio: Object.fromEntries(user.portfolio) } });
  } catch (error) {
    res.status(500).json({ error: "Internal server error during login." });
  }
});

// Trading: Buy Asset
app.post('/api/trade/buy', async (req, res) => {
  try {
    const { username, symbol, quantity } = req.body;
    const qty = parseInt(quantity);
    
    if (!stockMarket[symbol] || qty <= 0) return res.status(400).json({ error: "Invalid stock or quantity." });

    const user = await User.findOne({ username });
    if (!user) return res.status(404).json({ error: "User profile missing." });

    const totalCost = stockMarket[symbol] * qty;
    if (user.balance < totalCost) return res.status(400).json({ error: "Insufficient trading funds." });

    user.balance = parseFloat((user.balance - totalCost).toFixed(2));
    const currentOwned = user.portfolio.get(symbol) || 0;
    user.portfolio.set(symbol, currentOwned + qty);

    await user.save();
    res.json({ message: `Bought ${qty} shares of ${symbol}`, user: { balance: user.balance, portfolio: Object.fromEntries(user.portfolio) } });
  } catch (error) {
    res.status(500).json({ error: "Trade execution failed." });
  }
});

// Trading: Sell Asset
app.post('/api/trade/sell', async (req, res) => {
  try {
    const { username, symbol, quantity } = req.body;
    const qty = parseInt(quantity);

    if (!stockMarket[symbol] || qty <= 0) return res.status(400).json({ error: "Invalid stock or quantity." });

    const user = await User.findOne({ username });
    if (!user) return res.status(404).json({ error: "User profile missing." });

    const currentOwned = user.portfolio.get(symbol) || 0;
    if (currentOwned < qty) return res.status(400).json({ error: "You do not own enough shares." });

    const revenue = stockMarket[symbol] * qty;
    user.balance = parseFloat((user.balance + revenue).toFixed(2));
    
    if (currentOwned - qty === 0) {
      user.portfolio.delete(symbol);
    } else {
      user.portfolio.set(symbol, currentOwned - qty);
    }

    await user.save();
    res.json({ message: `Sold ${qty} shares of ${symbol}`, user: { balance: user.balance, portfolio: Object.fromEntries(user.portfolio) } });
  } catch (error) {
    res.status(500).json({ error: "Trade execution failed." });
  }
});

// Start listening
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`📡 Server actively listening on port ${PORT}`));
