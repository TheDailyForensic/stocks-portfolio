/**
 * STOCKSIM -- TRANSACTIONAL INTERFACE CONTROLLER
 * Evaluates execution forms, matching quantity limits with pricing values
 */

document.addEventListener("DOMContentLoaded", async () => {
  const user = Auth.requireAuth();
  if (!user) return;

  let activeStock = null;
  let mode = "buy";

  const searchInput = document.getElementById("stock-search");
  const searchResults = document.getElementById("search-results");
  const popularGrid = document.getElementById("popular-grid");
  const stockPanel = document.getElementById("stock-panel");
  const tradeCash = document.getElementById("trade-cash-display");
  const orderShares = document.getElementById("order-shares");

  function refreshCashView() {
    if (tradeCash) tradeCash.textContent = "$" + user.cash.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  refreshCashView();

  // Populate quick-select stocks grid
  if (popularGrid) {
    const tokens = ["AAPL", "TSLA", "NVDA", "MSFT"];
    popularGrid.innerHTML = "";
    tokens.forEach(t => {
      const item = StockAPI.getMockData(t);
      const card = document.createElement("div");
      card.className = "confirm-item";
      card.style.cursor = "pointer";
      card.innerHTML = `<div class="confirm-label" style="color:var(--accent);">${t}</div><div class="confirm-value">$${item.price}</div>`;
      card.addEventListener("click", () => focusTicker(t));
      popularGrid.appendChild(card);
    });
  }

  // Handle live autocomplete search filtering
  if (searchInput && searchResults) {
    searchInput.addEventListener("input", async () => {
      const q = searchInput.value;
      if (q.length < 1) {
        searchResults.classList.add("hidden");
        return;
      }
      const matches = await StockAPI.search(q);
      if (matches.length === 0) {
        searchResults.classList.add("hidden");
        return;
      }
      searchResults.innerHTML = "";
      searchResults.classList.remove("hidden");
      matches.forEach(m => {
        const div = document.createElement("div");
        div.style.padding = "0.75rem";
        div.style.cursor = "pointer";
        div.style.borderBottom = "1px solid var(--border)";
        div.innerHTML = `<strong>${m.symbol}</strong> - ${m.name} ($${m.price})`;
        div.addEventListener("click", () => {
          focusTicker(m.symbol);
          searchResults.classList.add("hidden");
          searchInput.value = "";
        });
        searchResults.appendChild(div);
      });
    });
  }

  async function focusTicker(symbol) {
    activeStock = await StockAPI.fetchQuote(symbol);
    if (!stockPanel) return;
    stockPanel.classList.remove("hidden");

    document.getElementById("detail-symbol").textContent = activeStock.symbol;
    document.getElementById("detail-name").textContent = activeStock.name;
    document.getElementById("detail-price").textContent = "$" + activeStock.price.toFixed(2);

    const delta = document.getElementById("detail-change");
    delta.textContent = `${activeStock.change >= 0 ? "+" : ""}${activeStock.change.toFixed(2)} (${activeStock.changesPercentage.toFixed(2)}%)`;
    delta.className = activeStock.change >= 0 ? "stock-change-big gain" : "stock-change-big loss";

    document.getElementById("st-open").textContent = "$" + activeStock.open.toFixed(2);
    document.getElementById("st-high").textContent = "$" + activeStock.high.toFixed(2);
    document.getElementById("st-low").textContent = "$" + activeStock.low.toFixed(2);
    document.getElementById("st-prev").textContent = "$" + activeStock.previousClose.toFixed(2);
    document.getElementById("st-vol").textContent = activeStock.volume.toLocaleString();
    document.getElementById("st-mktcap").textContent = "$" + (activeStock.marketCap / 1e9).toFixed(2) + "B";
    document.getElementById("stock-about").textContent = activeStock.about;

    const posInfo = document.getElementById("position-info");
    if (posInfo) {
      const pos = user.holdings[activeStock.symbol];
      posInfo.innerHTML = pos ? `Current Position: <strong>${pos.shares} Shares</strong> (Avg Cost: $${pos.avgCost.toFixed(2)})` : "No current positions held in asset index.";
    }

    runPreviewMath();
  }

  const btnBuy = document.getElementById("btn-type-buy");
  const btnSell = document.getElementById("btn-type-sell");

  if (btnBuy && btnSell) {
    btnBuy.addEventListener("click", () => { mode = "buy"; btnBuy.classList.add("active"); btnSell.classList.remove("active"); runPreviewMath(); });
    btnSell.addEventListener("click", () => { mode = "sell"; btnSell.classList.add("active"); btnBuy.classList.remove("active"); runPreviewMath(); });
  }

  if (orderShares) orderShares.addEventListener("input", runPreviewMath);

  function runPreviewMath() {
    if (!activeStock) return;
    const qty = parseInt(orderShares.value) || 0;
    const cost = qty * activeStock.price;
    const remainder = mode === "buy" ? user.cash - cost : user.cash + cost;

    document.getElementById("prev-price").textContent = "$" + activeStock.price.toFixed(2);
    document.getElementById("prev-shares").textContent = qty;
    document.getElementById("prev-total").textContent = "$" + cost.toFixed(2);
    document.getElementById("prev-cash").textContent = "$" + remainder.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // Handle transactional order execution confirmations
  const submitBtn = document.getElementById("btn-execute-trade");
  const modal = document.getElementById("trade-modal");
  const confirmGrid = document.getElementById("confirm-grid");

  if (submitBtn && modal && confirmGrid) {
    submitBtn.addEventListener("click", () => {
      const qty = parseInt(orderShares.value) || 0;
      if (qty <= 0) { alert("Enter valid quantity fields."); return; }

      confirmGrid.innerHTML = `
        <div class="confirm-item"><div class="confirm-label">ASSET TICKER</div><div class="confirm-value">${activeStock.symbol}</div></div>
        <div class="confirm-item"><div class="confirm-label">ACTION METHOD</div><div class="confirm-value">${mode.toUpperCase()}</div></div>
        <div class="confirm-item"><div class="confirm-label">SHARES SPECIFIED</div><div class="confirm-value">${qty}</div></div>
        <div class="confirm-item"><div class="confirm-label">TOTAL TRANSACTION</div><div class="confirm-value">$${(qty * activeStock.price).toFixed(2)}</div></div>
      `;
      modal.classList.remove("hidden");
    });
  }

  const hideModal = () => modal.classList.add("hidden");
  if (document.getElementById("modal-cancel")) document.getElementById("modal-cancel").addEventListener("click", hideModal);
  if (document.getElementById("modal-close")) document.getElementById("modal-close").addEventListener("click", hideModal);

  const confirmBtn = document.getElementById("modal-confirm");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      const qty = parseInt(orderShares.value) || 0;
      try {
        Portfolio.executeTransaction(user, activeStock.symbol, mode, qty, activeStock.price);
        hideModal();
        orderShares.value = "";
        refreshCashView();
        focusTicker(activeStock.symbol);
      } catch (err) {
        alert(err.message);
      }
    });
  }

  // Check URL parameters for direct trade navigation links
  const params = new URLSearchParams(window.location.search);
  const deepLinkSym = params.get("sym");
  if (deepLinkSym) focusTicker(deepLinkSym);
});