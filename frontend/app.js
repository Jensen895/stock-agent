// UI layer — talks ONLY to the REST API, never to storage directly.

const STOCKS_API = "/api/stocks";
const WISHLIST_API = "/api/wishlist";
const SUMMARY_API = "/api/summary";
const AI_API = "/api/ai";

// --- Dashboard: total worth + realized/unrealized gains (read only) ----

const totalWorthEl = document.getElementById("total-worth");
const realizedValueEl = document.getElementById("realized-value");
const realizedIntervalsEl = document.getElementById("realized-intervals");
const unrealizedValueEl = document.getElementById("unrealized-value");
const unrealizedViewsEl = document.getElementById("unrealized-views");
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
const UNREALIZED_KEY = "stockagent.unrealizedView";
const DEFAULT_INTERVAL = "1m";
const DEFAULT_UNREALIZED_VIEW = "1m";

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
  // Realized: 1D/1W/1M/YTD/1Y windows. Unrealized: Today / Total views.
  buildToggle(realizedIntervalsEl, summaryData.intervals || [], (key) => {
    setStoredInterval(REALIZED_KEY, key);
    renderRealized();
  });
  buildToggle(unrealizedViewsEl, summaryData.unrealized_views || [], (key) => {
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
  // Views are 1D/1W/1M/YTD/1Y/Total. Fall back to the default if the saved
  // choice isn't offered (e.g. an older stored value).
  const validKeys = (summaryData.unrealized_views || []).map((v) => v.key);
  let key = getStoredInterval(UNREALIZED_KEY, DEFAULT_UNREALIZED_VIEW);
  if (!validKeys.includes(key)) {
    key = validKeys.includes(DEFAULT_UNREALIZED_VIEW)
      ? DEFAULT_UNREALIZED_VIEW
      : validKeys[0];
  }
  setActiveInterval(unrealizedViewsEl, key);
  const entry =
    (summaryData.unrealized || {})[key] || { value: null, pct: null, series: [] };
  applyGain(unrealizedValueEl, entry.value, entry.pct);
  // The 1D line is intraday (minute readout); the rest are daily (date readout).
  drawChart(unrealizedChartEl, entry.series, entry.value, key === "total" ? "1y" : key);
}

// Show a signed dollar amount, green when positive, red when negative.
function applyGainValue(el, value) {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  el.textContent = `${sign}$${fmt(Math.abs(value))}`;
  el.classList.toggle("pos", value > 0);
  el.classList.toggle("neg", value < 0);
}

// Signed dollar amount + percentage — "+$123.45 (+2.34%)" — colored by sign.
// A null value (no live price) renders as an em dash.
function applyGain(el, value, pct) {
  if (value == null) {
    el.textContent = "—";
    el.classList.remove("pos", "neg");
    return;
  }
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const pctText = pct == null ? "" : ` (${sign}${fmt(Math.abs(pct))}%)`;
  el.textContent = `${sign}$${fmt(Math.abs(value))}${pctText}`;
  el.classList.toggle("pos", value > 0);
  el.classList.toggle("neg", value < 0);
}

function setActiveInterval(container, key) {
  container.querySelectorAll("[data-key]").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-key") === key);
  });
}

function getStoredInterval(storageKey, fallback = DEFAULT_INTERVAL) {
  try {
    return localStorage.getItem(storageKey) || fallback;
  } catch {
    return fallback;
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
    tbody.innerHTML = `<tr><td colspan="7" class="empty">Could not reach the API.</td></tr>`;
  }
}

