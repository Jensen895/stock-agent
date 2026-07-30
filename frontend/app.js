// UI layer — talks ONLY to the REST API, never to storage directly.

const STOCKS_API = "/api/stocks";
const WISHLIST_API = "/api/wishlist";
const SUMMARY_API = "/api/summary";

// --- Dashboard: total worth + realized/unrealized gains (read only) ----

const totalWorthEl = document.getElementById("total-worth");
const realizedValueEl = document.getElementById("realized-value");
const realizedIntervalsEl = document.getElementById("realized-intervals");
const unrealizedValueEl = document.getElementById("unrealized-value");
const unrealizedIntervalsEl = document.getElementById("unrealized-intervals");
const unrealizedChartEl = document.getElementById("unrealized-chart");
const chartWrapEl = document.getElementById("chart-wrap");
const chartGuideEl = document.getElementById("chart-guide");
const chartDotEl = document.getElementById("chart-dot");
const chartReadoutEl = document.getElementById("chart-readout");
const sellHistoryBody = document.getElementById("sell-history-body");

// Geometry of the chart currently drawn, so the hover handler can map a mouse
// position to the nearest data point. Set by drawChart().
let currentChart = null;

// Selected intervals are remembered so the app reopens on the last choice.
const REALIZED_KEY = "stockagent.realizedInterval";
const UNREALIZED_KEY = "stockagent.unrealizedInterval";
const DEFAULT_INTERVAL = "1m";

let summaryData = null;
let togglesBuilt = false;

async function loadSummary() {
  try {
    const res = await fetch(SUMMARY_API);
    summaryData = await res.json();
    renderSummary();
  } catch {
    totalWorthEl.textContent = "—";
  }
}

function renderSummary() {
  if (!summaryData) return;
  totalWorthEl.textContent = `$${fmt(summaryData.total_worth)}`;
  buildIntervalToggles();
  renderRealized();
  renderUnrealized();
}

// Build the interval toggle buttons once, from the intervals the API reports.
function buildIntervalToggles() {
  if (togglesBuilt || !summaryData) return;
  const intervals = summaryData.intervals || [];
  buildToggle(realizedIntervalsEl, intervals, (key) => {
    setStoredInterval(REALIZED_KEY, key);
    renderRealized();
  });
  buildToggle(unrealizedIntervalsEl, intervals, (key) => {
    setStoredInterval(UNREALIZED_KEY, key);
    renderUnrealized();
  });
  togglesBuilt = true;
}

function buildToggle(container, intervals, onSelect) {
  container.innerHTML = intervals
    .map(
      (iv) =>
        `<button type="button" class="interval-btn" data-key="${escapeHtml(
          iv.key
        )}">${escapeHtml(iv.label)}</button>`
    )
    .join("");
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-key]");
    if (btn) onSelect(btn.getAttribute("data-key"));
  });
}

function renderRealized() {
  if (!summaryData) return;
  const key = getStoredInterval(REALIZED_KEY);
  setActiveInterval(realizedIntervalsEl, key);
  const value = (summaryData.realized || {})[key] ?? 0;
  applyGainValue(realizedValueEl, value);
  renderSellHistory();
}

// Sell history: one row per recorded sale — ticker, shares, realized gain.
function renderSellHistory() {
  const sales = (summaryData && summaryData.sales) || [];
  if (!sales.length) {
    sellHistoryBody.innerHTML =
      `<tr><td colspan="3" class="empty">No sells yet.</td></tr>`;
    return;
  }
  sellHistoryBody.innerHTML = sales
    .map((s) => {
      const gain = s.realized_gain;
      const sign = gain > 0 ? "+" : gain < 0 ? "−" : "";
      const cls = gain > 0 ? "pos" : gain < 0 ? "neg" : "";
      return `<tr>
        <td>${escapeHtml(s.ticker)}</td>
        <td class="num">${fmt(s.shares)}</td>
        <td class="num ${cls}">${sign}$${fmt(Math.abs(gain))}</td>
      </tr>`;
    })
    .join("");
}

function renderUnrealized() {
  if (!summaryData) return;
  const key = getStoredInterval(UNREALIZED_KEY);
  setActiveInterval(unrealizedIntervalsEl, key);
  const entry = (summaryData.unrealized || {})[key] || { value: 0, series: [] };
  applyGainValue(unrealizedValueEl, entry.value);
  drawChart(unrealizedChartEl, entry.series, entry.value, key);
}

