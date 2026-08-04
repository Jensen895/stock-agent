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
    tbody.innerHTML = `<tr><td colspan="8" class="empty">Could not reach the API.</td></tr>`;
  }
}

function renderStocks(stocks) {
  if (!stocks.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">No stocks yet. Buy one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = stocks
    .map((s) => {
      const ticker = escapeHtml(s.ticker);
      const price = s.price == null ? "—" : `$${fmt(s.price)}`;
      return `<tr>
        <td>${ticker}</td>
        <td class="num">${fmt(s.shares)}</td>
        <td class="num">$${fmt(s.avg_price)}</td>
        <td class="num">${price}</td>
        ${gainCell(s.today)}
        ${gainCell(s.total)}
        <td class="num earnings">${fmtEarnings(s.earnings_date)}</td>
        <td class="actions-col">
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
const wishlistBody = document.getElementById("wishlist-body");

async function loadWishlist() {
  try {
    const res = await fetch(WISHLIST_API);
    const data = await res.json();
    renderWishlist(data.wishlist || []);
  } catch {
    wishlistBody.innerHTML = `<tr><td colspan="6" class="empty">Could not reach the API.</td></tr>`;
  }
}

function renderWishlist(items) {
  if (!items.length) {
    wishlistBody.innerHTML = `<tr><td colspan="6" class="empty">Nothing on your wishlist yet.</td></tr>`;
    return;
  }
  wishlistBody.innerHTML = items
    .map((w) => {
      const ticker = escapeHtml(w.ticker);
      const open = w.open == null ? "—" : `$${fmt(w.open)}`;
      const price = w.price == null ? "—" : `$${fmt(w.price)}`;
      // Change vs. the open price: green above the open, red below.
      const change = gainCell(
        w.change == null ? null : { value: w.change, pct: w.change_pct }
      );
      return `<tr>
        <td>${ticker}</td>
        <td class="num">${open}</td>
        <td class="num">${price}</td>
        ${change}
        <td class="num earnings">${fmtEarnings(w.earnings_date)}</td>
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
    document.getElementById("ticker").value = buyBtn.getAttribute("data-buy");
    setMessage(buyMsg, "", "");
    buyForm.scrollIntoView({ behavior: "smooth", block: "center" });
    document.getElementById("shares").focus();
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
const aiSeeDetailsBtn = document.getElementById("ai-see-details");
const aiDetailsView = document.getElementById("ai-details-view");
const aiDetailsList = document.getElementById("ai-details-list");
const aiBackBtn = document.getElementById("ai-back");
const aiRefreshBtn = document.getElementById("ai-refresh");
const riskToggleEl = document.getElementById("risk-toggle");

const AI_RISK_KEY = "stockagent.aiRisk";
const DEFAULT_RISK = "low";

let aiData = null;
let aiPollTimer = null; // fast poll while a refresh is in flight

// Map the model's action to a display label + color class.
const AI_ACTIONS = {
  buy: { label: "Buy more", cls: "buy" },
  hold: { label: "Hold", cls: "hold" },
  trim: { label: "Sell part", cls: "trim" },
  sell: { label: "Sell all", cls: "sell" },
};

function getRisk() {
  try {
    return localStorage.getItem(AI_RISK_KEY) || DEFAULT_RISK;
  } catch {
    return DEFAULT_RISK;
  }
}
function setRisk(risk) {
  try {
    localStorage.setItem(AI_RISK_KEY, risk);
  } catch {
    /* private mode — selection just won't persist */
  }
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

  // Status line + last-updated stamp.
  setAiStatus();
  aiUpdatedEl.textContent = aiData.generated_at
    ? `Updated ${fmtUpdated(aiData.generated_at)}`
    : "—";

  const profile =
    (aiData.risk_profiles && aiData.risk_profiles[risk]) || null;
  renderAiSummary(profile);
  renderAiDetails(profile);

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
    // Two models cross-check each other; say how much they agreed, and warn if
    // one of them dropped out so a lone opinion isn't mistaken for a consensus.
    const profile = aiData.risk_profiles[getRisk()];
    const bits = [];
    const ag = profile && profile.agreement;
    if (ag && ag.total && (profile.models || []).length > 1) {
      bits.push(
        `${profile.models.length} models · ${ag.agreed}/${ag.total} agreed` +
          (ag.split ? ` · ${ag.split} split` : ""),
      );
    }
    if (aiData.model_errors && aiData.model_errors.length) {
      bits.push(`${aiData.model_errors.length} model call(s) failed`);
      aiStatusEl.className = "ai-status warn";
    }
    aiStatusEl.textContent = bits.join(" — ");
  }
  aiRefreshBtn.disabled = !aiData.configured || aiData.refreshing;
}

// Summary: bullet per stock — ticker, action badge, horizon, one-line headline.
function renderAiSummary(profile) {
  const suggestions = (profile && profile.suggestions) || [];
  if (!suggestions.length) {
    aiSummaryList.innerHTML = `<li class="ai-empty">No suggestions yet.</li>`;
    aiPortfolioNote.textContent = "";
    aiSeeDetailsBtn.hidden = true;
    return;
  }
  aiSummaryList.innerHTML = suggestions
    .map((s) => {
      const a = AI_ACTIONS[s.action] || AI_ACTIONS.hold;
      return `<li>
        <span class="ai-ticker">${escapeHtml(s.ticker)}</span>
        <span class="ai-action ${a.cls}">${a.label}</span>
        ${consensusChip(s)}
        <span class="ai-horizon">${fmtHorizon(s.horizon_days)}</span>
        ${s.headline ? `<span class="ai-line">${escapeHtml(s.headline)}</span>` : ""}
      </li>`;
    })
    .join("");
  aiPortfolioNote.textContent = (profile && profile.portfolio_note) || "";
  aiSeeDetailsBtn.hidden = false;
}

// A small badge showing whether the models agreed on this call. Only shown when
// there were actually two opinions to compare.
function consensusChip(s) {
  if (s.consensus === "agree") {
    return `<span class="ai-consensus agree" title="Both models chose this action">Both agree</span>`;
  }
  if (s.consensus === "split") {
    const other = (s.votes || []).find((v) => v.action !== s.action);
    const alt = other ? (AI_ACTIONS[other.action] || AI_ACTIONS.hold).label : "";
    return `<span class="ai-consensus split" title="The models disagreed — see details">Split${
      alt ? ` · other says ${escapeHtml(alt)}` : ""
    }</span>`;
  }
  return "";
}

// Per-model breakdown, shown on the detail card when opinions were collected.
function votesBlock(s) {
  const votes = s.votes || [];
  if (votes.length < 2) return "";
  const rows = votes
    .map((v) => {
      const a = AI_ACTIONS[v.action] || AI_ACTIONS.hold;
      return `<li>
        <span class="ai-vote-model">${escapeHtml(v.model)}</span>
        <span class="ai-action ${a.cls}">${a.label}</span>
        <span class="ai-horizon">${fmtHorizon(v.horizon_days)}</span>
        ${v.headline ? `<span class="ai-line">${escapeHtml(v.headline)}</span>` : ""}
      </li>`;
    })
    .join("");
  return `<span class="ai-field-label">What each model said</span>
          <ul class="ai-votes">${rows}</ul>`;
}

// Details: one card per stock with the ~10-line reasoning, trigger, and risks.
function renderAiDetails(profile) {
  const suggestions = (profile && profile.suggestions) || [];
  if (!suggestions.length) {
    aiDetailsList.innerHTML = `<p class="ai-empty">No details yet.</p>`;
    return;
  }
  aiDetailsList.innerHTML = suggestions
    .map((s) => {
      const a = AI_ACTIONS[s.action] || AI_ACTIONS.hold;
      const trigger = s.price_trigger
        ? `<span class="ai-field-label">Price trigger</span>
           <p class="ai-trigger">${escapeHtml(s.price_trigger)}</p>`
        : "";
      const risks = s.risks
        ? `<span class="ai-field-label">Main risk</span>
           <p>${escapeHtml(s.risks)}</p>`
        : "";
      return `<div class="ai-detail">
        <div class="ai-detail-head">
          <span class="ai-ticker">${escapeHtml(s.ticker)}</span>
          <span class="ai-action ${a.cls}">${a.label}</span>
          ${consensusChip(s)}
          <span class="ai-horizon">${fmtHorizon(s.horizon_days)}</span>
        </div>
        <p class="ai-reason">${escapeHtml(s.reasoning)}</p>
        ${trigger}
        ${risks}
        ${votesBlock(s)}
      </div>`;
    })
    .join("");
}

// "in how many days" — capped at a week by the backend.
function fmtHorizon(days) {
  const d = Number(days) || 1;
  if (d >= 7) return "within a week";
  if (d === 1) return "within 1 day";
  return `within ${d} days`;
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

// Risk toggle — re-render from the already-loaded data (no new API call).
riskToggleEl.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-risk]");
  if (!btn) return;
  setRisk(btn.getAttribute("data-risk"));
  renderAI();
});

// "See details" / "Back" switch the tab within the AI panel.
aiSeeDetailsBtn.addEventListener("click", () => {
  aiSummaryView.hidden = true;
  aiDetailsView.hidden = false;
});
aiBackBtn.addEventListener("click", () => {
  aiDetailsView.hidden = true;
  aiSummaryView.hidden = false;
});

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

// Pick up scheduled (every-2h) refreshes without a reload.
setInterval(loadAI, 120000);

// initial load
loadSummary();
loadStocks();
loadWishlist();
loadAI();
