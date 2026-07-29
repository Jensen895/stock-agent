// UI layer — talks ONLY to the REST API, never to storage directly.

const API = "/api/stocks";

const form = document.getElementById("add-form");
const message = document.getElementById("message");
const tbody = document.getElementById("stocks-body");
const refreshBtn = document.getElementById("refresh");

// --- View stocks (read only) ------------------------------------------

async function loadStocks() {
  try {
    const res = await fetch(API);
    const data = await res.json();
    renderStocks(data.stocks || []);
  } catch {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">Could not reach the API.</td></tr>`;
  }
}

function renderStocks(stocks) {
  if (!stocks.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">No stocks yet. Add one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = stocks
    .map((s) => {
      const costBasis = s.shares * s.avg_price;
      return `<tr>
        <td>${escapeHtml(s.ticker)}</td>
        <td class="num">${fmt(s.shares)}</td>
        <td class="num">$${fmt(s.avg_price)}</td>
        <td class="num">$${fmt(costBasis)}</td>
      </tr>`;
    })
    .join("");
}

// --- Add stock (write only) -------------------------------------------

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  setMessage("", "");

  const payload = {
    ticker: document.getElementById("ticker").value,
    shares: parseFloat(document.getElementById("shares").value),
    avg_price: parseFloat(document.getElementById("avg_price").value),
  };

  try {
    const res = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(data.error || "Something went wrong.", "err");
      return;
    }
    const s = data.stock;
    setMessage(
      `Saved ${s.ticker}: ${fmt(s.shares)} shares @ $${fmt(s.avg_price)} avg.`,
      "ok"
    );
    form.reset();
    loadStocks();
  } catch {
    setMessage("Could not reach the API.", "err");
  }
});

refreshBtn.addEventListener("click", loadStocks);

// --- helpers ----------------------------------------------------------

function setMessage(text, kind) {
  message.textContent = text;
  message.className = "message" + (kind ? " " + kind : "");
}

function fmt(n) {
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// initial load
loadStocks();