// Show a signed dollar amount, green when positive, red when negative.
function applyGainValue(el, value) {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  el.textContent = `${sign}$${fmt(Math.abs(value))}`;
  el.classList.toggle("pos", value > 0);
  el.classList.toggle("neg", value < 0);
}

function setActiveInterval(container, key) {
  container.querySelectorAll("[data-key]").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-key") === key);
  });
}

function getStoredInterval(storageKey) {
  try {
    return localStorage.getItem(storageKey) || DEFAULT_INTERVAL;
  } catch {
    return DEFAULT_INTERVAL;
  }
}

function setStoredInterval(storageKey, value) {
  try {
    localStorage.setItem(storageKey, value);
  } catch {
    /* localStorage unavailable (private mode) — selection just won't persist. */
  }
}

// Draw a simple line graph (x = time, y = usd) as inline SVG. The line/fill is
// green when the latest value is positive, red when negative — matching the
// number above it.
function drawChart(svg, series, value, intervalKey) {
  if (!series || series.length < 2) {
    svg.innerHTML = "";
    currentChart = null;
    hideChartCursor();
    return;
  }
  const W = 480;
  const H = 140;
  const pad = 8;
  const vals = series.map((p) => p.v);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const n = series.length;
  const x = (i) => pad + (i / (n - 1)) * (W - pad * 2);
  const y = (v) => H - pad - ((v - min) / range) * (H - pad * 2);

  const pts = series.map((p, i) => `${x(i).toFixed(1)},${y(p.v).toFixed(1)}`);
  const line = "M" + pts.join(" L");
  const area =
    `M${x(0).toFixed(1)},${(H - pad).toFixed(1)} L` +
    pts.join(" L") +
    ` L${x(n - 1).toFixed(1)},${(H - pad).toFixed(1)} Z`;
  const color = value < 0 ? "var(--err)" : "var(--ok)";

  // Dashed zero baseline, only when the series crosses zero.
  let zeroLine = "";
  if (min < 0 && max > 0) {
    const zy = y(0).toFixed(1);
    zeroLine = `<line x1="${pad}" y1="${zy}" x2="${W - pad}" y2="${zy}"
      stroke="var(--border)" stroke-width="1" stroke-dasharray="4 4"
      vector-effect="non-scaling-stroke" />`;
  }

  svg.innerHTML = `
    ${zeroLine}
    <path d="${area}" fill="${color}" fill-opacity="0.12" stroke="none" />
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"
      vector-effect="non-scaling-stroke" />`;

  // Store geometry (as fractions of the box) so hovering can place the overlay
  // dot/guide exactly on the drawn line regardless of the box's pixel size.
  currentChart = {
    series,
    n,
    color,
    intervalKey,
    xFrac: (i) => x(i) / W,
    yFrac: (v) => y(v) / H,
    // invert a horizontal fraction of the box back to the nearest point index
    indexAt: (frac) => {
      const i = Math.round(((frac * W - pad) / (W - pad * 2)) * (n - 1));
      return Math.max(0, Math.min(n - 1, i));
    },
  };
  hideChartCursor();
}

// --- Chart hover: show the value + time of the nearest point ------------

function hideChartCursor() {
  chartGuideEl.hidden = true;
  chartDotEl.hidden = true;
}

function resetChartReadout() {
  chartReadoutEl.textContent = "Hover the line to see value & time.";
  chartReadoutEl.classList.remove("pos", "neg");
}

chartWrapEl.addEventListener("mousemove", (e) => {
  if (!currentChart) return;
  const rect = chartWrapEl.getBoundingClientRect();
  if (!rect.width) return;

  const frac = (e.clientX - rect.left) / rect.width;
  const i = currentChart.indexAt(frac);
  const point = currentChart.series[i];

  // Position the guide line and dot on the drawn line.
  chartGuideEl.style.left = `${currentChart.xFrac(i) * 100}%`;
  chartDotEl.style.left = `${currentChart.xFrac(i) * 100}%`;
  chartDotEl.style.top = `${currentChart.yFrac(point.v) * 100}%`;
  chartDotEl.style.background = currentChart.color;
  chartGuideEl.hidden = false;
  chartDotEl.hidden = false;

  // Readout under the chart: value (green/red) + time.
  const v = point.v;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  chartReadoutEl.textContent =
    `${sign}$${fmt(Math.abs(v))} · ${fmtTime(point.t, currentChart.intervalKey)}`;
  chartReadoutEl.classList.toggle("pos", v > 0);
  chartReadoutEl.classList.toggle("neg", v < 0);
});

