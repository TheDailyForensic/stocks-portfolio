/**
 * STOCKSIM -- FOOTER TICKER STREAM
 * Loops pricing labels dynamically across platform templates
 */

document.addEventListener("DOMContentLoaded", () => {
  const tape = document.getElementById("ticker-tape");
  if (!tape) return;

  const trackers = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL"];
  tape.innerHTML = "";

  // Duplicate arrays to build an unbroken animation carousel sequence
  const loopCollection = [...trackers, ...trackers, ...trackers];

  loopCollection.forEach(sym => {
    const data = StockAPI.getMockData(sym);
    const box = document.createElement("div");
    box.className = "tt-item";
    box.innerHTML = `
      <span class="tt-sym">${sym}</span>
      <span class="tt-price">$${data.price.toFixed(2)}</span>
      <span class="tt-chg ${data.change >= 0 ? 'up' : 'down'}">
        ${data.change >= 0 ? '+' : ''}${data.changesPercentage.toFixed(2)}%
      </span>
      <span class="tt-sep">//</span>
    `;
    tape.appendChild(box);
  });
});