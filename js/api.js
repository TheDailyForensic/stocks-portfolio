/**
 * STOCKSIM -- MARKET API PROTOCOL
 * Fetches equity coordinates with instant sandbox fallbacks
 */

const StockAPI = {
  mockDatabase: {
    "AAPL": { name: "Apple Inc.", price: 175.42, change: 2.34, changesPercentage: 1.35, open: 173.00, high: 176.00, low: 172.50, previousClose: 173.08, volume: 52000000, marketCap: 2750000000000, pe: 28.1, eps: 6.24, div: 0.96, beta: 1.28, about: "Apple Inc. designs, manufactures, and markets smartphones, personal computers, tablets, wearables, and accessories worldwide." },
    "TSLA": { name: "Tesla Inc.", price: 180.15, change: -5.20, changesPercentage: -2.81, open: 185.00, high: 186.50, low: 179.00, previousClose: 185.35, volume: 85000000, marketCap: 570000000000, pe: 42.3, eps: 4.25, div: 0.00, beta: 2.42, about: "Tesla, Inc. designs, develops, manufactures, leases, and sells electric vehicles, and energy generation and storage systems." },
    "NVDA": { name: "NVIDIA Corp.", price: 875.12, change: 24.50, changesPercentage: 2.88, open: 850.00, high: 880.00, low: 848.00, previousClose: 850.62, volume: 41000000, marketCap: 2180000000000, pe: 75.4, eps: 11.60, div: 0.16, beta: 1.75, about: "NVIDIA Corporation focuses on personal computer graphics, graphics processing units, and also artificial intelligence solutions." },
    "MSFT": { name: "Microsoft Corp.", price: 415.60, change: 0.85, changesPercentage: 0.21, open: 414.00, high: 417.50, low: 413.20, previousClose: 414.75, volume: 23000000, marketCap: 3090000000000, pe: 36.2, eps: 11.48, div: 3.00, beta: 0.90, about: "Microsoft Corporation develops, licenses, and supports software, services, devices, and solutions worldwide." },
    "AMZN": { name: "Amazon.com Inc.", price: 178.15, change: -1.10, changesPercentage: -0.61, open: 179.20, high: 180.10, low: 176.50, previousClose: 179.25, volume: 33000000, marketCap: 1850000000000, pe: 61.8, eps: 2.88, div: 0.00, beta: 1.15, about: "Amazon.com, Inc. engages in the retail sale of consumer products and subscriptions in North America and internationally." },
    "GOOGL":{ name: "Alphabet Inc.", price: 153.60, change: 1.95, changesPercentage: 1.29, open: 151.50, high: 154.20, low: 151.00, previousClose: 151.65, volume: 28000000, marketCap: 1910000000000, pe: 26.3, eps: 5.84, div: 0.80, beta: 1.05, about: "Alphabet Inc. offers Google Search, YouTube, and Google Cloud platforms across global markets." }
  },

  async fetchQuote(symbol) {
    const sym = symbol.toUpperCase().trim();
    try {
      const res = await fetch(`https://financialmodelingprep.com/api/v3/quote-short/${sym}?apikey=demo`);
      const data = await res.json();
      if (data && data.length > 0) {
        const base = this.mockDatabase[sym] || { name: sym + " Corp", open: data[0].price, high: data[0].price, low: data[0].price, previousClose: data[0].price, volume: 1000000, marketCap: 500000000, pe: 15, eps: 1, div: 0, beta: 1, about: "Publicly traded equity instrument asset." };
        return {
          symbol: sym,
          name: base.name,
          price: data[0].price,
          change: data[0].price - base.previousClose,
          changesPercentage: ((data[0].price - base.previousClose) / base.previousClose) * 100,
          ...base
        };
      }
    } catch (e) {
      // Gracefully defaults to mock layer on network drops or rate caps
    }
    return this.getMockData(sym);
  },

  getMockData(symbol) {
    const sym = symbol.toUpperCase().trim();
    if (this.mockDatabase[sym]) {
      return { symbol: sym, ...this.mockDatabase[sym] };
    }
    return {
      symbol: sym,
      name: sym + " Global Sandbox Asset",
      price: 100.00,
      change: 0.00,
      changesPercentage: 0.00,
      open: 100.00, high: 100.00, low: 100.00, previousClose: 100.00,
      volume: 500000, marketCap: 120000000, pe: 15.0, eps: 2.00, div: 0.00, beta: 1.00,
      about: "Custom paper trading tracking node assigned under sandbox platform terms."
    };
  },

  async search(query) {
    const q = query.toUpperCase().trim();
    if (!q) return [];
    return Object.keys(this.mockDatabase)
      .filter(key => key.includes(q) || this.mockDatabase[key].name.toUpperCase().includes(q))
      .map(key => ({ symbol: key, name: this.mockDatabase[key].name, price: this.mockDatabase[key].price }));
  }
};