function renderStocks(stocks) {
  if (!stocks.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No stocks yet. Buy one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = stocks
    .map((s) => {
      const ticker = escapeHtml(s.ticker);
      return `<tr>
        ${tickerCell(ticker, s.name)}
        <td class="num">${fmt(s.shares)}</td>
        <td class="num">$${fmt(s.avg_price)}</td>
        ${priceCell(s.price, s.today)}
        ${gainCell(s.total)}
        ${earningsCell(s.earnings)}
        <td class="actions-col">
          <button class="link-btn" data-buy="${ticker}">Buy</button>
          <button class="link-btn" data-sell="${ticker}">Sell</button>
          <button class="link-btn danger" data-delete="${ticker}">Delete</button>
        </td>
      </tr>`;
    })
    .join("");
}

// A right-aligned table cell for a {value, pct} gain: "+$12.34 (+1.2%)",
// green when positive, red when negative, an em dash when there's no data.
function gainCell(g) {
  if (!g || g.value == null) return `<td class="num">—</td>`;
  const v = g.value;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
  const pct = g.pct == null ? "" : ` (${sign}${fmt(Math.abs(g.pct))}%)`;
  return `<td class="num ${cls}">${sign}$${fmt(Math.abs(v))}${pct}</td>`;
}

// --- two-line cells ---------------------------------------------------
//
// Three pairs of figures get read together — a ticker and its company, a price
// and today's move, an earnings date and what the company reported — so each
// pair shares one cell, the second line small and quiet under the first. That
// keeps these tables to a width that fits beside the AI column.

// The symbol, with the company's full name underneath. Long names are clipped
// with an ellipsis and carry the whole thing as a tooltip. `ticker` arrives
// already escaped; `name` is escaped here.
function tickerCell(ticker, name) {
  const full = name ? escapeHtml(name) : "";
  const sub = full ? `<span class="co-name" title="${full}">${full}</span>` : "";
  return `<td><div class="cell-stack">
    <span class="cell-main">${ticker}</span>${sub}
  </div></td>`;
}

// The live price, with today's move as a small colored tag below it.
function priceCell(price, gain) {
  const main = price == null ? "—" : `$${fmt(price)}`;
  return `<td class="num"><div class="cell-stack">
    <span class="cell-main">${main}</span>${dayTag(gain)}
  </div></td>`;
}

// The move tag itself: "+$12.34 (+1.2%)". Empty when there's no quote to
// compare against — the price above it is already showing an em dash.
function dayTag(g) {
  if (!g || g.value == null) return "";
  const v = g.value;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "flat";
  const pct = g.pct == null ? "" : ` (${sign}${fmt(Math.abs(g.pct))}%)`;
  return `<span class="day-tag ${cls}">${sign}$${fmt(Math.abs(v))}${pct}</span>`;
}

// The earnings date, plus — when the company reported within the last week —
// what it actually earned against what the street expected. A result already
// in tells you more than a date three months out, so the recent report wins
// the cell and brings its outcome with it.
function earningsCell(e) {
  if (!e || !e.date) return `<td class="num earnings">—</td>`;
  return `<td class="num earnings"><div class="cell-stack">
    <span class="cell-main">${fmtEarnings(e.date)}</span>${epsTag(e)}
  </div></td>`;
}

// "$2.06 vs $1.85 exp" — green on a beat, red on a miss. Colored by the
// surprise, not by the EPS itself: a loss narrower than feared is good news.
function epsTag(e) {
  if (e.eps_actual == null) return "";
  const surprise = e.surprise_pct;
  const cls = surprise > 0 ? "pos" : surprise < 0 ? "neg" : "flat";
  const versus =
    e.eps_estimate == null ? "actual" : `vs ${fmtEps(e.eps_estimate)} exp`;
  return `<span class="eps-tag ${cls}">${fmtEps(e.eps_actual)} ${versus}</span>`;
}

// Prefill the Buy / Add Stock form with a ticker and jump to it. Shared by the
// holdings rows (buying more of what you hold) and the wishlist rows.
function prefillBuy(ticker) {
  document.getElementById("ticker").value = ticker;
  setMessage(buyMsg, "", "");
  buyForm.scrollIntoView({ behavior: "smooth", block: "center" });
  document.getElementById("shares").focus();
}

// Row actions — event-delegated so they work on re-rendered rows.
tbody.addEventListener("click", async (e) => {
  // Buy: top up an existing position through the Buy / Add Stock form.
  const buyBtn = e.target.closest("[data-buy]");
  if (buyBtn) {
    prefillBuy(buyBtn.getAttribute("data-buy"));
    return;
  }

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
const wishlistBody = document.getElementById("wishlist-body");

async function loadWishlist() {
  try {
    const res = await fetch(WISHLIST_API);
    const data = await res.json();
    renderWishlist(data.wishlist || []);
  } catch {
    wishlistBody.innerHTML = `<tr><td colspan="5" class="empty">Could not reach the API.</td></tr>`;
  }
}

function renderWishlist(items) {
  if (!items.length) {
    wishlistBody.innerHTML = `<tr><td colspan="5" class="empty">Nothing on your wishlist yet.</td></tr>`;
    return;
  }
  wishlistBody.innerHTML = items
    .map((w) => {
      const ticker = escapeHtml(w.ticker);
      const open = w.open == null ? "—" : `$${fmt(w.open)}`;
      // Change vs. the open price: green above the open, red below. It rides
      // under the price, the same way today's move does in the holdings table.
      const change =
        w.change == null ? null : { value: w.change, pct: w.change_pct };
      return `<tr>
        ${tickerCell(ticker, w.name)}
        <td class="num">${open}</td>
        ${priceCell(w.price, change)}
        ${earningsCell(w.earnings)}
        <td class="actions-col">
          <button class="link-btn" data-buy="${ticker}">Buy</button>
          <button class="link-btn danger" data-remove="${ticker}" title="Remove">Remove</button>
        </td>
      </tr>`;
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

wishlistBody.addEventListener("click", async (e) => {
  // Buy: prefill the Buy / Add Stock form with this ticker and jump to it.
  const buyBtn = e.target.closest("[data-buy]");
  if (buyBtn) {
    prefillBuy(buyBtn.getAttribute("data-buy"));
    return;
  }

  // Remove: drop the ticker from the wishlist.
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

// An EPS figure: "$2.06", or "−$0.10" for a loss.
function fmtEps(v) {
  return `${v < 0 ? "−" : ""}$${fmt(Math.abs(v))}`;
}

// Format an earnings date ("YYYY-MM-DD") as "Aug 15, 2026". Em dash if unknown.
function fmtEarnings(iso) {
  if (!iso) return "—";
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return escapeHtml(iso);
  return d.toLocaleDateString(undefined, {
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

// Refresh button also refreshes the dashboard totals and the wishlist (both
// now carry live prices).
refreshBtn.addEventListener("click", loadSummary);
refreshBtn.addEventListener("click", loadWishlist);

// --- AI Advisor (read only) -------------------------------------------

const aiUpdatedEl = document.getElementById("ai-updated");
const aiStatusEl = document.getElementById("ai-status");
const aiSummaryView = document.getElementById("ai-summary-view");
const aiSummaryList = document.getElementById("ai-summary-list");
const aiPortfolioNote = document.getElementById("ai-portfolio-note");
const aiWishlistSection = document.getElementById("ai-wishlist-section");
const aiWishlistList = document.getElementById("ai-wishlist-list");
const aiWishlistNote = document.getElementById("ai-wishlist-note");
const aiDetailsView = document.getElementById("ai-details-view");
const aiHoldingsDetailsWrap = document.getElementById("ai-holdings-details-wrap");
const aiDetailsList = document.getElementById("ai-details-list");
const aiWishlistDetailsWrap = document.getElementById("ai-wishlist-details-wrap");
const aiWishlistDetailsList = document.getElementById("ai-wishlist-details-list");
const aiBackBtn = document.getElementById("ai-back");
const aiRefreshBtn = document.getElementById("ai-refresh");
const riskToggleEl = document.getElementById("risk-toggle");
const aiWeightsEl = document.getElementById("ai-weights");
const aiWeightsListEl = document.getElementById("ai-weights-list");
const aiWeightsTallyEl = document.getElementById("ai-weights-tally");
const aiWeightsApplyBtn = document.getElementById("ai-weights-apply");
const aiWeightsResetBtn = document.getElementById("ai-weights-reset");
const aiWeightsMsg = document.getElementById("ai-weights-message");

const DEFAULT_RISK = "low";

let aiData = null;
let aiPollTimer = null; // fast poll while a refresh is in flight

// Map an action to a display label + color class. The backend derives the
// action from the blended confidence score, so these are the score's colours
// too: buy green, hold grey, trim amber, sell red.
const AI_ACTIONS = {
  buy: { label: "Buy more", cls: "buy" },
  hold: { label: "Hold", cls: "hold" },
  trim: { label: "Sell part", cls: "trim" },
  sell: { label: "Sell all", cls: "sell" },
};

// The AI risk toggle is remembered per portfolio, server-side, so each
// portfolio reopens on its own choice (and switching portfolios shows that
// portfolio's setting). It rides along on the portfolio entry from
// /api/portfolios; toggling persists it via /api/portfolios/risk.
function getRisk() {
  const p = activePortfolio();
  return (p && p.risk) || DEFAULT_RISK;
}
function setRisk(risk) {
  // Update the loaded state optimistically so the UI reacts instantly, then
  // persist it to the active portfolio.
  const p = activePortfolio();
  if (p) p.risk = risk;
  fetch(`${PORTFOLIOS_API}/risk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ risk }),
  }).catch(() => {
    /* offline — the optimistic choice still applies for this session */
  });
}

async function loadAI() {
  try {
    const res = await fetch(AI_API);
    aiData = await res.json();
    renderAI();
  } catch {
    aiStatusEl.textContent = "Could not reach the AI API.";
    aiStatusEl.className = "ai-status err";
  }
}

function renderAI() {
  if (!aiData) return;

  // Highlight the active risk button.
  const risk = getRisk();
  riskToggleEl.querySelectorAll("[data-risk]").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-risk") === risk);
  });

  // Status line + last-updated stamp. Now that suggestions regenerate once a
  // day rather than every couple of hours, a lone timestamp reads as stale
  // when it isn't — so say when the next one is due alongside it.
  setAiStatus();
  aiUpdatedEl.textContent = aiData.generated_at
    ? `Updated ${fmtUpdated(aiData.generated_at)}` +
      (aiData.next_refresh ? ` · next ${fmtUpdated(aiData.next_refresh)}` : "")
    : "—";

  const profile =
    (aiData.risk_profiles && aiData.risk_profiles[risk]) || null;
  renderAgentWeights();
  renderAiSummary(profile);
  renderAiDetails(profile);
  renderAiWishlist(profile);

  // Keep polling until a running refresh completes.
  if (aiData.refreshing) startAiPoll();
  else stopAiPoll();
}

function setAiStatus() {
  aiStatusEl.className = "ai-status";
  if (!aiData.configured) {
    aiStatusEl.className = "ai-status err";
    aiStatusEl.textContent =
      "AI advisor is off — no model is configured. See the README to enable it.";
  } else if (aiData.refreshing) {
    aiStatusEl.textContent = "Thinking… generating fresh suggestions.";
  } else if (aiData.error && !aiData.risk_profiles) {
    aiStatusEl.className = "ai-status err";
    aiStatusEl.textContent = aiData.error;
  } else if (!aiData.risk_profiles) {
    aiStatusEl.textContent = "No suggestions yet — hit Refresh to generate them.";
  } else {
    // Say how many of the five agents actually scored, how often they landed
    // in the same place, and where the average came out. A missing agent is
    // worth flagging: it means a whole dimension is absent from the number.
    const profile = aiData.risk_profiles[getRisk()];
    const bits = [];
    const answered = agentsOf(profile).length;
    const expected = (aiData.agents || []).length;
    if (answered && expected && answered < expected) {
      const missing = (aiData.agents || [])
        .filter((a) => !agentsOf(profile).some((x) => x.key === a.key))
        .map((a) => a.short)
        .join(", ");
      bits.push(
        `only ${answered} of ${expected} agents scored — ` +
          `no ${missing} view in these numbers`,
      );
      aiStatusEl.className = "ai-status warn";
    } else if (answered) {
      bits.push(`${answered} independent agents`);
    }
    const ag = profile && profile.agreement;
    if (ag && ag.total && answered > 1) {
      bits.push(
        `${ag.agreed}/${ag.total} agreed` + (ag.split ? ` · ${ag.split} split` : ""),
      );
    }
    if (profile && profile.avg_confidence != null) {
      bits.push(`avg score ${Math.round(profile.avg_confidence)}`);
    }
    if (aiData.model_errors && aiData.model_errors.length) {
      bits.push(`${aiData.model_errors.length} agent call(s) failed`);
      aiStatusEl.className = "ai-status warn";
    }
    aiStatusEl.textContent = bits.join(" — ");
  }
  aiRefreshBtn.disabled = !aiData.configured || aiData.refreshing;
}

// --- Confidence score display ------------------------------------------
//
// Each stock carries one 0-100 score: the weighted average of the five agents,
// each of which worked alone on its own evidence. 100 = strong buy, 50 = hold,
// 0 = strong sell, so the number IS the call and the action badge just names
// the band it falls in.

// The agents that scored this profile, in roster order. Falls back to the
// advisor-level roster so the weight controls still render before the first
// generation, and to [] for suggestions saved before the five-agent split.
function agentsOf(profile) {
  if (profile && profile.agents) return profile.agents;
  return [];
}

// The agent roster the backend reports, for the weight controls and for
// labelling per-agent chips even when an agent didn't answer.
function agentRoster() {
  return (aiData && aiData.agents) || [];
}

function agentMeta(key) {
  return agentRoster().find((a) => a.key === key) || null;
}

// The score of a suggestion, or null for suggestions saved before scoring
// existed (the panel still renders those from their action alone).
function scoreOf(s) {
  return typeof s.confidence === "number" ? s.confidence : null;
}

// The score as a pill, coloured by its band.
function scorePill(s, extraClass = "") {
  const score = scoreOf(s);
  if (score == null) return "";
  const cls = (AI_ACTIONS[s.action] || AI_ACTIONS.hold).cls;
  const label = s.confidence_label || "";
  return `<span class="ai-score ${cls}${extraClass ? " " + extraClass : ""}"
    title="Confidence ${Math.round(score)}/100${label ? ` — ${escapeHtml(label)}` : ""}
 (100 = buy hard, 50 = hold, 0 = sell out)">${Math.round(score)}</span>`;
}

// A 0-100 track with a tick at the neutral midpoint and a marker at the score,
// so the distance from "hold" is visible at a glance.
function scoreMeter(score, action) {
  if (score == null) return "";
  const cls = (AI_ACTIONS[action] || AI_ACTIONS.hold).cls;
  const pos = Math.max(0, Math.min(100, score));
  return `<div class="ai-meter" role="img"
    aria-label="Confidence ${Math.round(score)} out of 100">
    <span class="ai-meter-mid"></span>
    <span class="ai-meter-dot ${cls}" style="left:${pos}%"></span>
  </div>`;
}

// The band name ("Strong buy", "Hold", "Lean trim", ...) when the backend
// supplied one, else the plain action label.
function bandLabel(s) {
  const a = AI_ACTIONS[s.action] || AI_ACTIONS.hold;
  return { cls: a.cls, text: s.confidence_label || a.label };
}

// The five numbers behind the average, rendered smaller than the blended score
// they produce. Slots are keyed to the agent roster rather than to the order
// sources happen to arrive in, so each position always means the same agent and
// a reader learns where to look; an agent with no score for this ticker shows
// an em dash rather than shifting the others along.
//
// A zero-weighted agent is dimmed rather than hidden — it still has a view, it
// just isn't counted right now, and hiding it would make the average look like
// it was reached by fewer opinions than were actually gathered.
function miniScores(s, agents) {
  const sources = s.sources || [];
  if (!sources.length) return "";

  // Old saved data scored by models, not agents — render it as it was.
  if (!sources.some((v) => v.kind === "agent")) {
    return legacyMiniScores(sources);
  }

  const order = (agents && agents.length ? agents : agentRoster()).map((a) => a.key);
  const keys = order.length ? order : sources.map((v) => v.key);
  const cells = keys
    .map((key) => {
      const meta = agentMeta(key) || {};
      const src = sources.find((v) => v.key === key) || null;
      const short = escapeHtml((src && src.short) || meta.short || "??");
      const name = escapeHtml((src && src.name) || meta.name || key);
      if (!src) {
        return `<span class="ai-mini" title="${name} — no score for this stock">
          <span class="ai-mini-tag">${short}</span>
          <span class="ai-mini-val">—</span></span>`;
      }
      const cls = (AI_ACTIONS[src.action] || AI_ACTIONS.hold).cls;
      const muted = !src.weight ? " muted" : "";
      const weightNote = src.weight
        ? `weight ×${fmtWeight(src.weight)}`
        : "weight 0 — not counted";
      return `<span class="ai-mini${muted}" title="${name} — ${Math.round(
        src.confidence
      )}/100${src.label ? ` (${escapeHtml(src.label)})` : ""} · ${weightNote}">
        <span class="ai-mini-tag">${short}</span>
        <span class="ai-mini-val ${cls}">${Math.round(src.confidence)}</span>
      </span>`;
    })
    .join("");
  return `<span class="ai-minis">${cells}</span>`;
}

// Suggestions generated before the five-agent split carry one source per model.
function legacyMiniScores(sources) {
  const cells = sources
    .filter((v) => v.kind === "model")
    .map((v) => {
      const cls = (AI_ACTIONS[v.action] || AI_ACTIONS.hold).cls;
      return `<span class="ai-mini" title="${escapeHtml(v.name)} — ${Math.round(
        v.confidence
      )}/100">
        <span class="ai-mini-tag">AI</span>
        <span class="ai-mini-val ${cls}">${Math.round(v.confidence)}</span>
      </span>`;
    })
    .join("");
  return cells ? `<span class="ai-minis">${cells}</span>` : "";
}

// One summary bullet — shared by the holdings list and the wishlist so both
// read the same. Laid out over three lines rather than one: the ticker, its
// score, the call and the Details button sit on top; the per-model numbers and
// horizon drop underneath. In a 370px column a single line would wrap into a
// jumble and push the button somewhere unpredictable.
//
// ``kind`` tags the row so the details view knows which list to filter.
function summaryRow(s, agents, kind) {
  const band = bandLabel(s);
  const ticker = escapeHtml(s.ticker);
  const sub = [miniScores(s, agents), `<span class="ai-horizon">${fmtHorizon(s)}</span>`]
    .filter(Boolean)
    .join("");
  // The one-liner comes from whichever agent moved the score most, so say
  // which one — an unattributed headline reads as a consensus view, and this
  // deliberately isn't one.
  const from = s.headline_from ? agentMeta(s.headline_from) : null;
  const attribution = from
    ? ` <span class="ai-from" title="${escapeHtml(from.name)} — the agent that
 moved this score the most">${escapeHtml(from.short)}</span>`
    : "";
  return `<li>
    <div class="ai-row-top">
      <span class="ai-ticker">${ticker}</span>
      ${scorePill(s)}
      <span class="ai-action ${band.cls}">${escapeHtml(band.text)}</span>
      <button type="button" class="ai-row-detail-btn" data-ticker="${ticker}"
        data-kind="${kind}" title="See details for ${ticker}">Details →</button>
    </div>
    <div class="ai-row-sub">${sub}</div>
    ${scoreMeter(scoreOf(s), s.action)}
    ${
      s.headline
        ? `<span class="ai-line">${escapeHtml(s.headline)}${attribution}</span>`
        : ""
    }
    ${
      kind === "wishlist" && s.price_trigger
        ? `<span class="ai-line trigger">↳ ${escapeHtml(s.price_trigger)}</span>`
        : ""
    }
  </li>`;
}

// The per-agent notes on the list as a whole. Kept as five short paragraphs
// rather than merged into one: they were written from five different bodies of
// evidence, and stitching them together would imply a synthesis nobody did.
function renderNotes(container, profile) {
  if (!container) return;
  const notes = (profile && profile.portfolio_notes) || [];
  if (!notes.length) {
    // Older saved data had a single blended note.
    const legacy = profile && profile.portfolio_note;
    container.innerHTML = legacy
      ? `<p class="ai-note">${escapeHtml(legacy)}</p>`
      : "";
    return;
  }
  container.innerHTML = notes
    .map(
      (n) => `<p class="ai-note">
        <span class="ai-note-tag" title="${escapeHtml(n.name)}">${escapeHtml(
        n.short
      )}</span>${escapeHtml(n.note)}</p>`
    )
    .join("");
}

// --- List controls: sort + show -----------------------------------------
//
// Two dozen holdings is more than anyone reads top to bottom, so each list
// carries its own ordering and its own band filter. Both work on the rows
// already on screen — no model call, nothing recomputed, no request — which is
// what makes them worth flipping between: "who do the statistics hate?" is one
// click away from "who does the street love?".
//
// Only the holdings list gets a Show filter. The wishlist arrives already cut
// to the buy band by the backend, so every row in it is a buy: a control for
// choosing between calls would offer a choice that doesn't exist there. It
// still sorts — `show: null` is what marks a list as unfiltered.

const AI_SHOW_BANDS = ["buy", "hold", "trim", "sell"];

const AI_LIST_DEFAULTS = {
  holdings: { sort: "list", show: [...AI_SHOW_BANDS] },
  wishlist: { sort: "score:desc", show: null },
};

const AI_LIST_PREFS_KEY = "stockagent.aiList";

// Element handles per list, looked up once. `ctl` is the whole control block —
// hidden when the list it belongs to is empty, since sorting nothing is noise.
const AI_LIST_ELS = {
  holdings: {
    ctl: document.getElementById("ai-holdings-ctl"),
    sort: document.getElementById("ai-holdings-sort"),
    show: document.getElementById("ai-holdings-show"),
    count: document.getElementById("ai-holdings-count"),
  },
  // No `show` / `count` here: the wishlist sorts but doesn't filter.
  wishlist: {
    ctl: document.getElementById("ai-wishlist-ctl"),
    sort: document.getElementById("ai-wishlist-sort"),
    show: null,
    count: null,
  },
};

let aiListPrefsCache = null;

function aiListPrefs(kind) {
  if (!aiListPrefsCache) aiListPrefsCache = loadAiListPrefs();
  return aiListPrefsCache[kind];
}

// Read the saved settings, validating as we go: a stored value that no longer
// means anything (an agent that left the roster, a band that was renamed) falls
// back to the default rather than silently emptying the list.
function loadAiListPrefs() {
  const out = {};
  for (const kind of Object.keys(AI_LIST_DEFAULTS)) {
    const fallback = AI_LIST_DEFAULTS[kind];
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(`${AI_LIST_PREFS_KEY}.${kind}`));
    } catch {
      saved = null;
    }
    const saveable =
      fallback.show && saved && Array.isArray(saved.show)
        ? saved.show.filter((b) => AI_SHOW_BANDS.includes(b))
        : null;
    out[kind] = {
      sort: (saved && typeof saved.sort === "string" && saved.sort) || fallback.sort,
      // An empty saved selection is a real choice ("show me nothing"), so it is
      // kept; a missing or corrupt one isn't, and falls back. A list with no
      // Show control stays null — unfiltered, not "filtered to everything".
      show: saveable || (fallback.show ? [...fallback.show] : null),
    };
  }
  return out;
}

function saveAiListPrefs(kind) {
  try {
    localStorage.setItem(
      `${AI_LIST_PREFS_KEY}.${kind}`,
      JSON.stringify(aiListPrefs(kind))
    );
  } catch {
    /* localStorage unavailable (private mode) — the choice holds for this session */
  }
}

// The band a suggestion falls in, defaulting to hold for anything unrecognised
// so a stray action never drops a row out of every filter at once.
function actionOf(s) {
  return AI_ACTIONS[s && s.action] ? s.action : "hold";
}

// One agent's own score for a stock, or null when that agent didn't answer for
// it. Sorting by an agent that skipped half the list is legitimate — the ones
// it skipped simply sink to the bottom.
function agentScoreOf(s, key) {
  const src = (s.sources || []).find((v) => v.kind === "agent" && v.key === key);
  return src && typeof src.confidence === "number" ? src.confidence : null;
}

// Sort values are "field[:key]:direction"; "list" means the order the backend
// produced (portfolio order for holdings, best-first for the wishlist buys).
function aiComparator(sort) {
  const parts = String(sort || "").split(":");
  if (parts[0] === "ticker") {
    const dir = parts[1] === "desc" ? -1 : 1;
    return (x, y) => dir * byTicker(x, y);
  }
  if (parts[0] === "score") {
    return byScore((s) => scoreOf(s), parts[1] === "asc" ? 1 : -1);
  }
  if (parts[0] === "agent" && parts[1]) {
    return byScore((s) => agentScoreOf(s, parts[1]), parts[2] === "asc" ? 1 : -1);
  }
  return null; // "list" — leave the backend's order alone
}

function byTicker(x, y) {
  return String(x.ticker || "").localeCompare(String(y.ticker || ""));
}

// Sort on a number that may be missing. Unscored rows always sink to the
// bottom, whichever direction is chosen — flipping to "low first" to surface
// the worst stocks shouldn't hand you a screen of em dashes instead. Ties fall
// back to the ticker so the order is stable between renders.
function byScore(pick, dir) {
  return (x, y) => {
    const a = pick(x);
    const b = pick(y);
    if (a == null && b == null) return byTicker(x, y);
    if (a == null) return 1;
    if (b == null) return -1;
    return a === b ? byTicker(x, y) : dir * (a - b);
  };
}

// Apply one list's saved sort, and its band filter if it has one. Never mutates
// the input: the suggestions array belongs to the loaded API payload and other
// panels read it.
function aiListView(list, kind) {
  const prefs = aiListPrefs(kind);
  const shown = prefs.show
    ? (list || []).filter((s) => prefs.show.includes(actionOf(s)))
    : list || [];
  const cmp = aiComparator(prefs.sort);
  return cmp ? [...shown].sort(cmp) : shown;
}

// The sort menu, built from the live agent roster so the per-agent entries are
// named by whoever is actually scoring — and so an agent added later shows up
// here without a second edit.
function sortOptionsHtml(kind) {
  const listLabel = kind === "wishlist" ? "Wishlist order" : "Portfolio order";
  const agents = agentRoster()
    .map((a) => {
      const name = escapeHtml(`${a.short} · ${a.name}`);
      const title = escapeHtml(a.focus || a.name || "");
      return `<option value="agent:${escapeHtml(a.key)}:desc" title="${title}"
          >${name} — high first</option>
        <option value="agent:${escapeHtml(a.key)}:asc" title="${title}"
          >${name} — low first</option>`;
    })
    .join("");
  return `<option value="list">${listLabel}</option>
    <option value="ticker:asc">Ticker A → Z</option>
    <option value="ticker:desc">Ticker Z → A</option>
    <option value="score:desc">Avg score — high first</option>
    <option value="score:asc">Avg score — low first</option>
    ${agents ? `<optgroup label="One agent's score">${agents}</optgroup>` : ""}`;
}

function showChipsHtml() {
  const chips = AI_SHOW_BANDS.map((band) => {
    const meta = AI_ACTIONS[band];
    return `<button type="button" class="ai-show-btn ${band}" data-band="${band}"
      aria-pressed="false" title="${escapeHtml(meta.label)}">${escapeHtml(
      band[0].toUpperCase() + band.slice(1)
    )}</button>`;
  }).join("");
  return `<button type="button" class="ai-show-btn all" data-band="all"
      aria-pressed="false"
      title="Show every call — click again to clear them all">All</button>${chips}`;
}

// Draw the controls for one list. The markup is rebuilt only when it would
// actually differ — a rebuild on the two-minute poll would close an open sort
// menu under the cursor — so the usual path just re-marks the active chips.
function renderAiListControls(kind, total) {
  const els = AI_LIST_ELS[kind];
  if (!els || !els.sort) return;
  els.ctl.hidden = !total;
  if (!total) return;

  const signature = agentRoster()
    .map((a) => a.key)
    .join(",");
  if (els.sort.getAttribute("data-roster") !== signature) {
    els.sort.innerHTML = sortOptionsHtml(kind);
    els.sort.setAttribute("data-roster", signature);
  }
  const prefs = aiListPrefs(kind);
  els.sort.value = prefs.sort;
  // The saved sort named an option that no longer exists (an agent left the
  // roster). Fall back rather than leaving the menu blank.
  if (!els.sort.value) {
    prefs.sort = AI_LIST_DEFAULTS[kind].sort;
    els.sort.value = prefs.sort;
  }

  if (!els.show) return; // sort-only list — nothing else to draw
  if (!els.show.childElementCount) els.show.innerHTML = showChipsHtml();

  const all = AI_SHOW_BANDS.every((b) => prefs.show.includes(b));
  els.show.querySelectorAll("[data-band]").forEach((btn) => {
    const band = btn.getAttribute("data-band");
    const on = band === "all" ? all : prefs.show.includes(band);
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

// "5 of 23" — said out loud so a filter never quietly swallows rows. Silent
// when nothing is hidden; there is no news in "23 of 23".
function setAiListCount(kind, shown, total) {
  const el = AI_LIST_ELS[kind] && AI_LIST_ELS[kind].count;
  if (!el) return;
  el.textContent = shown === total ? "" : `${shown} of ${total}`;
  el.title = shown === total ? "" : `${total - shown} hidden by the Show filter`;
}

// What to say when the filter empties a list that does have rows in it.
function aiFilterEmptyText(kind, total) {
  if (!aiListPrefs(kind).show.length) {
    return "No calls selected — pick at least one above.";
  }
  return `Nothing in the selected calls — all ${total} hidden.`;
}

// Flip one chip. Bands are stored in their canonical order however they were
// clicked, so the saved value always reads like the row of chips.
function toggleShowBand(kind, band) {
  const prefs = aiListPrefs(kind);
  if (band === "all") {
    // "All" is a check-all/clear-all: it fills the set, and clicking it again
    // once everything is on empties it. Emptying the list is a real thing to
    // want — a blank slate to build a selection back up from — so the list
    // says why it's blank rather than the chip refusing the click.
    prefs.show = AI_SHOW_BANDS.every((b) => prefs.show.includes(b))
      ? []
      : [...AI_SHOW_BANDS];
  } else if (prefs.show.includes(band)) {
    prefs.show = prefs.show.filter((b) => b !== band);
  } else {
    prefs.show = AI_SHOW_BANDS.filter((b) => b === band || prefs.show.includes(b));
  }
  saveAiListPrefs(kind);
}

// Wire both lists' controls once. Changing either re-renders the panel from the
// data already loaded, so it costs a repaint and nothing else.
for (const kind of Object.keys(AI_LIST_ELS)) {
  const els = AI_LIST_ELS[kind];

  if (els.sort) {
    els.sort.addEventListener("change", () => {
      aiListPrefs(kind).sort = els.sort.value;
      saveAiListPrefs(kind);
      renderAI();
    });
  }

  // Absent on a sort-only list.
  if (els.show) {
    els.show.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-band]");
      if (!btn) return;
      toggleShowBand(kind, btn.getAttribute("data-band"));
      renderAI();
    });
  }
}

// Summary: bullet per stock — ticker, the blended score, the band it falls in,
// the source scores behind it, horizon, meter.
function renderAiSummary(profile) {
  const suggestions = (profile && profile.suggestions) || [];
  renderAiListControls("holdings", suggestions.length);
  if (!suggestions.length) {
    setAiListCount("holdings", 0, 0);
    aiSummaryList.innerHTML = `<li class="ai-empty">No suggestions yet.</li>`;
    aiPortfolioNote.innerHTML = "";
    return;
  }
  const shown = aiListView(suggestions, "holdings");
  setAiListCount("holdings", shown.length, suggestions.length);
  const agents = agentsOf(profile);
  aiSummaryList.innerHTML = shown.length
    ? shown.map((s) => summaryRow(s, agents, "holdings")).join("")
    : `<li class="ai-empty">${escapeHtml(
        aiFilterEmptyText("holdings", suggestions.length)
      )}</li>`;
  renderNotes(aiPortfolioNote, profile);
}

// Wishlist buys (minimized): only rendered when the AI flags a real buy.
// The backend already dropped everything below the buy threshold, so an empty
// list means "nothing worth entering today" and the section stays hidden — it
// doesn't exist until it has something to say. Which is also why this list
// sorts but doesn't filter: everything left in it is a buy.
function renderAiWishlist(profile) {
  const wl = (profile && profile.wishlist) || null;
  const suggestions = (wl && wl.suggestions) || [];
  if (!aiWishlistSection || !aiWishlistList) return;
  if (!suggestions.length) {
    aiWishlistSection.hidden = true;
    renderAiListControls("wishlist", 0);
    aiWishlistList.innerHTML = "";
    if (aiWishlistNote) aiWishlistNote.innerHTML = "";
    return;
  }
  const agents = agentsOf(wl).length ? agentsOf(wl) : agentsOf(profile);
  aiWishlistSection.hidden = false;
  renderAiListControls("wishlist", suggestions.length);
  aiWishlistList.innerHTML = aiListView(suggestions, "wishlist")
    .map((s) => summaryRow(s, agents, "wishlist"))
    .join("");
  renderNotes(aiWishlistNote, wl);
}

// The firms behind an analyst score — who upgraded, downgraded, or reiterated.
function firmsBlock(source) {
  const firms = source.firms || [];
  if (!firms.length) return "";
  const arrows = { up: "▲", down: "▼", init: "＋", main: "=", reit: "=" };
  const rows = firms
    .map((f) => {
      const arrow = arrows[f.action] || "·";
      const cls =
        f.action === "up" ? "pos" : f.action === "down" ? "neg" : "";
      const target = f.price_target ? ` · $${fmt(f.price_target)}` : "";
      return `<li>
        <span class="ai-firm-mark ${cls}">${arrow}</span>
        <span class="ai-firm">${escapeHtml(f.firm)}</span>
        <span class="ai-firm-grade">${escapeHtml(f.grade)}${target}</span>
        <span class="ai-firm-date">${escapeHtml(f.date || "")}</span>
      </li>`;
    })
    .join("");
  return `<ul class="ai-firms">${rows}</ul>`;
}

// The whole argument behind the blended score: each agent's number, the weight
// it carried, and the case it actually made.
//
// This is the part of the UI the five-agent split exists for. One model writing
// one paragraph hides which evidence drove the verdict; five short arguments,
// each labelled with the dimension it came from, let you see that (say) the
// numbers hate a stock the street loves — and then move a slider about it.
function sourcesBlock(s) {
  const sources = (s.sources || []).filter((v) => v.kind !== "analyst");
  if (!sources.length) return "";
  const agentSources = sources.filter((v) => v.kind === "agent");
  const rows = (agentSources.length ? agentSources : sources)
    .map((v) => {
      const cls = (AI_ACTIONS[v.action] || AI_ACTIONS.hold).cls;
      const counted = v.weight > 0;
      const trigger = v.price_trigger
        ? `<p class="ai-src-trigger">↳ ${escapeHtml(v.price_trigger)}</p>`
        : "";
      const risk = v.risks
        ? `<p class="ai-src-risk">Risk: ${escapeHtml(v.risks)}</p>`
        : "";
      return `<li class="ai-source agent${counted ? "" : " muted"}">
        <div class="ai-src-head">
          <span class="ai-src-tag" title="${escapeHtml(v.focus || "")}">${escapeHtml(
        v.short || "?"
      )}</span>
          <span class="ai-src-name">${escapeHtml(v.name || v.key || "")}</span>
          <span class="ai-score sm ${cls}">${Math.round(v.confidence)}</span>
          <span class="ai-src-weight" title="${
            counted
              ? "Weight in the blended score"
              : "Weight 0 — this agent's view is not counted"
          }">×${fmtWeight(v.weight)}</span>
        </div>
        ${scoreMeter(v.confidence, v.action)}
        ${v.detail ? `<p class="ai-src-headline">${escapeHtml(v.detail)}</p>` : ""}
        ${v.reasoning ? `<p class="ai-src-reason">${escapeHtml(v.reasoning)}</p>` : ""}
        ${trigger}
        ${risk}
        ${
          v.model
            ? `<span class="ai-src-model" title="Which model ran this agent —
 capacity, not opinion">${escapeHtml(v.model)}</span>`
            : ""
        }
      </li>`;
    })
    .join("");
  const score = scoreOf(s);
  const counted = (agentSources.length ? agentSources : sources).filter(
    (v) => v.weight > 0
  ).length;
  const heading =
    score == null
      ? "The agents"
      : `Confidence ${Math.round(score)}/100 · weighted average of ${counted} ` +
        `agent${counted === 1 ? "" : "s"}`;
  return `<span class="ai-field-label">${escapeHtml(heading)}
    <span class="ai-evidence-tag" title="Each agent worked alone on its own
 evidence and never saw the others' answers">independent</span></span>
          <ul class="ai-sources">${rows}</ul>`;
}

// What Wall Street says — the raw research, shown because it is exactly what
// the expert agent (WS) read and nothing else did. It explains where that one
// agent's number came from without pretending to be a number of its own. Falls
// back to the legacy analyst *source* on suggestions saved when the street was
// still scored directly, so old data still renders.
function streetBlock(s) {
  const w =
    s.wall_street ||
    (s.sources || []).find((v) => v.kind === "analyst") ||
    null;
  if (!w) return "";

  // Bull / hold / bear head count as a single readable line.
  const counts = [];
  if (w.bulls != null) counts.push(`${w.bulls} buy`);
  if (w.neutral != null) counts.push(`${w.neutral} hold`);
  if (w.bears != null) counts.push(`${w.bears} sell`);

  const t = w.target || {};
  const spread =
    t.low && t.high
      ? `<p class="ai-street-line">Targets $${fmt(t.low)} – $${fmt(t.high)}${
          t.mean ? ` (mean $${fmt(t.mean)}` : ""
        }${
          t.upside_pct != null
            ? `, ${t.upside_pct > 0 ? "+" : ""}${fmt(t.upside_pct)}% from here)`
            : t.mean
            ? ")"
            : ""
        }</p>`
      : "";

  const head = [
    w.rating ? escapeHtml(w.rating) : "",
    w.mean != null ? `${w.mean}/5` : "",
    w.analyst_count ? `${w.analyst_count} analysts` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return `<span class="ai-field-label">What Wall Street says
    <span class="ai-evidence-tag" title="The only evidence the WS agent read.
 No other agent saw it.">WS agent's evidence</span></span>
    <div class="ai-street">
      ${head ? `<p class="ai-street-head">${head}</p>` : ""}
      ${counts.length ? `<p class="ai-street-line">${counts.join(" · ")}</p>` : ""}
      ${spread}
      ${
        w.disagreement_note
          ? `<p class="ai-street-note">${escapeHtml(w.disagreement_note)}</p>`
          : ""
      }
      ${firmsBlock(w)}
    </div>`;
}

// How far apart the five landed, in one line. A split is not a defect to be
// smoothed over — it is the most useful thing this panel can tell you, so it
// gets said out loud rather than left implicit in the numbers.
const AI_CONSENSUS = {
  agree: { text: "the agents broadly agree", cls: "agree" },
  mixed: { text: "the agents are mixed", cls: "mixed" },
  split: { text: "the agents are split — read all five", cls: "split" },
  single: { text: "only one agent scored this", cls: "single" },
};

function consensusBadge(s) {
  const c = AI_CONSENSUS[s.consensus];
  if (!c) return "";
  return `<span class="ai-consensus ${c.cls}">${escapeHtml(c.text)}</span>`;
}

// One detail card. The body is the five agents' own arguments rather than a
// single blended paragraph: nobody wrote one, because no agent saw enough to.
// Suggestions saved before the split still carry a top-level reasoning, so that
// is rendered when it's there.
function detailCard(s, kind) {
  const band = bandLabel(s);
  const wishlist = kind === "wishlist";
  const legacyReason = s.reasoning
    ? `<p class="ai-reason">${escapeHtml(s.reasoning)}</p>`
    : "";
  const legacyTrigger = s.price_trigger
    ? `<span class="ai-field-label">${wishlist ? "Entry" : "Price"} trigger</span>
       <p class="ai-trigger">${escapeHtml(s.price_trigger)}</p>`
    : "";
  const legacyRisks = s.risks
    ? `<span class="ai-field-label">Main risk</span>
       <p>${escapeHtml(s.risks)}</p>`
    : "";
  return `<div class="ai-detail${wishlist ? " wishlist" : ""}">
    <div class="ai-detail-head">
      <span class="ai-ticker">${escapeHtml(s.ticker)}</span>
      ${scorePill(s, "lg")}
      <span class="ai-action ${band.cls}">${escapeHtml(band.text)}</span>
      <span class="ai-horizon">${fmtHorizon(s)}</span>
      ${wishlist ? `<span class="ai-wishlist-badge">wishlist</span>` : ""}
    </div>
    ${scoreMeter(scoreOf(s), s.action)}
    ${consensusBadge(s)}
    ${legacyReason}
    ${legacyTrigger}
    ${legacyRisks}
    ${sourcesBlock(s)}
    ${streetBlock(s)}
  </div>`;
}

// Details view. A row's "Details →" sets a filter so only that stock's card
// renders — with two dozen holdings, opening one used to mean scrolling the
// whole list. Null filter shows everything.
let aiDetailFilter = null; // { ticker, kind } or null

// Pick the cards a list should show under the current filter: everything when
// unfiltered, the one ticker when this list owns the filter, nothing when the
// other list does.
//
// Unfiltered, the cards follow the summary's own sort and band filter — the
// two views are the same list at two depths, and having them disagree on what
// is in it would be worse than either order.
function detailsFor(suggestions, kind) {
  const filter = aiDetailFilter;
  if (!filter) return aiListView(suggestions, kind);
  if (filter.kind !== kind) return null; // not our list — stay hidden
  return (suggestions || []).filter((s) => s.ticker === filter.ticker);
}

function renderAiDetails(profile) {
  const suggestions = (profile && profile.suggestions) || [];
  const toShow = detailsFor(suggestions, "holdings");

  if (toShow === null) {
    aiHoldingsDetailsWrap.hidden = true;
    aiDetailsList.innerHTML = "";
  } else {
    aiHoldingsDetailsWrap.hidden = false;
    aiDetailsList.innerHTML = toShow.length
      ? toShow.map((s) => detailCard(s, "holdings")).join("")
      : `<p class="ai-empty">No details yet.</p>`;
  }
  // Wishlist details lives in its own wrap alongside the holdings details.
  renderAiWishlistDetails(profile);
}

function renderAiWishlistDetails(profile) {
  if (!aiWishlistDetailsWrap || !aiWishlistDetailsList) return;
  const wl = (profile && profile.wishlist) || null;
  const suggestions = (wl && wl.suggestions) || [];
  const toShow = suggestions.length ? detailsFor(suggestions, "wishlist") : null;

  if (toShow === null || !toShow.length) {
    aiWishlistDetailsWrap.hidden = true;
    aiWishlistDetailsList.innerHTML = "";
    return;
  }
  aiWishlistDetailsWrap.hidden = false;
  aiWishlistDetailsList.innerHTML = toShow
    .map((s) => detailCard(s, "wishlist"))
    .join("");
}

// Weights are usually whole numbers ("×1"); show a decimal only when there is one.
function fmtWeight(w) {
  const n = Number(w);
  if (!isFinite(n)) return "1";
  return Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/0+$/, "");
}

// "over what period" — the backend caps this at a quarter. Falls back to the
// legacy horizon_days field so suggestions saved before the switch to a one-to
// -three-month view still read sensibly.
function fmtHorizon(s) {
  let months = s && s.horizon_months;
  if (months == null && s && s.horizon_days != null) {
    months = Math.round(Number(s.horizon_days) / 30) || 1;
  }
  const m = Math.max(1, Math.min(3, Math.round(Number(months) || 1)));
  return m === 1 ? "over 1 month" : `over ${m} months`;
}

// A compact "updated" stamp: date + time, local.
function fmtUpdated(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// --- Agent weights ------------------------------------------------------
//
// How much each of the five agents counts toward the average. Applying posts
// them and the server re-blends the scores it already has — no model runs, so
// this comes back instantly and costs nothing. That is what makes the sliders
// worth having: you can ask "what if I didn't care what Wall Street thinks?"
// and see the whole panel resettle.

const WEIGHT_MAX = 5;
const WEIGHT_STEP = 0.25;

// Slider positions live here while the panel is open, so a re-render (the
// 2-minute poll, say) doesn't yank a half-adjusted slider back.
let weightDraft = null;

function currentWeights() {
  return (aiData && aiData.agent_weights) || {};
}

function renderAgentWeights() {
  if (!aiWeightsListEl) return;
  const roster = agentRoster();
  if (!roster.length) {
    aiWeightsEl.hidden = true;
    return;
  }
  aiWeightsEl.hidden = false;
  const weights = weightDraft || currentWeights();

  // Don't rebuild the sliders under someone's cursor. renderAI() runs on a
  // timer, and replacing the markup mid-drag would drop the drag and lose the
  // unapplied edit; the draft is already on screen and still correct.
  if (weightDraft && aiWeightsEl.open) {
    renderWeightTally(weights);
    return;
  }

  aiWeightsListEl.innerHTML = roster
    .map((a) => {
      const w = weights[a.key] != null ? weights[a.key] : 1;
      return `<div class="ai-weight-row${w > 0 ? "" : " off"}" data-key="${escapeHtml(
        a.key
      )}">
        <span class="ai-weight-tag" title="${escapeHtml(a.focus)}">${escapeHtml(
        a.short
      )}</span>
        <label class="ai-weight-name" for="w-${escapeHtml(a.key)}"
          title="${escapeHtml(a.focus)}">${escapeHtml(a.name)}</label>
        <input id="w-${escapeHtml(a.key)}" class="ai-weight-slider" type="range"
          min="0" max="${WEIGHT_MAX}" step="${WEIGHT_STEP}" value="${w}"
          data-key="${escapeHtml(a.key)}"
          aria-label="${escapeHtml(a.name)} weight" />
        <output class="ai-weight-val">${w > 0 ? "×" + fmtWeight(w) : "off"}</output>
      </div>`;
    })
    .join("");

  renderWeightTally(weights);
}

// The summary line shows the share of the score each agent carries, which is
// the number that actually matters — a weight of 2 means nothing until you know
// what the others are.
function renderWeightTally(weights) {
  if (!aiWeightsTallyEl) return;
  const roster = agentRoster();
  const total = roster.reduce((sum, a) => sum + (weights[a.key] || 0), 0);
  const off = roster.filter((a) => !(weights[a.key] > 0));
  if (!total) {
    aiWeightsTallyEl.textContent = "";
    return;
  }
  const equal = roster.every(
    (a) => (weights[a.key] || 0) === (weights[roster[0].key] || 0)
  );
  aiWeightsTallyEl.textContent = equal
    ? `equal — ${Math.round(100 / roster.length)}% each`
    : roster
        .filter((a) => weights[a.key] > 0)
        .map((a) => `${a.short} ${Math.round((weights[a.key] / total) * 100)}%`)
        .join(" · ") + (off.length ? ` · ${off.length} off` : "");
}

function readWeightDraft() {
  const out = {};
  aiWeightsListEl.querySelectorAll("[data-key].ai-weight-slider").forEach((el) => {
    out[el.getAttribute("data-key")] = parseFloat(el.value);
  });
  return out;
}

if (aiWeightsListEl) {
  aiWeightsListEl.addEventListener("input", (e) => {
    const slider = e.target.closest(".ai-weight-slider");
    if (!slider) return;
    weightDraft = readWeightDraft();
    const row = slider.closest(".ai-weight-row");
    const value = parseFloat(slider.value);
    row.classList.toggle("off", !(value > 0));
    const out = row.querySelector(".ai-weight-val");
    if (out) out.textContent = value > 0 ? "×" + fmtWeight(value) : "off";
    renderWeightTally(weightDraft);
    setMessage(aiWeightsMsg, "Not applied yet.", "");
  });
}

async function applyWeights(weights) {
  try {
    const res = await fetch(`${AI_API}/weights`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ weights }),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(aiWeightsMsg, data.error || "Could not apply weights.", "err");
      return;
    }
    weightDraft = null;
    aiData = data;
    renderAI();
    setMessage(aiWeightsMsg, "Applied — scores re-blended.", "ok");
  } catch {
    setMessage(aiWeightsMsg, "Could not reach the AI API.", "err");
  }
}

if (aiWeightsApplyBtn) {
  aiWeightsApplyBtn.addEventListener("click", () =>
    applyWeights(weightDraft || currentWeights())
  );
}
if (aiWeightsResetBtn) {
  aiWeightsResetBtn.addEventListener("click", () => {
    const defaults =
      (aiData && aiData.default_agent_weights) ||
      Object.fromEntries(agentRoster().map((a) => [a.key, 1]));
    weightDraft = { ...defaults };
    renderAgentWeights();
    applyWeights(defaults);
  });
}

// Risk toggle — re-render from the already-loaded data (no new API call).
// It drives the news column too: both panels generate at both risk settings,
// so switching is a re-render on either side rather than a second control.
riskToggleEl.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-risk]");
  if (!btn) return;
  setRisk(btn.getAttribute("data-risk"));
  renderAI();
  if (discoverData) renderDiscover();
});

// Per-stock "Details →" — one button per row, so opening a stock never means
// scrolling to the end of the list. Delegated from both summary lists; each
// button filters the details view to its own ticker.
function activeProfile() {
  return (aiData && aiData.risk_profiles && aiData.risk_profiles[getRisk()]) || null;
}
function enterDetails(ticker, kind) {
  aiDetailFilter = { ticker, kind };
  renderAiDetails(activeProfile());
  aiSummaryView.hidden = true;
  aiDetailsView.hidden = false;
  // Scrolls the card's body back to the top, and the card itself into view if
  // the page has been scrolled past it.
  aiDetailsView.scrollIntoView({ behavior: "smooth", block: "start" });
}
function exitDetails() {
  const from = aiDetailFilter;
  aiDetailFilter = null;
  renderAiDetails(activeProfile());
  aiDetailsView.hidden = true;
  aiSummaryView.hidden = false;
  // The list scrolls on its own now, so land back on the row that was opened
  // rather than at whatever offset the details view left behind.
  const row =
    from &&
    [...document.querySelectorAll(".ai-row-detail-btn")].find(
      (b) =>
        b.getAttribute("data-ticker") === from.ticker &&
        b.getAttribute("data-kind") === from.kind
    );
  if (row) row.scrollIntoView({ block: "center" });
  else aiSummaryView.scrollIntoView({ block: "start" });
}

function onDetailClick(e) {
  const btn = e.target.closest(".ai-row-detail-btn");
  if (!btn) return;
  const ticker = btn.getAttribute("data-ticker");
  if (ticker) enterDetails(ticker, btn.getAttribute("data-kind") || "holdings");
}
aiSummaryList.addEventListener("click", onDetailClick);
if (aiWishlistList) aiWishlistList.addEventListener("click", onDetailClick);

// "Back" returns to the summary and clears the per-stock filter.
aiBackBtn.addEventListener("click", exitDetails);

// Manual refresh: kick off a background regeneration, then poll for the result.
aiRefreshBtn.addEventListener("click", async () => {
  try {
    const res = await fetch(`${AI_API}/refresh`, { method: "POST" });
    const data = await res.json();
    if (data.started) {
      aiStatusEl.className = "ai-status";
      aiStatusEl.textContent = "Thinking… generating fresh suggestions.";
      aiRefreshBtn.disabled = true;
      startAiPoll();
    } else {
      loadAI();
    }
  } catch {
    aiStatusEl.textContent = "Could not reach the AI API.";
    aiStatusEl.className = "ai-status err";
  }
});

// While a refresh is running, poll every few seconds so the result appears
// without a manual reload.
function startAiPoll() {
  if (aiPollTimer) return;
  aiPollTimer = setInterval(loadAI, 5000);
}
function stopAiPoll() {
  if (aiPollTimer) {
    clearInterval(aiPollTimer);
    aiPollTimer = null;
  }
}

// Pick up the daily opening-bell refresh without a reload — and keep the
// "next refresh" stamp honest as the day rolls over.
setInterval(loadAI, 120000);

// --- In the News (read only) ------------------------------------------
//
// The one panel that starts from outside your list: three stocks the market is
// talking about that you neither hold nor watch. Each answers three questions
// in this order — why is it here (the chatter that surfaced it), what is it
// (the company), and what do the agents make of it (the score).
//
// That order matters. The backend scores these with the *same* five agents as
// the AI Advisor, so the numbers in the two columns are directly comparable —
// but a 64/100 on a ticker you've never heard of means nothing until you know
// what the company sells, so the score comes last rather than first.

const DISCOVER_API = "/api/discover";

const newsListEl = document.getElementById("news-list");
const newsStatusEl = document.getElementById("news-status");
const newsUpdatedEl = document.getElementById("news-updated");
const newsLanesEl = document.getElementById("news-lanes");
const newsRefreshBtn = document.getElementById("news-refresh");
const newsSummaryView = document.getElementById("news-summary-view");
const newsDetailsView = document.getElementById("news-details-view");
const newsDetailsList = document.getElementById("news-details-list");
const newsBackBtn = document.getElementById("news-back");

// What each lane is called on screen. The backend reports which source actually
// answered each one (Reddit or StockTwits for the retail lane), so the label
// here is the lane, not the source.
const NEWS_LANES = {
  reddit: { label: "Retail", cls: "reddit" },
  news: { label: "News", cls: "news" },
  wsj: { label: "WSJ", cls: "wsj" },
};

let discoverData = null;
let discoverPollTimer = null;
let newsDetailTicker = null; // which pick's agent breakdown is open

async function loadDiscover() {
  try {
    const res = await fetch(DISCOVER_API);
    discoverData = await res.json();
    renderDiscover();
  } catch {
    newsStatusEl.textContent = "Could not reach the discover API.";
    newsStatusEl.className = "ai-status err";
  }
}

// The suggestion for one pick, at the risk setting the advisor column is on.
// Both panels read the same per-portfolio toggle rather than each carrying
// their own — two risk switches on one screen would be a puzzle, not a control.
function discoverProfile() {
  const profiles = discoverData && discoverData.risk_profiles;
  return (profiles && profiles[getRisk()]) || null;
}

function suggestionFor(ticker) {
  const profile = discoverProfile();
  const list = (profile && profile.suggestions) || [];
  return list.find((s) => s.ticker === ticker) || null;
}

function renderDiscover() {
  if (!discoverData) return;
  setNewsStatus();

  newsUpdatedEl.textContent = discoverData.generated_at
    ? `Updated ${fmtUpdated(discoverData.generated_at)}`
    : "—";

  // Which rooms answered, and how much each returned. A lane that came back
  // empty is named too — a blocked source should be visible, not silent.
  const lanes = discoverData.lanes || [];
  newsLanesEl.textContent = lanes.length
    ? lanes.map((l) => `${l.label} ${l.headlines}`).join(" · ")
    : discoverData.sources || "";
  newsLanesEl.title = lanes.length
    ? "Headlines read per source in this refresh. A source showing 0 " +
      "didn't answer — its lane contributed nothing to these picks."
    : "";

  renderNewsPicks();
  renderNewsDetails();

  if (discoverData.refreshing) startDiscoverPoll();
  else stopDiscoverPoll();
}

function setNewsStatus() {
  newsStatusEl.className = "ai-status";
  const d = discoverData;
  if (!d.configured) {
    newsStatusEl.className = "ai-status err";
    newsStatusEl.textContent = d.model_configured === false
      ? "Discover is off — no AI model is configured. See the README."
      : "Discover is off — no chatter source is reachable.";
  } else if (d.refreshing) {
    newsStatusEl.textContent = "Reading the room… finding what's trending.";
  } else if (d.error && !d.picks) {
    newsStatusEl.className = "ai-status err";
    newsStatusEl.textContent = d.error;
  } else if (!d.picks) {
    newsStatusEl.textContent = "Nothing yet — hit Refresh to go looking.";
  } else if (!d.picks.length) {
    newsStatusEl.textContent =
      "Everything trending is already in your holdings or wishlist.";
  } else {
    const bits = [];
    if (d.considered) bits.push(`${d.considered} names considered`);
    if (d.skipped_known) bits.push(`${d.skipped_known} already yours`);
    if (d.model_errors && d.model_errors.length) {
      bits.push(`${d.model_errors.length} agent call(s) failed`);
      newsStatusEl.className = "ai-status warn";
    }
    newsStatusEl.textContent = bits.join(" — ");
  }
  newsRefreshBtn.disabled = !d.configured || !!d.refreshing;
}

function renderNewsPicks() {
  const picks = (discoverData && discoverData.picks) || [];
  if (!picks.length) {
    // The status line above already says which of the two empty cases this is.
    newsListEl.innerHTML = discoverData.picks
      ? ""
      : `<p class="ai-empty">No picks yet.</p>`;
    return;
  }
  const agents = agentsOf(discoverProfile());
  newsListEl.innerHTML = picks.map((p) => newsPick(p, agents)).join("");
}

// One pick: the chatter that found it, the company behind it, the agents' call.
function newsPick(pick, agents) {
  const ticker = escapeHtml(pick.ticker);
  const suggestion = suggestionFor(pick.ticker);
  const trending = pick.trending || {};

  return `<article class="news-pick" data-ticker="${ticker}">
    <div class="news-pick-head">
      <span class="ai-ticker">${ticker}</span>
      ${suggestion ? scorePill(suggestion) : ""}
      <span class="news-pick-price">${newsPrice(pick)}</span>
      ${
        pick.name
          ? `<span class="news-pick-name" title="${escapeHtml(
              pick.name
            )}">${escapeHtml(pick.name)}</span>`
          : ""
      }
    </div>

    <span class="news-label">Why it's being talked about</span>
    ${laneChips(trending)}
    ${headlineList(trending.headlines)}

    <span class="news-label">About the company</span>
    ${backgroundBlock(pick, ticker)}

    ${suggestionBlock(pick, suggestion, agents, ticker)}
  </article>`;
}

// Price with today's move beside it, coloured like the tables.
function newsPrice(pick) {
  if (pick.price == null) return "";
  const c = pick.change;
  if (!c || c.value == null) return `$${fmt(pick.price)}`;
  const sign = c.value > 0 ? "+" : c.value < 0 ? "−" : "";
  const cls = c.value > 0 ? "pos" : c.value < 0 ? "neg" : "flat";
  return `$${fmt(pick.price)}
    <span class="day-tag ${cls}">${sign}${fmt(Math.abs(c.pct))}%</span>`;
}

// One chip per room that mentioned it, with how loudly. Mentions are weighted
// counts, not raw headline tallies — a cashtag counts for more than a guessed
// company name — so they're rounded and labelled as a strength, not a total.
function laneChips(trending) {
  const mentions = trending.mentions || {};
  const chips = Object.keys(NEWS_LANES)
    .filter((lane) => mentions[lane])
    .map((lane) => {
      const meta = NEWS_LANES[lane];
      return `<span class="news-chip ${meta.cls}"
        title="Mention strength in the ${escapeHtml(meta.label)} lane. Weighted:
 a ticker written as $SYM counts for more than a company name matched in prose.">
        ${escapeHtml(meta.label)}
        <span class="news-chip-n">${Math.round(mentions[lane])}</span>
      </span>`;
    })
    .join("");
  return chips ? `<div class="news-lane-chips">${chips}</div>` : "";
}

function headlineList(headlines) {
  const items = (headlines || []).slice(0, 4);
  if (!items.length) return "";
  return `<ul class="news-heads">${items
    .map((h) => {
      const lane = (NEWS_LANES[h.lane] || {}).label || h.lane || "";
      const text = escapeHtml(h.headline || "");
      const body = h.url
        ? `<a href="${escapeHtml(h.url)}" target="_blank"
             rel="noopener noreferrer">${text}</a>`
        : text;
      const src = [h.source, h.datetime].filter(Boolean).map(escapeHtml).join(" · ");
      return `<li>
        <span class="news-head-lane">${escapeHtml(lane)}</span>
        <span>${body}${src ? `<span class="news-head-src">${src}</span>` : ""}</span>
      </li>`;
    })
    .join("")}</ul>`;
}

// What the company is. The facts line first — sector, size, valuation — then
// what it actually sells, clamped with the rest behind a toggle.
function backgroundBlock(pick, ticker) {
  const b = pick.background || {};
  const facts = [];
  if (b.sector) facts.push(["Sector", b.sector]);
  if (b.industry) facts.push(["Industry", b.industry]);
  if (b.market_cap != null) facts.push(["Market cap", fmtBig(b.market_cap)]);
  if (b.revenue_growth_pct != null)
    facts.push(["Revenue growth", `${fmt(b.revenue_growth_pct)}%`]);
  if (b.profit_margin_pct != null)
    facts.push(["Margin", `${fmt(b.profit_margin_pct)}%`]);
  if (b.trailing_pe != null) facts.push(["P/E", fmt(b.trailing_pe)]);
  if (b.beta != null) facts.push(["Beta", fmt(b.beta)]);
  if (b.week52_low != null && b.week52_high != null)
    facts.push(["52w", `$${fmt(b.week52_low)}–$${fmt(b.week52_high)}`]);
  // A company that reported three days ago and one reporting next week both
  // have "an earnings date" — say which this is rather than calling a date in
  // the past "next earnings". A result just in is usually why it's trending.
  const e = pick.earnings;
  if (e && e.date) {
    facts.push([e.reported ? "Reported" : "Next earnings", fmtEarnings(e.date)]);
  }

  const factLine = facts.length
    ? `<div class="news-facts">${facts
        .map(
          ([k, v]) =>
            `<span><span class="news-fact-key">${escapeHtml(k)}</span>
             ${escapeHtml(String(v))}</span>`
        )
        .join("")}</div>`
    : "";

  if (!b.business_summary) {
    return factLine || `<p class="news-about">No company profile available.</p>`;
  }
  return `${factLine}
    <p class="news-about clamped" data-about="${ticker}">${escapeHtml(
    b.business_summary
  )}</p>
    <button type="button" class="news-more" data-more="${ticker}">Read more</button>`;
}

// The agents' call. Same score pill, meter and per-agent chips as the advisor
// column, because it is literally the same scoring run — then a Details button
// onto the same five-argument breakdown.
function suggestionBlock(pick, suggestion, agents, ticker) {
  if (!suggestion) {
    return `<span class="news-label">What the agents make of it</span>
      <p class="news-about">Not scored yet.</p>
      ${wishlistAction(ticker)}`;
  }
  const band = bandLabel(suggestion);
  return `<span class="news-label">What the agents make of it</span>
    <div class="news-verdict">
      <span class="ai-action ${band.cls}">${escapeHtml(band.text)}</span>
      <span class="ai-horizon">${fmtHorizon(suggestion)}</span>
    </div>
    ${scoreMeter(scoreOf(suggestion), suggestion.action)}
    <div class="ai-row-sub">${miniScores(suggestion, agents)}</div>
    ${
      suggestion.headline
        ? `<span class="ai-line">${escapeHtml(suggestion.headline)}</span>`
        : ""
    }
    ${wishlistAction(ticker, true)}`;
}

function wishlistAction(ticker, withDetails = false) {
  return `<div class="news-actions">
    ${
      withDetails
        ? `<button type="button" data-news-details="${ticker}"
             title="The five agents' full reasoning for ${ticker}">Why? →</button>`
        : ""
    }
    <button type="button" data-news-wishlist="${ticker}"
      title="Add ${ticker} to your wishlist">＋ Wishlist</button>
    <span class="message" data-news-msg="${ticker}"></span>
  </div>`;
}

// Details: the same card the AI Advisor renders, so a discovered stock's
// argument is presented exactly like a holding's.
function renderNewsDetails() {
  if (!newsDetailsList) return;
  const suggestion = newsDetailTicker ? suggestionFor(newsDetailTicker) : null;
  if (!suggestion) {
    newsDetailsList.innerHTML = `<p class="ai-empty">No details.</p>`;
    return;
  }
  newsDetailsList.innerHTML = detailCard(suggestion, "wishlist");
}

function enterNewsDetails(ticker) {
  newsDetailTicker = ticker;
  renderNewsDetails();
  newsSummaryView.hidden = true;
  newsDetailsView.hidden = false;
  newsDetailsView.scrollIntoView({ behavior: "smooth", block: "start" });
}

function exitNewsDetails() {
  const from = newsDetailTicker;
  newsDetailTicker = null;
  newsDetailsView.hidden = true;
  newsSummaryView.hidden = false;
  const card =
    from && newsListEl.querySelector(`.news-pick[data-ticker="${from}"]`);
  if (card) card.scrollIntoView({ block: "center" });
}

// Pick actions — delegated, so they survive every re-render.
newsListEl.addEventListener("click", async (e) => {
  const moreBtn = e.target.closest("[data-more]");
  if (moreBtn) {
    const ticker = moreBtn.getAttribute("data-more");
    const para = newsListEl.querySelector(`[data-about="${ticker}"]`);
    if (para) {
      const clamped = para.classList.toggle("clamped");
      moreBtn.textContent = clamped ? "Read more" : "Show less";
    }
    return;
  }

  const detailsBtn = e.target.closest("[data-news-details]");
  if (detailsBtn) {
    enterNewsDetails(detailsBtn.getAttribute("data-news-details"));
    return;
  }

  // Add to wishlist: the natural next step once a pick looks interesting, and
  // it also takes the stock out of future discover runs — the backend excludes
  // anything you watch, so a pick you act on won't come back tomorrow.
  const addBtn = e.target.closest("[data-news-wishlist]");
  if (!addBtn) return;
  const ticker = addBtn.getAttribute("data-news-wishlist");
  const msg = newsListEl.querySelector(`[data-news-msg="${ticker}"]`);
  addBtn.disabled = true;
  try {
    const res = await fetch(WISHLIST_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (msg) setMessage(msg, data.error || "Could not add.", "err");
      addBtn.disabled = false;
      return;
    }
    if (msg) setMessage(msg, "Added to wishlist.", "ok");
    loadWishlist();
  } catch {
    if (msg) setMessage(msg, "Could not reach the API.", "err");
    addBtn.disabled = false;
  }
});

newsBackBtn.addEventListener("click", exitNewsDetails);

newsRefreshBtn.addEventListener("click", async () => {
  try {
    const res = await fetch(`${DISCOVER_API}/refresh`, { method: "POST" });
    const data = await res.json();
    if (data.started) {
      newsStatusEl.className = "ai-status";
      newsStatusEl.textContent = "Reading the room… finding what's trending.";
      newsRefreshBtn.disabled = true;
      startDiscoverPoll();
    } else {
      loadDiscover();
    }
  } catch {
    newsStatusEl.textContent = "Could not reach the discover API.";
    newsStatusEl.className = "ai-status err";
  }
});

function startDiscoverPoll() {
  if (discoverPollTimer) return;
  discoverPollTimer = setInterval(loadDiscover, 5000);
}
function stopDiscoverPoll() {
  if (discoverPollTimer) {
    clearInterval(discoverPollTimer);
    discoverPollTimer = null;
  }
}

// Pick up the daily opening-bell refresh without a reload, on the advisor's
// clock — the two panels regenerate at the same bell.
setInterval(loadDiscover, 120000);

// A big currency figure in the shortest honest form: $1.24T, $890.5B, $12.3M.
function fmtBig(n) {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  const abs = Math.abs(v);
  const units = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ];
  for (const [size, suffix] of units) {
    if (abs >= size) {
      return `$${(v / size).toFixed(abs / size >= 100 ? 0 : 1)}${suffix}`;
    }
  }
  return `$${fmt(v)}`;
}

// --- Portfolios (switch / create / rename / delete) -------------------
//
// Each portfolio is a separate workspace on the server; the active one is
// remembered server-side, so the app reopens on whichever you last used. All we
// do here is drive the dropdown and reload every panel when the active
// portfolio changes.

const PORTFOLIOS_API = "/api/portfolios";

const portfolioSelect = document.getElementById("portfolio-select");
const portfolioNewBtn = document.getElementById("portfolio-new");
const portfolioRenameBtn = document.getElementById("portfolio-rename");
const portfolioDeleteBtn = document.getElementById("portfolio-delete");
const portfolioMsg = document.getElementById("portfolio-message");

let portfolioState = null; // { active, portfolios: [{id, name, active}] }

async function loadPortfolios() {
  try {
    const res = await fetch(PORTFOLIOS_API);
    portfolioState = await res.json();
    renderPortfolios();
  } catch {
    setMessage(portfolioMsg, "Could not load portfolios.", "err");
  }
}

function renderPortfolios() {
  const list = (portfolioState && portfolioState.portfolios) || [];
  const active = portfolioState && portfolioState.active;
  portfolioSelect.innerHTML = list
    .map(
      (p) =>
        `<option value="${escapeHtml(p.id)}"${
          p.id === active ? " selected" : ""
        }>${escapeHtml(p.name)}</option>`
    )
    .join("");
  // Can't delete your only portfolio — disable the button to make that clear.
  portfolioDeleteBtn.disabled = list.length <= 1;

  // The AI risk toggle is per-portfolio; now that the active portfolio (and its
  // saved risk) is known, refresh the panels that key off it so they show the
  // right profile — covers the case where their data loaded before the
  // portfolio list did.
  if (aiData) renderAI();
  if (discoverData) renderDiscover();
}

// Return the entry for the currently active portfolio (for prefilling rename).
function activePortfolio() {
  const list = (portfolioState && portfolioState.portfolios) || [];
  return list.find((p) => p.id === portfolioState.active) || null;
}

// Reload every panel — used after the active portfolio changes so the whole UI
// reflects the newly active workspace. Clear any per-stock filter so the new
// portfolio's suggestions start at the summary.
function reloadAllPanels() {
  aiDetailFilter = null;
  // Agent weights are per-portfolio, so an unapplied draft belongs to the
  // portfolio being left behind.
  weightDraft = null;
  setMessage(aiWeightsMsg, "", "");
  if (aiDetailsView) aiDetailsView.hidden = true;
  if (aiSummaryView) aiSummaryView.hidden = false;
  // The news picks are per-portfolio too — what counts as a discovery depends
  // on what that portfolio already holds and watches.
  newsDetailTicker = null;
  if (newsDetailsView) newsDetailsView.hidden = true;
  if (newsSummaryView) newsSummaryView.hidden = false;
  loadSummary();
  loadStocks();
  loadWishlist();
  loadAI();
  loadDiscover();
}

// Small POST helper for the portfolio endpoints: sends JSON, applies the
// returned state, and reports errors. Returns true on success.
async function portfolioRequest(url, body, method = "POST") {
  try {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      setMessage(portfolioMsg, data.error || "Something went wrong.", "err");
      return false;
    }
    if (data.state) {
      portfolioState = data.state;
      renderPortfolios();
    }
    return true;
  } catch {
    setMessage(portfolioMsg, "Could not reach the API.", "err");
    return false;
  }
}

// Switch when the dropdown changes.
portfolioSelect.addEventListener("change", async () => {
  setMessage(portfolioMsg, "", "");
  const id = portfolioSelect.value;
  if (await portfolioRequest(`${PORTFOLIOS_API}/switch`, { id })) {
    const p = activePortfolio();
    setMessage(portfolioMsg, `Switched to ${p ? p.name : "portfolio"}.`, "ok");
    reloadAllPanels();
  } else {
    renderPortfolios(); // revert the <select> to the real active portfolio
  }
});

// Create a new portfolio (the server switches to it).
portfolioNewBtn.addEventListener("click", async () => {
  setMessage(portfolioMsg, "", "");
  const name = (prompt("Name your new portfolio:", "") || "").trim();
  if (!name) return;
  if (await portfolioRequest(PORTFOLIOS_API, { name })) {
    setMessage(portfolioMsg, `Created and switched to ${name}.`, "ok");
    reloadAllPanels();
  }
});

// Rename the active portfolio.
portfolioRenameBtn.addEventListener("click", async () => {
  setMessage(portfolioMsg, "", "");
  const current = activePortfolio();
  if (!current) return;
  const name = (prompt("Rename this portfolio:", current.name) || "").trim();
  if (!name || name === current.name) return;
  if (await portfolioRequest(`${PORTFOLIOS_API}/rename`, { id: current.id, name })) {
    setMessage(portfolioMsg, `Renamed to ${name}.`, "ok");
  }
});

// Delete the active portfolio (archived server-side, then the app moves to
// another portfolio).
portfolioDeleteBtn.addEventListener("click", async () => {
  setMessage(portfolioMsg, "", "");
  const current = activePortfolio();
  if (!current) return;
  if (
    !confirm(
      `Delete the "${current.name}" portfolio? Its holdings, wishlist, and ` +
        `history will be removed from the app.`
    )
  ) {
    return;
  }
  if (
    await portfolioRequest(PORTFOLIOS_API, { id: current.id }, "DELETE")
  ) {
    const p = activePortfolio();
    setMessage(
      portfolioMsg,
      `Deleted "${current.name}". Now on ${p ? p.name : "another portfolio"}.`,
      "ok"
    );
    reloadAllPanels();
  }
});

// initial load
loadPortfolios();
loadSummary();
loadStocks();
loadWishlist();
loadAI();
loadDiscover();
