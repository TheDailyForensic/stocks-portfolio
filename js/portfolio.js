/**
 * STOCKSIM -- CORE MATH ENGINE
 * Evaluates trade math metrics, asset weight margins, and P/L allocations
 */

const Portfolio = {
  async getCalculatedState(user) {
    let investedValue = 0;
    const items = [];

    for (const symbol in user.holdings) {
      const qty = user.holdings[symbol].shares;
      if (qty <= 0) continue;

      const quote = await StockAPI.fetchQuote(symbol);
      const marketVal = qty * quote.price;
      const totalCost = qty * user.holdings[symbol].avgCost;
      const gainLoss = marketVal - totalCost;
      const returnsPct = totalCost > 0 ? (gainLoss / totalCost) * 100 : 0;

      investedValue += marketVal;
      items.push({
        symbol,
        name: quote.name,
        shares: qty,
        avgCost: user.holdings[symbol].avgCost,
        currentPrice: quote.price,
        marketValue: marketVal,
        gainLoss,
        returnsPct
      });
    }

    const totalPortfolioValue = user.cash + investedValue;
    const initialDeposit = 10000.00;
    const absoluteReturn = totalPortfolioValue - initialDeposit;
    const percentageReturn = (absoluteReturn / initialDeposit) * 100;

    return {
      cash: user.cash,
      investedValue,
      totalPortfolioValue,
      absoluteReturn,
      percentageReturn,
      holdingsList: items
    };
  },

  executeTransaction(user, symbol, type, qty, currentPrice) {
    const sym = symbol.toUpperCase().trim();
    const costBasis = qty * currentPrice;

    if (type === "buy") {
      if (user.cash < costBasis) throw new Error("Liquidation limits crossed: Insufficient Cash Balance.");
      
      user.cash -= costBasis;
      if (!user.holdings[sym]) user.holdings[sym] = { shares: 0, avgCost: 0 };

      const currentQty = user.holdings[sym].shares;
      const currentCost = user.holdings[sym].avgCost;

      user.holdings[sym].shares += qty;
      user.holdings[sym].avgCost = ((currentCost * currentQty) + costBasis) / user.holdings[sym].shares;

      user.history.unshift({
        date: new Date().toLocaleString(),
        type: "BUY",
        symbol: sym,
        shares: qty,
        price: currentPrice,
        total: costBasis,
        realizedPL: 0
      });
    } else if (type === "sell") {
      if (!user.holdings[sym] || user.holdings[sym].shares < qty) {
        throw new Error("Asset structural parameters rejected: Insufficient volume positions.");
      }

      const costBasisSold = qty * user.holdings[sym].avgCost;
      const revenueBasis = qty * currentPrice;
      const realizedPL = revenueBasis - costBasisSold;

      user.cash += revenueBasis;
      user.holdings[sym].shares -= qty;

      user.history.unshift({
        date: new Date().toLocaleString(),
        type: "SELL",
        symbol: sym,
        shares: qty,
        price: currentPrice,
        total: revenueBasis,
        realizedPL: realizedPL
      });

      if (user.holdings[sym].shares === 0) delete user.holdings[sym];
    }

    // Append history data loops for charts tracking
    let currentInvested = 0;
    Object.keys(user.holdings).forEach(s => {
      currentInvested += (user.holdings[s].shares * currentPrice);
    });

    user.equityHistory.push(user.cash + currentInvested);
    user.datesHistory.push(new Date().toLocaleDateString());

    Auth.saveUserData(user);
  }
};