chartWrapEl.addEventListener("mouseleave", () => {
  hideChartCursor();
  resetChartReadout();
});

// --- Stocks: view (read only) -----------------------------------------

const tbody = document.getElementById("stocks-body");
const refreshBtn = document.getElementById("refresh");

async function loadStocks() {
  try {
    const res = await fetch(STOCKS_API);
    const data = await res.json();
    renderStocks(data.stocks || []);
  } catch {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">Could not reach the API.</td></tr>`;
  }
}

function renderStocks(stocks) {
  if (!stocks.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">No stocks yet. Buy one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = stocks
    .map((s) => {
      const costBasis = s.shares * s.avg_price;
      const ticker = escapeHtml(s.ticker);
      return `<tr>
        <td>${ticker}</td>
        <td class="num">${fmt(s.shares)}</td>
        <td class="num">$${fmt(s.avg_price)}</td>
        <td class="num">$${fmt(costBasis)}</td>
        <td class="actions-col">
          <button class="link-btn" data-sell="${ticker}">Sell</button>
          <button class="link-btn danger" data-delete="${ticker}">Delete</button>
        </td>
      </tr>`;
    })
    .join("");
}

// Row actions — event-delegated so they work on re-rendered rows.
tbody.addEventListener("click", async (e) => {
  // Sell: prefill the Sell Stock form with this ticker and jump to it.
  const sellBtn = e.target.closest("[data-sell]");
  if (sellBtn) {
    const ticker = sellBtn.getAttribute("data-sell");
    const sellTicker = document.getElementById("sell-ticker");
    sellTicker.value = ticker;
    setMessage(sellMsg, "", "");
    sellForm.scrollIntoView({ behavior: "smooth", block: "center" });
    document.getElementById("sell-shares").focus();
    return;
  }

  // Delete: remove an entire holding.
  const btn = e.target.closest("[data-delete]");
  if (!btn) return;
  const ticker = btn.getAttribute("data-delete");
  if (!confirm(`Delete your entire ${ticker} holding? This cannot be undone.`)) {
    return;
  }
  try {
    const res = await fetch(STOCKS_API, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(sellMsg, data.error || "Could not delete.", "err");
      return;
    }
    setMessage(sellMsg, `Deleted ${data.deleted} from your holdings.`, "ok");
    loadStocks();
    loadSummary();
  } catch {
    setMessage(sellMsg, "Could not reach the API.", "err");
  }
});

refreshBtn.addEventListener("click", loadStocks);

// --- Stocks: buy / add (write) ----------------------------------------

const buyForm = document.getElementById("buy-form");
const buyMsg = document.getElementById("buy-message");

buyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  setMessage(buyMsg, "", "");

  const payload = {
    ticker: document.getElementById("ticker").value,
    shares: parseFloat(document.getElementById("shares").value),
    avg_price: parseFloat(document.getElementById("avg_price").value),
  };

  try {
    const res = await fetch(STOCKS_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(buyMsg, data.error || "Something went wrong.", "err");
      return;
    }
    const s = data.stock;
    setMessage(
      buyMsg,
      `Saved ${s.ticker}: ${fmt(s.shares)} shares @ $${fmt(s.avg_price)} avg.`,
      "ok"
    );
    buyForm.reset();
    loadStocks();
    loadSummary();
  } catch {
    setMessage(buyMsg, "Could not reach the API.", "err");
  }
});

// --- Stocks: sell (write) ---------------------------------------------

const sellForm = document.getElementById("sell-form");
const sellMsg = document.getElementById("sell-message");

sellForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  setMessage(sellMsg, "", "");

  const payload = {
    ticker: document.getElementById("sell-ticker").value,
    shares: parseFloat(document.getElementById("sell-shares").value),
    price: parseFloat(document.getElementById("sell-price").value),
  };

  try {
    const res = await fetch(`${STOCKS_API}/sell`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(sellMsg, data.error || "Something went wrong.", "err");
      return;
    }
    const sale = data.sale;
    const proceeds = `$${fmt(sale.proceeds)}`;
    const tail = sale.sold_out
      ? `Sold out of ${sale.ticker}.`
      : `${fmt(sale.remaining)} shares of ${sale.ticker} remaining.`;
    setMessage(
      sellMsg,
      `Sold ${fmt(sale.sold_shares)} ${sale.ticker} @ $${fmt(sale.sale_price)} (${proceeds}). ${tail}`,
      "ok"
    );
    sellForm.reset();
    loadStocks();
    loadSummary();
  } catch {
    setMessage(sellMsg, "Could not reach the API.", "err");
  }
});

// --- Wishlist (read + write) ------------------------------------------

const wishlistForm = document.getElementById("wishlist-form");
const wishlistMsg = document.getElementById("wishlist-message");
const wishlistList = document.getElementById("wishlist-list");

async function loadWishlist() {
  try {
    const res = await fetch(WISHLIST_API);
    const data = await res.json();
    renderWishlist(data.wishlist || []);
  } catch {
    wishlistList.innerHTML = `<li class="empty">Could not reach the API.</li>`;
  }
}

function renderWishlist(items) {
  if (!items.length) {
    wishlistList.innerHTML = `<li class="empty">Nothing on your wishlist yet.</li>`;
    return;
  }
  wishlistList.innerHTML = items
    .map((w) => {
      const ticker = escapeHtml(w.ticker);
      return `<li class="tag">
        <span>${ticker}</span>
        <button class="tag-remove" data-remove="${ticker}" title="Remove">&times;</button>
      </li>`;
    })
    .join("");
}

wishlistForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  setMessage(wishlistMsg, "", "");

  const payload = { ticker: document.getElementById("wishlist-ticker").value };

  try {
    const res = await fetch(WISHLIST_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(wishlistMsg, data.error || "Something went wrong.", "err");
      return;
    }
    setMessage(wishlistMsg, `Added ${data.entry.ticker} to your wishlist.`, "ok");
    wishlistForm.reset();
    loadWishlist();
  } catch {
    setMessage(wishlistMsg, "Could not reach the API.", "err");
  }
});

wishlistList.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-remove]");
  if (!btn) return;
  const ticker = btn.getAttribute("data-remove");
  try {
    const res = await fetch(WISHLIST_API, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(wishlistMsg, data.error || "Could not remove.", "err");
      return;
    }
    setMessage(wishlistMsg, `Removed ${data.removed} from your wishlist.`, "ok");
    loadWishlist();
  } catch {
    setMessage(wishlistMsg, "Could not reach the API.", "err");
  }
});

// --- helpers ----------------------------------------------------------

function setMessage(el, text, kind) {
  el.textContent = text;
  el.className = "message" + (kind ? " " + kind : "");
}

function fmt(n) {
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// Format an ISO timestamp for the chart readout, with granularity matched to
// the selected interval:
//   1D  -> minutes            ("Jul 29, 3:45 PM")
//   1W  -> snapped to 3 hours ("Jul 29, 3:00 PM")
//   1M  -> snapped to 2 days  ("Jul 28, 2026")
//   1Y  -> first day of week  ("Jul 26, 2026", the Sunday of that week)
function fmtTime(iso, intervalKey) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;

  if (intervalKey === "1d") {
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  if (intervalKey === "1w") {
    // Snap down to a 3-hour grid, keeping the date since a week spans days.
    const h = new Date(d);
    h.setMinutes(0, 0, 0);
    h.setHours(h.getHours() - (h.getHours() % 3));
    return h.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  // Date-only intervals: snap to the interval's granularity, then format.
  const s = new Date(d);
  s.setHours(0, 0, 0, 0);
  if (intervalKey === "1m") {
    // Snap down to a 2-day grid (stable across months via days-since-epoch).
    const dayNum = Math.floor((s.getTime() - s.getTimezoneOffset() * 60000) / 86400000);
    s.setDate(s.getDate() - (dayNum % 2));
  } else if (intervalKey === "1y") {
    s.setDate(s.getDate() - s.getDay()); // back to Sunday, the start of the week
  }
  return s.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Refresh button also refreshes the dashboard totals.
refreshBtn.addEventListener("click", loadSummary);

// initial load
loadSummary();
loadStocks();
loadWishlist();
