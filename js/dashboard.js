/**
 * STOCKSIM -- CENTRAL PORTFOLIO MANAGEMENT PANEL
 * Updates user matrices and renders statistical line/donut charts
 */

document.addEventListener("DOMContentLoaded", async () => {
  const user = Auth.requireAuth();
  if (!user) return;

  const timeEl = document.getElementById("header-time");
  if (timeEl) timeEl.textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

  const state = await Portfolio.getCalculatedState(user);

  // Set quantitative DOM metrics
  document.getElementById("kv-total").textContent = "$" + state.totalPortfolioValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  document.getElementById("kv-cash").textContent = "$" + state.cash.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  document.getElementById("kv-invested").textContent = "$" + state.investedValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const retEl = document.getElementById("kv-return");
  retEl.textContent = `${state.absoluteReturn >= 0 ? "+" : ""}$${state.absoluteReturn.toFixed(2)} (${state.percentageReturn.toFixed(2)}%)`;
  retEl.className = state.absoluteReturn >= 0 ? "kpi-value gain" : "kpi-value loss";

  // Build structural position arrays
  const table = document.getElementById("holdings-table");
  const empty = document.getElementById("holdings-empty");
  const body = document.getElementById("holdings-body");

  if (state.holdingsList.length > 0 && body) {
    if (empty) empty.classList.add("hidden");
    if (table) table.classList.remove("hidden");
    body.innerHTML = "";

    state.holdingsList.forEach(item => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="sym-badge">${item.symbol}</span></td>
        <td style="font-family:var(--font-body);">${item.name}</td>
        <td>${item.shares}</td>
        <td>$${item.avgCost.toFixed(2)}</td>
        <td>$${item.currentPrice.toFixed(2)}</td>
        <td>$${item.marketValue.toFixed(2)}</td>
        <td class="${item.gainLoss >= 0 ? 'gain' : 'loss'}">${item.gainLoss >= 0 ? '+' : ''}$${item.gainLoss.toFixed(2)}</td>
        <td class="${item.returnsPct >= 0 ? 'gain' : 'loss'}">${item.returnsPct >= 0 ? '+' : ''}${item.returnsPct.toFixed(2)}%</td>
        <td><a href="trade.html?sym=${item.symbol}" class="btn-sm">TRADE</a></td>
      `;
      body.appendChild(tr);
    });
  }

  // Inject recent transaction items
  const activityList = document.getElementById("activity-list");
  if (activityList && user.history && user.history.length > 0) {
    activityList.innerHTML = "";
    user.history.slice(0, 5).forEach(log => {
      const li = document.createElement("li");
      li.style.padding = "0.6rem 0";
      li.style.borderBottom = "1px solid var(--border)";
      li.style.fontFamily = "var(--font-mono)";
      li.style.fontSize = "0.8rem";
      li.innerHTML = `<span class="${log.type === 'BUY' ? 'gain' : 'loss'}">[${log.type}]</span> ${log.shares} ${log.symbol} @ $${log.price.toFixed(2)} (${log.date.split(',')[0]})`;
      activityList.appendChild(li);
    });
  }

  // Draw main Chart.js historical portfolio line graph
  const ctx = document.getElementById("portfolio-chart");
  if (ctx) {
    new Chart(ctx.getContext("2d"), {
      type: "line",
      data: {
        labels: user.datesHistory,
        datasets: [{
          data: user.equityHistory,
          borderColor: "#00ff88",
          borderWidth: 2,
          pointRadius: 2,
          pointBackgroundColor: "#080c0f",
          fill: false,
          tension: 0.1
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: "#1e2a38" }, ticks: { color: "#7a9ab8", font: { family: "Share Tech Mono" } } },
          y: { grid: { color: "#1e2a38" }, ticks: { color: "#7a9ab8", font: { family: "Share Tech Mono" } } }
        }
      }
    });
  }

  // Draw portfolio allocation doughnut chart
  const pieCtx = document.getElementById("alloc-chart");
  if (pieCtx) {
    const labels = state.holdingsList.map(h => h.symbol);
    const dataVals = state.holdingsList.map(h => h.marketValue);

    if (labels.length === 0) {
      labels.push("Liquid Cash");
      dataVals.push(user.cash);
    }

    new Chart(pieCtx.getContext("2d"), {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [{
          data: dataVals,
          backgroundColor: ["#00ff88", "#4da6ff", "#ffd24d", "#ff3860", "#a8d8b8"],
          borderWidth: 1,
          borderColor: "#0d1117"
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom", labels: { color: "#e8f0f8", font: { family: "Share Tech Mono" } } } }
      }
    });
  }
});