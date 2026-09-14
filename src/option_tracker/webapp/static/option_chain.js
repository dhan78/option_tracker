"use strict";

Highcharts.setOptions({ chart: { animation: false, resetZoomButton: { position: { align: "left", verticalAlign: "bottom", x: 10, y: -8 }, relativeTo: "plotBox" } }, plotOptions: { series: { animation: false } }, tooltip: { valueDecimals: 2 }, xAxis: { title: { text: null } }, yAxis: { title: { text: null } } });

const REFRESH_MS = 30000; // polling fallback only; primary transport is WebSocket
const state = {
  charts: new Map(), byExpiry: new Map(), leftMax: { oi: null, vol: null },
  heatmap: null, smile: null, current: null, polling: false, ivCache: new Map(), highlight: null, spot: null, gex: null, gexCurve: null, charm: null, blendK: 0.3, pinHistory: [], pinChart: null,
};

function maxOf(arrays) {
  let m = 0;
  arrays.forEach((a) => a.forEach((v) => { if (v != null && v > m) m = v; }));
  return m;
}

// Shared scale for the stacked OI+Volume bars: max stacked height (OI+Vol) × 1.1.
function computeLeftMax(payload) {
  let m = 0;
  payload.expiries.forEach((e) => {
    e.strikes.forEach((s, i) => {
      const c = (e.c_oi[i] || 0) + (e.c_vol[i] || 0);
      const p = (e.p_oi[i] || 0) + (e.p_vol[i] || 0);
      if (c > m) m = c;
      if (p > m) m = p;
    });
  });
  state.leftMax = { total: m * 1.1 || null };
}

// Average call/put IV at strike index i (used by the surface and smile).
function ivValue(exp, i) {
  const c = exp.c_iv[i], p = exp.p_iv[i];
  if (c != null && p != null) return (c + p) / 2;
  return c != null ? c : (p != null ? p : null);
}

// Fill null IVs by linear interpolation across strikes; flat-extrapolate the edges.
function interpolateIV(xs, ys) {
  const out = ys.slice();
  const known = [];
  for (let i = 0; i < out.length; i++) if (out[i] != null) known.push(i);
  if (!known.length) return out;
  for (let i = 0; i < known[0]; i++) out[i] = out[known[0]];
  for (let i = known[known.length - 1] + 1; i < out.length; i++) out[i] = out[known[known.length - 1]];
  for (let k = 0; k < known.length - 1; k++) {
    const a = known[k], b = known[k + 1];
    if (b - a <= 1) continue;
    const xa = xs[a], xb = xs[b], ya = out[a], yb = out[b];
    for (let i = a + 1; i < b; i++) {
      const t = xb === xa ? 0 : (xs[i] - xa) / (xb - xa);
      out[i] = ya + (yb - ya) * t;
    }
  }
  return out;
}

const fmt = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const fmtInt = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString());

function zip(xs, ys) {
  const out = [];
  for (let i = 0; i < xs.length; i++) {
    if (xs[i] === null) continue;
    out.push([xs[i], ys && ys[i] !== undefined ? ys[i] : null]);
  }
  return out;
}

function priceLines(exp) {
  const lines = [];
  if (window.__lastPrice != null) {
    lines.push({
      value: window.__lastPrice, color: "#000", dashStyle: "Dash", width: 1, zIndex: 4,
      label: { text: `$${fmt(window.__lastPrice)}`, rotation: 0, y: -2, style: { color: "#000", fontSize: "11px", fontWeight: "bold", textOutline: "2px #fff" } },
    });
  }
  return lines;
}

function sigmaLines(exp) {
  const lines = priceLines(exp);
  if (exp.upper_2sigma != null) {
    lines.push({
      value: exp.upper_2sigma, color: "orange", dashStyle: "Dash", width: 1, zIndex: 4,
      label: { text: `+2σ $${fmt(exp.upper_2sigma)}`, rotation: 0, y: -2, style: { color: "#000", fontSize: "11px", fontWeight: "bold", textOutline: "2px #fff" } },
    });
  }
  if (exp.lower_2sigma != null) {
    lines.push({
      value: exp.lower_2sigma, color: "orange", dashStyle: "Dash", width: 1, zIndex: 4,
      label: { text: `-2σ $${fmt(exp.lower_2sigma)}`, rotation: 0, y: -2, style: { color: "#000", fontSize: "11px", fontWeight: "bold", textOutline: "2px #fff" } },
    });
  }
  return lines;
}

// Blue highlight line for a strike selected from the IV surface.
function highlightPlotLine(strike) {
  return {
    id: "hl-strike", value: strike, color: "#2962ff", width: 2, zIndex: 6,
    label: { text: `${strike}`, rotation: 0, y: -2, style: { color: "#2962ff", fontWeight: "bold", fontSize: "10px", textOutline: "2px #fff" } },
  };
}

// Left-chart x-axis plot lines (spot + 2σ) plus the active surface highlight.
function leftPlotLines(exp) {
  const lines = sigmaLines(exp);
  if (state.highlight && state.highlight.expiry === exp.expiry) {
    lines.push(highlightPlotLine(state.highlight.strike));
  }
  return lines;
}

// Highlight a strike (clicked on the surface) in its expiry's OI/Volume chart.
// Clicking the same strike again toggles it off.
function highlightStrike(expiry, strike) {
  if (state.highlight && state.highlight.expiry === expiry && state.highlight.strike === strike) {
    clearHighlight();
    return;
  }
  state.highlight = { expiry, strike };
  const pair = state.charts.get(expiry);
  const exp = state.byExpiry.get(expiry);
  if (!pair || !exp) return;
  pair.left.xAxis[0].update({ plotLines: leftPlotLines(exp) }, true);
  if (pair.left.renderTo) pair.left.renderTo.scrollIntoView({ behavior: "smooth", block: "center" });
}

function clearHighlight() {
  const h = state.highlight;
  state.highlight = null;
  if (!h) return;
  const pair = state.charts.get(h.expiry);
  const exp = state.byExpiry.get(h.expiry);
  if (pair && exp) pair.left.xAxis[0].update({ plotLines: leftPlotLines(exp) }, true);
}

function pointClick(expiryLabel) {
  return function () {
    const exp = state.byExpiry.get(expiryLabel);
    if (!exp) return;
    const idx = exp.strikes.indexOf(this.x);
    const detail = document.getElementById("detail");
    if (!detail) return;
    if (idx < 0) {
      detail.textContent = `${expiryLabel} — strike $${fmt(this.x)} : ${this.series.name} = ${fmt(this.y)}`;
      return;
    }
    detail.innerHTML =
      `<b>${expiryLabel} · Strike $${fmt(exp.strikes[idx], 1)}</b> &nbsp; ` +
      `Call ${fmt(exp.c_last[idx])} (prev ${fmt(exp.c_prev[idx])}) · Put ${fmt(exp.p_last[idx])} (prev ${fmt(exp.p_prev[idx])}) &nbsp;|&nbsp; ` +
      `IV C ${fmt(exp.c_iv[idx], 1)}% / P ${fmt(exp.p_iv[idx], 1)}% &nbsp;|&nbsp; ` +
      `OI C ${fmtInt(exp.c_oi[idx])} / P ${fmtInt(exp.p_oi[idx])} &nbsp;|&nbsp; ` +
      `Vol C ${fmtInt(exp.c_vol[idx])} / P ${fmtInt(exp.p_vol[idx])}`;
  };
}

// x-axis config that prints every strike tick (dtick 2.5, angled -60) without skipping.
function strikeAxis(plotLines) {
  return {
    crosshair: true,
    plotLines: plotLines,
    tickInterval: 2.5,
    startOnTick: false,
    endOnTick: false,
    labels: { step: 1, rotation: -60, style: { fontSize: "9px" } },
  };
}

// The "limited" (default) y-axis maxima per chart kind.
function limitedMaxes(kind) {
  if (kind === "left") return [state.leftMax.total];
  return [null]; // right: price autoscales
}

// Double-click toggles: 1st = full auto-scale + all traces; 2nd = limited 1.1× scale.
function attachReset(chart, kind) {
  let expanded = false;
  const reset = () => {
    chart.series.forEach((s) => s.setVisible(true, false));
    if (kind === "left") clearHighlight();
    expanded = !expanded;
    const maxes = expanded ? chart.yAxis.map(() => null) : limitedMaxes(kind);
    chart.yAxis.forEach((ax, i) => ax.update({ min: 0, max: maxes[i] }, false));
    chart.xAxis.forEach((ax) => ax.setExtremes(null, null, false));
    chart.redraw();
    if (chart.resetZoomButton) chart.resetZoomButton = chart.resetZoomButton.destroy();
  };
  // zoomType can suppress the native dblclick, so also detect it from click timing.
  let lastClick = 0;
  const onClick = () => {
    const now = Date.now();
    if (now - lastClick < 400) { reset(); lastClick = 0; } else { lastClick = now; }
  };
  const node = chart.renderTo || chart.container;
  node.addEventListener("click", onClick);
  node.addEventListener("dblclick", reset);
}

function leftChart(el, exp) {
  return Highcharts.chart(el, {
    chart: { zoomType: "x", spacingTop: 24, events: { load() { attachReset(this, "left"); } } },
    title: { text: exp.expiry, align: "left", style: { fontSize: "13px" } },
    credits: { enabled: false },
    legend: { enabled: false },
    xAxis: strikeAxis(leftPlotLines(exp)),
    yAxis: [
      { min: 0, max: state.leftMax.total, title: { text: "Proj OI", align: "high", offset: 0, rotation: 0, x: 8, y: 12, textAlign: "left", style: { fontSize: "10px", fontWeight: "bold", color: "#555" } } },
    ],
    tooltip: {
      shared: true,
      useHTML: true,
      formatter() {
        const v = {};
        this.points.forEach((p) => { v[p.series.name] = p.y; });
        const n = (x) => (x == null ? "—" : Highcharts.numberFormat(x, 0));
        const cProj = (v["Call OI"] || 0) + (v["Call Vol"] || 0);
        const pProj = (v["Put OI"] || 0) + (v["Put Vol"] || 0);
        return (
          `<b>Strike $${fmt(this.x, 1)}</b>` +
          `<table style="margin-top:3px;border-collapse:collapse">` +
          `<tr><td></td><th style="padding:0 6px"><span style="font-size:15px;color:rgba(0,0,0,0.45);vertical-align:middle">&#9679;</span> OI</th><th style="padding:0 6px">&#128202; Vol</th><th style="padding:0 6px">&#931; Proj</th></tr>` +
          `<tr><td style="color:rgb(0,128,0);font-weight:bold">Calls</td>` +
          `<td style="text-align:right;padding:0 6px">${n(v["Call OI"])}</td>` +
          `<td style="text-align:right;padding:0 6px">${n(v["Call Vol"])}</td>` +
          `<td style="text-align:right;padding:0 6px;font-weight:bold">${n(cProj)}</td></tr>` +
          `<tr><td style="color:rgb(210,0,0);font-weight:bold">Puts</td>` +
          `<td style="text-align:right;padding:0 6px">${n(v["Put OI"])}</td>` +
          `<td style="text-align:right;padding:0 6px">${n(v["Put Vol"])}</td>` +
          `<td style="text-align:right;padding:0 6px;font-weight:bold">${n(pProj)}</td></tr>` +
          `</table>` +
          `<div style="margin-top:3px;color:#888;font-size:10px">Proj OI = T-1 OI + today's Vol (assumes all volume opens)</div>`
        );
      },
    },
    plotOptions: {
      column: { stacking: "normal", borderWidth: 0, pointPadding: 0.03, groupPadding: 0.12 },
      series: { point: { events: { click: pointClick(exp.expiry) } } },
    },
    series: [
      { name: "Call OI", type: "column", stack: "call", color: "rgb(0,128,0)", data: zip(exp.strikes, exp.c_oi) },
      { name: "Call Vol", type: "column", stack: "call", color: "rgba(0,128,0,0.35)", data: zip(exp.strikes, exp.c_vol) },
      { name: "Put OI", type: "column", stack: "put", color: "rgb(210,0,0)", data: zip(exp.strikes, exp.p_oi) },
      { name: "Put Vol", type: "column", stack: "put", color: "rgba(210,0,0,0.35)", data: zip(exp.strikes, exp.p_vol) },
    ],
  });
}

function buildAll(payload) {
  const container = document.getElementById("charts");
  container.innerHTML = "";
  state.charts.clear();
  state.byExpiry.clear();
  window.__lastPrice = payload.lastSalePrice;
  computeLeftMax(payload);
  buildHeatmap(payload);
  buildSmile(payload);
  buildGex(payload);
  buildCharm(payload);
  if (state.blendK > 0) applyBlend();
  updateWheel(payload);

  payload.expiries.forEach((exp, i) => {
    state.byExpiry.set(exp.expiry, exp);
    const row = document.createElement("div");
    row.className = "expiry-row";
    const left = document.createElement("div");
    left.className = "chart-box";
    left.id = `left-${i}`;
    const ladder = document.createElement("div");
    ladder.className = "chart-box";
    ladder.id = `ladder-${i}`;
    row.appendChild(left);
    row.appendChild(ladder);
    container.appendChild(row);
    state.charts.set(exp.expiry, { left: leftChart(left, exp), ladder: ladderChart(ladder, exp, payload.wheel) });
  });
}

// ── IV surface (heatmap): strike × expiry, colour = IV ───────────────
// [strike, iv] pairs with gaps interpolated; carries forward the last good
// row when an expiry has no solvable IV at all this cycle.
function ivPairs(exp) {
  const iv = interpolateIV(exp.strikes, exp.strikes.map((s, i) => ivValue(exp, i)));
  const pairs = [];
  exp.strikes.forEach((s, i) => { if (s != null && iv[i] != null) pairs.push([s, iv[i]]); });
  if (pairs.length) { state.ivCache.set(exp.expiry, pairs); return pairs; }
  return state.ivCache.get(exp.expiry) || [];
}

function heatmapData(payload) {
  const rows = payload.expiries.map((e) => ivPairs(e));
  // A 0-DTE / illiquid expiry may have no solvable IV at all — borrow the
  // nearest populated expiry's row so it stays visible on the surface.
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].length) continue;
    for (let d = 1; d < rows.length; d++) {
      if (rows[i - d] && rows[i - d].length) { rows[i] = rows[i - d]; break; }
      if (rows[i + d] && rows[i + d].length) { rows[i] = rows[i + d]; break; }
    }
  }
  const data = [];
  rows.forEach((pairs, j) => pairs.forEach(([s, iv]) => data.push([s, j, Math.round(iv * 10) / 10])));
  return data;
}

// Spot-price vertical reference line for the IV surface x-axis.
function spotLine(price) {
  if (price == null) return [];
  return [{
    value: price, color: "#000", dashStyle: "Dash", width: 1.5, zIndex: 5,
    label: { text: `Spot $${fmt(price)}`, rotation: 0, style: { color: "#000", fontWeight: "bold", fontSize: "10px", textOutline: "2px #fff" } },
  }];
}

function buildHeatmap(payload) {
  if (state.heatmap) { state.heatmap.destroy(); state.heatmap = null; }
  state.heatmap = Highcharts.chart("iv-heatmap", {
    chart: { type: "heatmap", height: 260, spacingTop: 16, zoomType: "x" },
    title: { text: "Implied Volatility Surface (strike × expiry)", align: "left", style: { fontSize: "13px" } },
    credits: { enabled: false },
    xAxis: { tickInterval: 5, plotLines: spotLine(payload.lastSalePrice) },
    yAxis: { categories: payload.expiries.map((e) => e.expiry), title: null, reversed: true },
    colorAxis: { min: 20, max: 90, stops: [[0, "#3b4cc0"], [0.5, "#f7f7b0"], [1, "#b40426"]] },
    legend: { align: "right", layout: "vertical", verticalAlign: "top", symbolHeight: 180 },
    tooltip: {
      formatter() {
        const cats = this.series.chart.yAxis[0].categories;
        return `Strike $${this.point.x} · ${cats[this.point.y]}<br/>IV <b>${this.point.value}%</b>`;
      },
    },
    series: [{
      name: "IV", borderWidth: 0, colsize: 2.5, data: heatmapData(payload),
      point: { events: { click() { highlightStrike(this.series.chart.yAxis[0].categories[this.y], this.x); } } },
    }],
  });
}

// Heatmap setData can drop rows on update, so rebuild the (small) surface each time.
function updateHeatmap(payload) {
  buildHeatmap(payload);
}

// ── IV smile / skew on a moneyness axis (K ÷ S) ──────────────────────
function smileSeries(payload) {
  const spot = payload.lastSalePrice;
  return payload.expiries.map((e) => ({
    name: e.expiry,
    data: ivPairs(e).map(([s, iv]) => (spot ? { x: s / spot, y: iv, strike: s } : null)).filter(Boolean),
  }));
}

function buildSmile(payload) {
  state.smile = Highcharts.chart("iv-smile", {
    chart: { type: "spline", height: 260, spacingTop: 16, zoomType: "xy" },
    title: { text: "IV Smile / Skew (moneyness K ÷ S)", align: "left", style: { fontSize: "13px" } },
    credits: { enabled: false },
    xAxis: {
      crosshair: true,
      plotLines: [{ value: 1, color: "#000", dashStyle: "Dash", width: 1, label: { text: "ATM", rotation: 0, style: { fontSize: "9px" } } }],
    },
    yAxis: { min: 0 },
    legend: { layout: "vertical", align: "right", verticalAlign: "middle", itemStyle: { fontSize: "10px" } },
    tooltip: {
      headerFormat: "",
      pointFormatter() { return `${this.series.name}<br/>Strike $${this.strike.toFixed(1)} · IV <b>${this.y.toFixed(1)}%</b>`; },
    },
    plotOptions: { series: { marker: { enabled: false } } },
    series: smileSeries(payload),
  });
}

function updateSmile(payload) {
  if (!state.smile || state.smile.series.length !== payload.expiries.length) { buildSmile(payload); return; }
  smileSeries(payload).forEach((s, i) => state.smile.series[i].setData(s.data, false));
  state.smile.redraw();
}

// ── Dealer gamma exposure (GEX) ──────────────────────────────────────
function gexBadge(gex) {
  const el = document.getElementById("gex-badge");
  if (!el) return;
  if (!gex) { el.textContent = ""; return; }
  const long = gex.regime === "long";
  el.className = "gex-badge " + (long ? "long" : "short");
  el.textContent = long
    ? `🟢 Long gamma · mean-reverting · ${fmt(gex.total_mm, 0)} $mm/1%`
    : `🔴 Short gamma · trending · ${fmt(gex.total_mm, 0)} $mm/1%`;
}

function emBadge() {
  const el = document.getElementById("em-badge");
  if (!el) return;
  const m = expectedMove();
  if (!m) { el.textContent = ""; return; }
  el.className = "em-badge";
  el.textContent = `↔ Exp move ±$${fmt(m.em, 1)} (${fmt(m.lo, 0)}–${fmt(m.hi, 0)})`;
}

function gexPlotLines(gex) {
  const lines = [];
  if (window.__lastPrice != null) lines.push({ value: window.__lastPrice, color: "#000", dashStyle: "Dash", width: 1.5, zIndex: 5, label: { text: `Spot $${fmt(window.__lastPrice)}`, rotation: 0, style: { color: "#000", fontWeight: "bold", fontSize: "10px", textOutline: "2px #fff" } } });
  if (gex.flip != null) lines.push({ value: gex.flip, color: "purple", width: 2, zIndex: 5, label: { text: `Flip $${fmt(gex.flip)}`, rotation: 0, y: 12, style: { color: "purple", fontWeight: "bold", fontSize: "10px", textOutline: "2px #fff" } } });
  if (gex.call_wall != null) lines.push({ value: gex.call_wall, color: "rgb(0,128,0)", dashStyle: "ShortDash", width: 1, zIndex: 4, label: { text: `Call wall $${fmt(gex.call_wall)}`, rotation: 0, y: 26, style: { color: "rgb(0,128,0)", fontSize: "9px", textOutline: "2px #fff" } } });
  if (gex.put_wall != null) lines.push({ value: gex.put_wall, color: "rgb(210,0,0)", dashStyle: "ShortDash", width: 1, zIndex: 4, label: { text: `Put wall $${fmt(gex.put_wall)}`, rotation: 0, y: 40, style: { color: "rgb(210,0,0)", fontSize: "9px", textOutline: "2px #fff" } } });
  return lines;
}

function gexData(gex) {
  return gex.strikes.map((s, i) => ({ x: s, y: gex.net_mm[i], color: gex.net_mm[i] >= 0 ? "rgb(0,128,0)" : "rgb(210,0,0)" }));
}

// Shared near-money x-domain so all three GEX charts line up vertically.
function gexDomain(gex) {
  const S = window.__lastPrice != null ? window.__lastPrice : (gex && gex.spot);
  if (S == null) return null;
  let lo = S * 0.92, hi = S * 1.08;
  if (gex && gex.put_wall != null) lo = Math.min(lo, gex.put_wall);
  if (gex && gex.call_wall != null) hi = Math.max(hi, gex.call_wall);
  const pad = (hi - lo) * 0.04;
  return [lo - pad, hi + pad];
}

// Expected 1-day move from the nearest expiry's ATM straddle (~0.85 x straddle).
function expectedMove() {
  const p = state.current;
  if (!p || !p.expiries || !p.expiries.length) return null;
  const S = p.lastSalePrice;
  const e = p.expiries[0];
  let bi = -1, bd = Infinity;
  (e.strikes || []).forEach((k, i) => { if (k == null) return; const d = Math.abs(k - S); if (d < bd) { bd = d; bi = i; } });
  if (bi < 0) return null;
  const c = e.c_last && e.c_last[bi], pu = e.p_last && e.p_last[bi];
  if (c == null || pu == null) return null;
  const em = (c + pu) * 0.85;
  return { em, lo: S - em, hi: S + em, expiry: e.expiry };
}

// Shaded expected-move band for the strike-axis GEX charts.
function emBands() {
  const m = expectedMove();
  if (!m) return [];
  return [{ from: m.lo, to: m.hi, color: "rgba(41,98,255,0.07)", zIndex: 5,
    label: { text: `Exp move ±$${fmt(m.em, 1)}`, align: "center", verticalAlign: "top", y: 8,
      style: { fontSize: "10px", color: "#2451c7", fontWeight: "bold", textOutline: "2px #fff" } } }];
}

// ── Pin-evolution tracker: session history of spot / flip / walls / total GEX ─
function recordPinPoint(gex) {
  if (!gex) return;
  const S = gex.spot != null ? gex.spot : window.__lastPrice;
  const h = state.pinHistory;
  const now = etAsUtcMs();
  if (h.length && now - h[h.length - 1].t < 3000) return; // dedupe rapid re-renders
  h.push({ t: now, spot: S, flip: gex.flip, put_wall: gex.put_wall, call_wall: gex.call_wall, total_mm: gex.total_mm });
  if (h.length > 2500) h.shift();
}

// ET wall-clock encoded as a UTC epoch — matches the spot chart / server timestamps.
function etAsUtcMs() {
  const p = {};
  new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
    .formatToParts(new Date()).forEach((x) => { p[x.type] = x.value; });
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
}

function pinData(key) {
  return state.pinHistory.filter((p) => p[key] != null).map((p) => [p.t, p[key]]);
}

// One-line live verdict of the current pin state for the chart title.
function pinSummary() {
  const h = state.pinHistory;
  if (!h.length) return "Pin evolution — accumulating…";
  const p = h[h.length - 1];
  const S = p.spot, tot = p.total_mm, flip = p.flip, pw = p.put_wall, cw = p.call_wall;
  let tag;
  if (tot == null) tag = "Pin evolution";
  else if (tot > 150) tag = `🔒 PINNED · long +${fmt(tot, 0)}mm`;
  else if (tot < -150) tag = `🔓 UNPINNED · short ${fmt(tot, 0)}mm`;
  else tag = `⚖️ KNIFE-EDGE · ${fmt(tot, 0)}mm`;
  const box = (pw != null && cw != null) ? ` · box ${fmt(pw, 0)}–${fmt(cw, 0)}` : "";
  let where = "";
  if (S != null) {
    if (cw != null && Math.abs(S - cw) <= 0.75) where = ` · spot ${fmt(S, 1)} on call wall`;
    else if (pw != null && Math.abs(S - pw) <= 0.75) where = ` · spot ${fmt(S, 1)} on put wall`;
    else where = ` · spot ${fmt(S, 1)}`;
  }
  let cush = "";
  if (flip != null && S != null) { const d = S - flip; cush = ` · flip ${fmt(flip, 0)} (${d >= 0 ? "+" : ""}${fmt(d, 1)})`; }
  let trend = "";
  if (h.length > 6) {
    const prev = h[Math.max(0, h.length - 11)].total_mm;
    if (prev != null && tot != null && Math.abs(tot - prev) > 50) trend = tot - prev > 0 ? " · tightening ↑" : " · loosening ↓";
  }
  return `${tag}${box}${where}${cush}${trend}`;
}

function buildPinHistory() {
  state.pinChart = Highcharts.chart("pin-history", {
    chart: { height: 240, spacingTop: 16, zoomType: "x" },
    title: { text: pinSummary(), align: "left", style: { fontSize: "12px" } },
    credits: { enabled: false },
    legend: { enabled: true, itemStyle: { fontSize: "9px" } },
    xAxis: { type: "datetime", crosshair: true },
    yAxis: [
      { title: { text: "Price" } },
      { title: { text: "Total GEX ($mm)" }, opposite: true, gridLineWidth: 0, plotLines: [{ value: 0, color: "#ccc", width: 1 }] },
    ],
    tooltip: {
      shared: true, useHTML: true,
      formatter() {
        // Swatch that mirrors each trace: filled block for the area, dashed/solid line for the rest.
        const swatch = (p) => {
          const c = p.color || p.series.color, o = p.series.userOptions || {};
          if (o.type === "area") return `<span style="display:inline-block;width:12px;height:9px;background:${c};vertical-align:middle;margin-right:4px"></span>`;
          const dashed = /dash|dot/i.test(o.dashStyle || "");
          return `<span style="display:inline-block;width:16px;border-top:3px ${dashed ? "dashed" : "solid"} ${c};vertical-align:middle;margin-right:4px"></span>`;
        };
        const pts = (this.points || []).slice();
        const gex = pts.find((p) => p.series.name === "Total GEX");
        const price = pts.filter((p) => p.series.name !== "Total GEX").sort((a, b) => b.y - a.y);
        let s = `<b>${Highcharts.dateFormat("%H:%M:%S", this.x)}</b><br/>`;
        price.forEach((p) => {
          s += `${swatch(p)} ${p.series.name}: <b>${Highcharts.numberFormat(p.y, 2)}</b><br/>`;
        });
        if (gex) {
          s += `<span style="color:#bbb">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</span><br/>`;
          const pos = gex.y >= 0;
          const bg = pos ? "#e7f6ec" : "#fdeaea", fg = pos ? "#1a7f37" : "#b40426";
          const tag = pos ? "long" : "short";
          s += `${swatch(gex)} Total GEX: <b style="background:${bg};color:${fg};padding:1px 6px;border-radius:4px">${Highcharts.numberFormat(gex.y, 0)} $mm ${tag}</b>`;
        }
        return s;
      },
    },
    plotOptions: { series: { lineWidth: 1.5, marker: { enabled: false } } },
    series: [
      { name: "Spot", color: "#000", zIndex: 5, dashStyle: "Dash", data: pinData("spot") },
      { name: "Flip", color: "purple", data: pinData("flip") },
      { name: "Call wall", color: "rgb(0,128,0)", step: "center", data: pinData("call_wall") },
      { name: "Put wall", color: "rgb(210,0,0)", step: "center", data: pinData("put_wall") },
      { name: "Total GEX", yAxis: 1, type: "area", color: "rgba(41,98,255,0.55)", negativeColor: "rgba(230,145,0,0.6)", fillOpacity: 0.08, threshold: 0, marker: { enabled: false }, data: pinData("total_mm") },
    ],
  });
}

function updatePinHistory() {
  if (!document.getElementById("pin-history")) return;
  if (!state.pinChart) { buildPinHistory(); return; }
  ["spot", "flip", "call_wall", "put_wall", "total_mm"].forEach((k, i) => state.pinChart.series[i].setData(pinData(k), false));
  state.pinChart.setTitle({ text: pinSummary() }, false, false);
  state.pinChart.redraw();
}

function buildGex(payload) {
  const gex = payload.gex;
  gexBadge(gex);
  emBadge();
  if (!gex) { if (state.gex) { state.gex.destroy(); state.gex = null; } return; }
  state.gex = Highcharts.chart("gex-chart", {
    chart: { type: "column", height: 300, spacingTop: 20, zoomType: "x" },
    title: { text: "Net Dealer Gamma by Strike ($mm per 1% move)", align: "left", style: { fontSize: "13px" } },
    credits: { enabled: false },
    legend: { enabled: false },
    xAxis: { title: { text: "Strike" }, crosshair: true, min: gexDomain(gex)?.[0], max: gexDomain(gex)?.[1], plotBands: emBands(), plotLines: gexPlotLines(gex) },
    yAxis: { title: { text: "GEX ($mm)" }, plotLines: [{ value: 0, color: "#888", width: 1, zIndex: 3 }] },
    tooltip: {
      formatter() {
        const side = this.y >= 0 ? "call-dominated (stabilizing)" : "put-dominated (destabilizing)";
        return `<b>Strike $${fmt(this.x, 1)}</b><br/>Net GEX <b>${fmt(this.y, 2)}</b> $mm/1%<br/><span style="font-size:10px;color:#666">${side}</span>`;
      },
    },
    plotOptions: { column: { borderWidth: 0, pointPadding: 0.02, groupPadding: 0.05 } },
    series: [{ name: "Net GEX", data: gexData(gex) }],
  });
  buildGexCurve(gex);
}

function updateGex(payload) {
  const gex = payload.gex;
  gexBadge(gex);
  emBadge();
  if (!gex) return;
  if (!state.gex) { buildGex(payload); return; }
  state.gex.series[0].setData(gexData(gex), false);
  state.gex.xAxis[0].update({ min: gexDomain(gex)?.[0], max: gexDomain(gex)?.[1], plotBands: emBands(), plotLines: gexPlotLines(gex) }, false);
  state.gex.redraw();
  updateGexCurve(gex);
}

// Vertical reference lines for the GEX-vs-spot curve (x-axis = hypothetical spot).
function gexCurvePlotLines(gex) {
  const lines = [];
  if (window.__lastPrice != null) lines.push({ value: window.__lastPrice, color: "#000", dashStyle: "Dash", width: 1.5, zIndex: 5, label: { text: `Spot $${fmt(window.__lastPrice)}`, rotation: 0, y: -2, style: { color: "#000", fontWeight: "bold", fontSize: "10px", textOutline: "2px #fff" } } });
  (gex.flips || []).forEach((f) => {
    const nearest = gex.flip != null && Math.abs(f - gex.flip) < 0.01;
    lines.push({ value: f, color: "purple", width: nearest ? 2 : 1, dashStyle: nearest ? "Solid" : "ShortDash", zIndex: 5, label: { text: `Flip $${fmt(f)}`, rotation: 0, y: 12, style: { color: "purple", fontWeight: "bold", fontSize: "10px", textOutline: "2px #fff" } } });
  });
  if (gex.call_wall != null) lines.push({ value: gex.call_wall, color: "rgb(0,128,0)", dashStyle: "ShortDash", width: 1, zIndex: 4, label: { text: `Call wall $${fmt(gex.call_wall)}`, rotation: 0, y: 26, style: { color: "rgb(0,128,0)", fontSize: "9px", textOutline: "2px #fff" } } });
  if (gex.put_wall != null) lines.push({ value: gex.put_wall, color: "rgb(210,0,0)", dashStyle: "ShortDash", width: 1, zIndex: 4, label: { text: `Put wall $${fmt(gex.put_wall)}`, rotation: 0, y: 40, style: { color: "rgb(210,0,0)", fontSize: "9px", textOutline: "2px #fff" } } });
  return lines;
}

function buildGexCurve(gex) {
  if (!gex || !gex.curve) { if (state.gexCurve) { state.gexCurve.destroy(); state.gexCurve = null; } return; }
  state.gexCurve = Highcharts.chart("gex-curve", {
    chart: { type: "areaspline", height: 240, spacingTop: 16, zoomType: "x" },
    title: { text: "Total GEX as spot moves (dome = long gamma · dip = short gamma)", align: "left", style: { fontSize: "12px" } },
    credits: { enabled: false },
    legend: { enabled: false },
    xAxis: { title: { text: "Hypothetical spot" }, crosshair: true, min: gexDomain(gex)?.[0], max: gexDomain(gex)?.[1], plotBands: emBands(), plotLines: gexCurvePlotLines(gex) },
    yAxis: { title: { text: "GEX ($mm)" }, plotLines: [{ value: 0, color: "#888", width: 1, zIndex: 3 }] },
    tooltip: {
      formatter() {
        return `Spot $${fmt(this.x, 1)}<br/>Total GEX <b>${fmt(this.y, 0)}</b> $mm/1%<br/><span style="font-size:10px;color:#666">${this.y >= 0 ? "long gamma (stabilizing)" : "short gamma (destabilizing)"}</span>`;
      },
    },
    plotOptions: { areaspline: { threshold: 0, color: "rgb(0,128,0)", negativeColor: "rgb(210,0,0)", fillOpacity: 0.22, lineWidth: 2, marker: { enabled: false } } },
    series: [{ name: "Total GEX", data: gex.curve }],
  });
}

function updateGexCurve(gex) {
  if (!gex || !gex.curve) return;
  if (!state.gexCurve) { buildGexCurve(gex); return; }
  state.gexCurve.series[0].setData(gex.curve, false);
  state.gexCurve.xAxis[0].update({ min: gexDomain(gex)?.[0], max: gexDomain(gex)?.[1], plotBands: emBands(), plotLines: gexCurvePlotLines(gex) }, false);
  state.gexCurve.redraw();
}

// ── Charm: into-close delta-decay hedge flow ────────────────────────
// Positive = dealers must BUY into the 4pm close (upward pin drift); negative
// = must SELL (downward). Per-strike bars show WHERE the mechanical pull sits.
function charmBadge(charm) {
  const el = document.getElementById("charm-badge");
  if (!el) return;
  if (!charm) { el.textContent = ""; el.className = "charm-badge"; return; }
  const up = charm.direction === "up";
  el.className = "charm-badge " + (up ? "up" : "down");
  el.textContent = `⏱ Charm ${fmt(charm.hours_to_close, 1)}h→close · ${up ? "+" : ""}${fmt(charm.flow_mm, 0)} $mm → pins ${up ? "↑" : "↓"}`;
}

function charmData(charm) {
  return charm.strikes.map((s, i) => ({ x: s, y: charm.flow_by_strike_mm[i], color: charm.flow_by_strike_mm[i] >= 0 ? "rgb(0,128,0)" : "rgb(210,0,0)" }));
}

function buildCharm(payload) {
  const charm = payload.charm;
  charmBadge(charm);
  if (!charm) { if (state.charm) { state.charm.destroy(); state.charm = null; } return; }
  state.charm = Highcharts.chart("charm-chart", {
    chart: { type: "column", height: 240, spacingTop: 16, zoomType: "x" },
    title: { text: "Into-close hedge flow by strike ($mm) — green = dealers buy (pull ↑) · red = sell (pull ↓)", align: "left", style: { fontSize: "12px" } },
    credits: { enabled: false },
    legend: { enabled: false },
    xAxis: { title: { text: "Strike" }, crosshair: true, min: gexDomain(payload.gex)?.[0], max: gexDomain(payload.gex)?.[1], plotLines: gexPlotLines(payload.gex || {}) },
    yAxis: { title: { text: "Hedge flow ($mm)" }, plotLines: [{ value: 0, color: "#888", width: 1, zIndex: 3 }] },
    tooltip: {
      formatter() {
        return `<b>Strike $${fmt(this.x, 1)}</b><br/>Hedge flow <b>${fmt(this.y, 2)}</b> $mm<br/><span style="font-size:10px;color:#666">${this.y >= 0 ? "dealers buy (upward pull)" : "dealers sell (downward pull)"}</span>`;
      },
    },
    plotOptions: { column: { borderWidth: 0, pointPadding: 0.02, groupPadding: 0.05 } },
    series: [{ name: "Charm flow", data: charmData(charm) }],
  });
}

function updateCharm(payload) {
  const charm = payload.charm;
  charmBadge(charm);
  if (!charm) { if (state.charm) { state.charm.destroy(); state.charm = null; } return; }
  if (!state.charm) { buildCharm(payload); return; }
  state.charm.series[0].setData(charmData(charm), false);
  state.charm.xAxis[0].update({ min: gexDomain(payload.gex)?.[0], max: gexDomain(payload.gex)?.[1], plotLines: gexPlotLines(payload.gex || {}) }, false);
  state.charm.redraw();
}

// ── Wheel scanner: sortable CSP / covered-call candidate table ───────
state.wheel = { rows: [], sortKey: "ann_roc", sortDir: -1, side: "all", minPop: 0, minDte: 1, maxDte: 60 };
const WHEEL_COLS = [
  ["side", "Side"], ["expiry", "Expiry"], ["strike", "Strike"], ["dte", "DTE"],
  ["delta", "Δ"], ["pop", "PoP%"], ["bid", "Bid"], ["ann_roc", "Ann ROC%"],
  ["breakeven", "Breakeven"], ["cushion", "Cushion%"],
];

function wheelFiltered() {
  const w = state.wheel;
  const rows = w.rows.filter((r) =>
    (w.side === "all" || r.side === w.side) &&
    r.pop >= w.minPop && r.dte >= w.minDte && r.dte <= w.maxDte);
  const k = w.sortKey, d = w.sortDir;
  rows.sort((a, b) => (a[k] < b[k] ? -d : a[k] > b[k] ? d : 0));
  return rows;
}

// Shade rows sitting near the standard 0.30 / 0.16 delta sell targets.
function wheelRowClass(r) {
  const base = r.side === "CSP" ? "w-csp" : "w-cc";
  if (r.delta >= 0.28 && r.delta <= 0.32) return base + " w-d30";
  if (r.delta >= 0.14 && r.delta <= 0.18) return base + " w-d16";
  return base;
}

function renderWheel() {
  const el = document.getElementById("wheel-table");
  if (!el) return;
  const rows = wheelFiltered();
  const cnt = document.getElementById("wheel-count");
  if (cnt) cnt.textContent = `${rows.length} candidates`;
  const w = state.wheel;
  const arrow = (k) => (k === w.sortKey ? (w.sortDir < 0 ? " ▾" : " ▴") : "");
  let h = '<table class="wheel"><thead><tr>';
  WHEEL_COLS.forEach(([k, l]) => { h += `<th data-k="${k}" class="w-sort">${l}${arrow(k)}</th>`; });
  h += "</tr></thead><tbody>";
  rows.forEach((r) => {
    h += `<tr class="${wheelRowClass(r)}">` +
      `<td>${r.side}</td><td>${r.expiry}</td><td>${fmt(r.strike, 1)}</td><td>${r.dte}</td>` +
      `<td>${fmt(r.delta, 2)}</td><td>${fmt(r.pop, 0)}</td><td>${fmt(r.bid, 2)}</td>` +
      `<td><b>${fmt(r.ann_roc, 0)}</b></td><td>${fmt(r.breakeven, 2)}</td>` +
      `<td>${fmt(r.cushion, 1)}${r.beyond_move ? ' <span class="w-out" title="beyond 2σ expected move">⚠</span>' : ""}</td></tr>`;
  });
  h += "</tbody></table>";
  el.innerHTML = h;
  el.querySelectorAll("th.w-sort").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.k;
      if (state.wheel.sortKey === k) state.wheel.sortDir *= -1;
      else { state.wheel.sortKey = k; state.wheel.sortDir = (k === "side" || k === "expiry") ? 1 : -1; }
      renderWheel();
    };
  });
}

// Per-expiry yield ladder: annualized ROC by strike with that expiry's spot + gamma walls.
function expiryLadderData(rows, side, key) {
  return rows.filter((r) => r.side === side)
    .map((r) => ({ x: r.strike, y: r[key], dte: r.dte, pop: r.pop, delta: r.delta }))
    .filter((p) => p.y != null)
    .sort((a, b) => a.x - b.x);
}

// bid = actionable yield (what you collect) · t-1 = yesterday's close (Last - Change).
function ladderSeries(rows) {
  const mk = (name, side, key, color, dash, lw, marker) => ({ name, type: "line", color, dashStyle: dash, lineWidth: lw, marker, data: expiryLadderData(rows, side, key) });
  return [
    mk("CSP bid", "CSP", "ann_roc", "rgb(210,0,0)", "Solid", 2, { enabled: true, radius: 4 }),
    mk("CSP t-1", "CSP", "ann_roc_prev", "rgba(210,0,0,0.55)", "Dot", 1, { enabled: false }),
    mk("CC bid", "CC", "ann_roc", "rgb(0,128,0)", "Solid", 2, { enabled: true, radius: 4 }),
    mk("CC t-1", "CC", "ann_roc_prev", "rgba(0,128,0,0.55)", "Dot", 1, { enabled: false }),
  ];
}

function expiryLadderPlotLines(exp) {
  const lines = [];
  if (window.__lastPrice != null) lines.push({ value: window.__lastPrice, color: "#000", dashStyle: "Dash", width: 1.5, zIndex: 5, label: { text: `Spot $${fmt(window.__lastPrice)}`, rotation: 0, y: -2, style: { fontWeight: "bold", fontSize: "9px", textOutline: "2px #fff" } } });
  if (exp.put_wall != null) lines.push({ value: exp.put_wall, color: "rgb(210,0,0)", dashStyle: "ShortDash", width: 1, zIndex: 4, label: { text: `put wall $${fmt(exp.put_wall)}`, rotation: 0, y: 12, style: { color: "rgb(210,0,0)", fontSize: "9px", textOutline: "2px #fff" } } });
  if (exp.call_wall != null) lines.push({ value: exp.call_wall, color: "rgb(0,128,0)", dashStyle: "ShortDash", width: 1, zIndex: 4, label: { text: `call wall $${fmt(exp.call_wall)}`, rotation: 0, y: 26, style: { color: "rgb(0,128,0)", fontSize: "9px", textOutline: "2px #fff" } } });
  return lines;
}

function ladderChart(el, exp, wheel) {
  const rows = (wheel || []).filter((r) => r.expiry === exp.expiry);
  return Highcharts.chart(el, {
    chart: { type: "line", spacingTop: 24, zoomType: "xy" },
    title: { text: exp.expiry + " \u00b7 Yield (ann ROC%) \u2014 bid vs t-1", align: "left", style: { fontSize: "13px" } },
    credits: { enabled: false },
    legend: { enabled: true, itemStyle: { fontSize: "9px" } },
    xAxis: { title: { text: null }, crosshair: true, plotLines: expiryLadderPlotLines(exp) },
    yAxis: { title: { text: "Ann ROC %" }, min: 0 },
    tooltip: {
      useHTML: true, headerFormat: "",
      pointFormatter() {
        return `<b>${this.series.name} $${fmt(this.x, 1)}</b> (${this.dte}d)<br/>ROC <b>${fmt(this.y, 0)}%</b><br/>PoP ${this.pop != null ? fmt(this.pop, 0) + "%" : "—"} · Δ ${this.delta != null ? fmt(this.delta, 2) : "—"}`;
      },
    },
    series: ladderSeries(rows),
  });
}

function updateExpiryLadder(exp, wheel) {
  const pair = state.charts.get(exp.expiry);
  if (!pair || !pair.ladder) return;
  const rows = (wheel || []).filter((r) => r.expiry === exp.expiry);
  ladderSeries(rows).forEach((cfg, i) => pair.ladder.series[i].setData(cfg.data, false));
  pair.ladder.xAxis[0].update({ plotLines: expiryLadderPlotLines(exp) }, false);
  pair.ladder.redraw();
}

function updateWheel(payload) {
  if (payload.wheel) state.wheel.rows = payload.wheel;
  renderWheel();
}

(function initWheelControls() {
  const bind = (id, key, num) => {
    const elc = document.getElementById(id);
    if (!elc) return;
    elc.addEventListener(num ? "input" : "change", () => {
      state.wheel[key] = num ? Number(elc.value) : elc.value;
      renderWheel();
    });
  };
  bind("w-side", "side", false);
  bind("w-pop", "minPop", true);
  bind("w-mindte", "minDte", true);
  bind("w-maxdte", "maxDte", true);
})();

// Adjusted-OI blend: recompute GEX + charm off the cached chain at OI + k·volume.
async function applyBlend() {
  try {
    const resp = await fetch(`/api/gex?k=${state.blendK}`);
    const d = await resp.json();
    if (!d || d.error) return;
    updateGex({ gex: d.gex });
    updateCharm({ gex: d.gex, charm: d.charm });
    if (d.pin_history) { state.pinHistory = d.pin_history; updatePinHistory(); } // server-persisted
  } catch (e) { /* keep last view on transient failure */ }
}

// Live blend => override the streamed k=0 GEX/charm; otherwise render as sent.
function renderGex(payload) {
  if (state.blendK > 0) applyBlend();
  else { updateGex(payload); updateCharm(payload); recordPinPoint(payload.gex); updatePinHistory(); }
}

(function initGexBlend() {
  const slider = document.getElementById("gex-blend");
  const val = document.getElementById("gex-blend-val");
  if (!slider) return;
  slider.addEventListener("input", () => {
    state.blendK = parseFloat(slider.value) || 0;
    if (val) val.textContent = state.blendK.toFixed(2);
    if (state.blendK > 0) applyBlend();
    else if (state.current) { updateGex(state.current); updateCharm(state.current); }
  });
})();

function updateAll(payload, changed) {
  window.__lastPrice = payload.lastSalePrice;
  payload.expiries.forEach((e) => state.byExpiry.set(e.expiry, e));
  renderGex(payload);
  updateWheel(payload);
  payload.expiries.forEach((exp) => updateExpiryLadder(exp, payload.wheel));
  if (changed && changed.size === 0) return; // idle delta: nothing else to redraw
  computeLeftMax(payload);
  updateHeatmap(payload);
  updateSmile(payload);
  payload.expiries.forEach((exp) => {
    if (changed && !changed.has(exp.expiry)) return;
    const pair = state.charts.get(exp.expiry);
    if (!pair) return;
    const l = pair.left;
    l.series[0].setData(zip(exp.strikes, exp.c_oi), false);
    l.series[1].setData(zip(exp.strikes, exp.c_vol), false);
    l.series[2].setData(zip(exp.strikes, exp.p_oi), false);
    l.series[3].setData(zip(exp.strikes, exp.p_vol), false);
    l.yAxis[0].update({ max: state.leftMax.total }, false);
    l.xAxis[0].update({ plotLines: leftPlotLines(exp) }, false);
    l.redraw();
  });
}

function renderStatus(payload) {
  const bar = document.getElementById("status-bar");
  const chg = payload.prevClose != null ? payload.lastSalePrice - payload.prevClose : null;
  const pct = chg != null && payload.prevClose ? (chg / payload.prevClose) * 100 : null;
  const cls = chg == null ? "" : chg >= 0 ? "up" : "down";
  const sign = chg == null ? "" : chg >= 0 ? "+" : "";
  bar.innerHTML =
    `<span class="price">${payload.ticker} $${fmt(payload.lastSalePrice)}</span>` +
    (chg != null ? `<span class="${cls}">${sign}${fmt(chg)}${pct != null ? ` (${sign}${fmt(pct)}%)` : ""}</span>` : "") +
    `<span class="muted">Source: ${payload.dataSource || "—"}</span>` +
    `<span class="muted">Market: ${payload.marketStatus || "—"}</span>` +
    `<span class="muted">Updated: ${payload.timestamp}</span>`;
}

function render(payload, changed) {
  state.current = payload;
  renderStatus(payload);
  markUpdated();
  const sameShape =
    state.charts.size === payload.expiries.length &&
    payload.expiries.every((e) => state.charts.has(e.expiry));
  if (sameShape) updateAll(payload, changed);
  else buildAll(payload);
}

// ── Next-refresh countdown bar ───────────────────────────────────────
let lastUpdateAt = Date.now();
state.cadenceMs = 15000; // WS push cadence; polling fallback overrides to REFRESH_MS
function markUpdated() { lastUpdateAt = Date.now(); }
setInterval(() => {
  const label = document.getElementById("refresh-label");
  if (!label) return;
  const remaining = Math.max(0, state.cadenceMs - (Date.now() - lastUpdateAt));
  label.textContent = `next refresh ~${Math.ceil(remaining / 1000)}s`;
}, 250);

// Merge a per-expiry delta into the cached payload, then re-render only what changed.
function applyDelta(msg) {
  const cur = state.current;
  if (!cur) return;
  ["lastSalePrice", "prevClose", "marketStatus", "dataSource", "timestamp", "gex", "charm", "wheel"].forEach((k) => {
    if (msg[k] !== undefined) cur[k] = msg[k];
  });
  const changed = new Set();
  if (msg.expiries) {
    for (const label in msg.expiries) {
      const idx = cur.expiries.findIndex((e) => e.expiry === label);
      if (idx >= 0) { cur.expiries[idx] = msg.expiries[label]; changed.add(label); }
    }
  }
  render(cur, changed);
}

// ── Transport: WebSocket streaming with a polling fallback ───────────
let pollTimer = null;
async function pollOnce() {
  try {
    const resp = await fetch("/api/option-chain");
    const payload = await resp.json();
    if (!resp.ok || payload.error) throw new Error(payload.error || `HTTP ${resp.status}`);
    render(payload);
  } catch (err) {
    document.getElementById("status-bar").innerHTML =
      `<span class="error">Failed to load option chain: ${err.message}</span>`;
  }
}

function startPolling() {
  if (state.polling) return;
  state.polling = true;
  state.cadenceMs = REFRESH_MS;
  pollOnce();
  pollTimer = setInterval(pollOnce, REFRESH_MS);
}

function connectWS() {
  if (!window.WebSocket) { startPolling(); return; }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}/ws/option-chain`); }
  catch (e) { startPolling(); return; }

  const guard = setTimeout(startPolling, 6000); // fall back if no data arrives
  ws.onmessage = (ev) => {
    clearTimeout(guard);
    if (state.polling) { clearInterval(pollTimer); state.polling = false; }
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.error) {
      document.getElementById("status-bar").innerHTML = `<span class="error">${msg.error}</span>`;
      return;
    }
    if (msg.type === "full") render(msg.payload);
    else if (msg.type === "delta") applyDelta(msg);
  };
  ws.onclose = () => { setTimeout(connectWS, 3000); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

// Highcharts renders at zero size while a <details> is collapsed; reflow both on expand.
const ivPanel = document.getElementById("iv-panel");
if (ivPanel) {
  ivPanel.addEventListener("toggle", () => {
    if (!ivPanel.open) return;
    if (state.heatmap) state.heatmap.reflow();
    if (state.smile) state.smile.reflow();
  });
}

// Smile starts collapsed; size it only once the user expands it.
const smilePanel = document.getElementById("smile-panel");
if (smilePanel) {
  smilePanel.addEventListener("toggle", () => {
    if (smilePanel.open && state.smile) state.smile.reflow();
  });
}

// ── Calculation window (real 2σ + probability-of-expiring per expiry) ─
function calcWindowHTML(payload) {
  const S = payload.lastSalePrice;
  const f = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
  const data = [];
  let rows = "";
  payload.expiries.forEach((e) => {
    const a = e.atm || {}, pr = e.prob || {};
    if (a.avg_iv == null) return;
    const i = data.length;
    const bd = e.bus_days;
    const straddle = (a.call_price || 0) + (a.put_price || 0);
    const sigT = straddle / (0.8 * S);
    data.push({ bd: bd, rate: pr.rate || 0, avg_iv: a.avg_iv });
    rows += `
      <section>
        <h2>${e.expiry}</h2>
        <table class="io">
          <tr><td>Spot S</td><td>$${f(S)}</td><td>ATM strike K</td><td>$${f(a.strike, 1)}</td></tr>
          <tr><td>Call IV</td><td>${f(a.call_iv, 1)}%</td><td>Put IV</td><td>${f(a.put_iv, 1)}%</td></tr>
          <tr><td>IV used</td><td><b id="iv${i}">${f(a.avg_iv, 1)}%</b></td><td>Call / Put price</td><td>$${f(a.call_price)} / $${f(a.put_price)}</td></tr>
          <tr><td>Business days</td><td>${f(bd, 2)}</td><td>Calendar days</td><td>${f(pr.cal_days, 2)}</td></tr>
        </table>
        <p class="formula"><b>2σ move</b> = S · IV · √(bd/252) · 2 = <b id="mv${i}">±${f(e.move)}</b></p>
        <p class="formula"><b>2σ range</b> = <span id="rg${i}">[ $${f(e.lower_2sigma)},  $${f(e.upper_2sigma)} ]</span></p>
        <p class="check"><b>Market straddle anchor</b>: C+P = $${f(straddle)} → 2σ ≈ <b>±$${f(2 * S * sigT)}</b> (convention-free true expected move; the scenario above uses your IV)</p>
        <table class="prob">
          <caption>Probability of Expiring (±3σ, lognormal)</caption>
          <tr><td>Below $<span id="pl${i}">${f(pr.lower)}</span></td><td>${f(pr.p_below, 2)}%</td></tr>
          <tr class="mid"><td>Between $<span id="pla${i}">${f(pr.lower)}</span> – $<span id="pua${i}">${f(pr.upper)}</span></td><td>${f(pr.p_between, 2)}%</td></tr>
          <tr><td>Above $<span id="pu${i}">${f(pr.upper)}</span></td><td>${f(pr.p_above, 2)}%</td></tr>
        </table>
      </section>`;
  });
  const boot = "var S=" + S + ",D=" + JSON.stringify(data) + ";" +
    "function g(id){return document.getElementById(id);}" +
    "function ff(v){return (v==null||isNaN(v))?'\\u2014':Number(v).toFixed(2);}" +
    "function recompute(){var ov=parseFloat(g('ivov').value);var use=(isFinite(ov)&&ov>0);" +
    "g('ovlabel').textContent=use?('scenario IV '+ov+'%'):'live market IV';" +
    "D.forEach(function(e,i){var iv=(use?ov:e.avg_iv)/100;var T=e.bd/252,sq=Math.sqrt(T),sig=iv*sq;" +
    "var move=S*iv*sq*2,lo=S-move,hi=S+move;var drift=(e.rate-0.5*iv*iv)*T;" +
    "var l3=S*Math.exp(drift-3*sig),u3=S*Math.exp(drift+3*sig);" +
    "g('iv'+i).textContent=ff(use?ov:e.avg_iv)+'%';g('mv'+i).textContent='\\u00b1'+ff(move);" +
    "g('rg'+i).textContent='[ $'+ff(lo)+',  $'+ff(hi)+' ]';" +
    "g('pl'+i).textContent=ff(l3);g('pla'+i).textContent=ff(l3);g('pu'+i).textContent=ff(u3);g('pua'+i).textContent=ff(u3);});}";
  return `<!doctype html><html><head><meta charset="utf-8"><title>2σ & Probability — ${payload.ticker}</title>
    <style>
      body{font-family:Arial,Helvetica,sans-serif;margin:20px;color:#222;background:#fafbfc}
      h1{font-size:18px} h2{font-size:15px;margin:0 0 6px;color:#2c3e50}
      section{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:12px 16px;margin-bottom:14px}
      table.io{border-collapse:collapse;margin-bottom:8px} table.io td{padding:2px 10px;font-size:13px} table.io td:nth-child(odd){color:#667}
      .formula{font-family:Consolas,monospace;font-size:13px;margin:3px 0}
      .check{font-size:12px;color:#478;margin:6px 0}
      table.prob{border-collapse:collapse;margin-top:8px;width:360px} table.prob caption{text-align:left;font-weight:bold;font-size:12px;margin-bottom:3px}
      table.prob td{border:1px solid #e3e6ea;padding:4px 10px;font-size:13px} table.prob tr.mid{background:#eef7ee;font-weight:bold}
      table.prob td:last-child{text-align:right}
      .hdr{color:#667;font-size:12px;margin-bottom:14px}
      .ivbox{background:#eef2ff;border:1px solid #ccd6ff;border-radius:6px;padding:8px 12px;margin-bottom:14px;font-size:13px}
      .ivbox input{width:70px;font-size:13px}
    </style></head><body>
    <h1>${payload.ticker} — 2σ &amp; Probability of Expiring</h1>
    <div class="hdr">Spot $${f(S)} · updated ${payload.timestamp} · tenor = business-days/252</div>
    <div class="ivbox"><b>Scenario IV override:</b> <input id="ivov" type="number" step="1" min="1" placeholder="market" oninput="recompute()" /> %
      &nbsp;→ using <b id="ovlabel">live market IV</b>. Blank = live ATM IV; type e.g. <b>41</b> to match a broker assumption. (The market-straddle anchor stays fixed as the true expected move.)</div>
    ${rows}
    <script>${boot}<\/script>
    </body></html>`;
}

function openCalcWindow() {
  if (!state.current) return;
  const w = window.open("", "calc", "width=800,height=920,scrollbars=yes");
  if (!w) return;
  w.document.open();
  w.document.write(calcWindowHTML(state.current));
  w.document.close();
}

const calcLink = document.getElementById("calc-link");
if (calcLink) calcLink.addEventListener("click", (ev) => { ev.preventDefault(); openCalcWindow(); });

function ladderHelpHTML(payload) {
  const f = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
  const S = payload ? payload.lastSalePrice : null;
  const pc = payload ? payload.prevClose : null;
  return `<!doctype html><html><head><meta charset="utf-8"><title>How to read the Yield Ladder</title>
    <style>
      body{font-family:Arial,Helvetica,sans-serif;margin:20px;color:#222;background:#fafbfc;max-width:760px}
      h1{font-size:18px} h2{font-size:14px;margin:0 0 6px;color:#2c3e50}
      section{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:12px 16px;margin-bottom:12px}
      .formula{font-family:Consolas,monospace;font-size:13px;margin:4px 0;background:#f6f8fa;padding:4px 8px;border-radius:4px}
      table.k{border-collapse:collapse;font-size:13px;margin:4px 0} table.k td,table.k th{border:1px solid #e3e6ea;padding:4px 10px;text-align:left}
      .sw{display:inline-block;width:26px;height:0;border-top-width:3px;vertical-align:middle;margin-right:6px}
      .csp{color:rgb(210,0,0)} .cc{color:rgb(0,128,0)}
      ul{margin:4px 0 4px 18px;padding:0} li{font-size:13px;margin:3px 0}
      .hdr{color:#667;font-size:12px;margin-bottom:12px}
      .warn{color:#a15c00}
    </style></head><body>
    <h1>Reading the Yield Ladder (annualized ROC %)</h1>
    <div class="hdr">One ladder per expiry, beside its Proj-OI chart.${S != null ? ` Spot $${f(S)}${pc != null ? ` · prev close $${f(pc)}` : ""}.` : ""}</div>

    <section>
      <h2>Axes &amp; colors</h2>
      <ul>
        <li><b>X = strike</b>, <b>Y = annualized return on capital (%)</b>.</li>
        <li><span class="sw csp" style="border-top-style:solid;border-color:rgb(210,0,0)"></span><b class="csp">CSP</b> (cash-secured puts) — strikes <b>below</b> spot.</li>
        <li><span class="sw cc" style="border-top-style:solid;border-color:rgb(0,128,0)"></span><b class="cc">CC</b> (covered calls) — strikes <b>above</b> spot.</li>
        <li>Only the <b>sellable band</b> (delta 0.05–0.45) is drawn.</li>
      </ul>
    </section>

    <section>
      <h2>The two lines per side</h2>
      <table class="k">
        <tr><th>Line</th><th>Premium used</th><th>Meaning</th></tr>
        <tr><td><b>bid</b> (solid, dots)</td><td>bid</td><td>What you actually collect selling now — the <b>actionable</b> yield.</td></tr>
        <tr><td><b>t-1</b> (dotted)</td><td>yesterday's close</td><td>Imputed from <code>Last − Change</code>, prev-close spot, DTE+1. Shows the <b>overnight yield shift</b>.</td></tr>
      </table>
    </section>

    <section>
      <h2>Formula</h2>
      <p class="formula">Ann ROC = premium ÷ capital × 365 ÷ DTE × 100</p>
      <ul>
        <li><b>CSP</b> capital = strike × 100 (cash secured).  <b>CC</b> capital = spot × 100 (shares held).</li>
        <li><b>t-1</b> uses yesterday's premium, <b>prev-close</b> as the CC base, and <b>DTE + 1</b>.</li>
      </ul>
    </section>

    <section>
      <h2>Worked examples</h2>
      <p><b>CSP — sell the $345 put</b>, 30 DTE, bid $6.00:</p>
      <p class="formula">Ann ROC = 6.00 ÷ 345 × 365 ÷ 30 × 100 = <b>21.2%</b></p>
      <p class="formula">Breakeven (cost basis if assigned) = 345 − 6.00 = <b>$339.00</b></p>
      <p class="formula">Capital = 345 × 100 = $34,500 → you collect $600 (1.74% in 30 days).</p>

      <p style="margin-top:10px"><b>CC — sell the $360 call</b> (spot $350), 30 DTE, bid $4.00:</p>
      <p class="formula">Ann ROC = 4.00 ÷ 350 × 365 ÷ 30 × 100 = <b>13.9%</b></p>
      <p class="formula">Effective sale price if called = 360 + 4.00 = <b>$364.00</b></p>

      <p style="margin-top:10px"><b>t-1 for that $345 put</b> — today Last $6.00, Change −$1.20:</p>
      <p class="formula">yesterday's close = Last − Change = 6.00 − (−1.20) = <b>$7.20</b></p>
      <p class="formula">Ann ROC (t-1) = 7.20 ÷ 345 × 365 ÷ 31 × 100 = <b>24.6%</b>  (uses DTE+1)</p>
      <p>Yield fell <b>24.6% → 21.2%</b> overnight — the premium decayed (better to have sold yesterday).</p>
    </section>

    <section>
      <h2>PoP &amp; delta (for the table)</h2>
      <p class="formula">PoP = (1 − |Δ|) × 100</p>
      <p>Δ 0.30 → PoP ≈ <b>70%</b> (yellow rows, richer/riskier) · Δ 0.16 → PoP ≈ <b>84%</b> (green rows, safer).</p>
    </section>

    <section>
      <h2>Reference lines</h2>
      <ul>
        <li><b>Spot</b> (black dashed) — current price.</li>
        <li><span class="sw" style="border-top-style:dashed;border-color:rgb(0,128,0)"></span><b class="cc">Call wall</b> / <span class="sw" style="border-top-style:dashed;border-color:rgb(210,0,0)"></span><b class="csp">Put wall</b> — this expiry's biggest dealer-gamma strikes (resistance / support). Good places to anchor CCs (near call wall) and CSPs (near put wall).</li>
      </ul>
    </section>

    <section>
      <h2>How to use it</h2>
      <ul>
        <li><b>Higher line = more premium per dollar-day.</b> Yield rises as strikes approach spot (more premium, less cushion).</li>
        <li><b>today vs t-1</b>: t-1 <i>above</i> today = premium <b>decayed overnight</b> (good if you already sold; less attractive to open now). t-1 <i>below</i> today = premium <b>richened</b> (IV pop — better selling now).</li>
        <li>Pick a strike that's high-yield <i>and</i> sitting at a wall — then confirm <b>PoP / cushion</b> in the table. Don't chase the highest ROC alone.</li>
      </ul>
    </section>

    <section>
      <h2 class="warn">Caveats</h2>
      <ul>
        <li class="warn"><b>t-1 is mark-based</b> — Nasdaq gives Change on <i>Last</i>, not bid, so the dotted line reads slightly richer than a true bid curve.</li>
        <li class="warn"><b>Short DTE inflates ROC</b> (annualizing a 1–2 day premium). Compare within a similar DTE.</li>
        <li class="warn">Walls use <b>T-1 open interest</b> and the "dealers long calls / short puts" convention.</li>
      </ul>
    </section>
    </body></html>`;
}

function openLadderHelp() {
  const w = window.open("", "ladderhelp", "width=820,height=920,scrollbars=yes");
  if (!w) return;
  w.document.open();
  w.document.write(ladderHelpHTML(state.current));
  w.document.close();
}

const ladderHelpLink = document.getElementById("ladder-help-link");
if (ladderHelpLink) ladderHelpLink.addEventListener("click", (ev) => { ev.preventDefault(); openLadderHelp(); });

function gexHelpHTML(payload) {
  const f = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
  const S = payload ? payload.lastSalePrice : null;
  return `<!doctype html><html><head><meta charset="utf-8"><title>How to read the GEX charts</title>
    <style>
      body{font-family:Arial,Helvetica,sans-serif;margin:20px;color:#222;background:#fafbfc;max-width:820px}
      h1{font-size:18px} h2{font-size:14px;margin:0 0 6px;color:#2c3e50}
      section{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:12px 16px;margin-bottom:12px}
      .formula{font-family:Consolas,monospace;font-size:13px;margin:4px 0;background:#f6f8fa;padding:4px 8px;border-radius:4px}
      table.k{border-collapse:collapse;font-size:13px;margin:4px 0;width:100%} table.k td,table.k th{border:1px solid #e3e6ea;padding:4px 10px;text-align:left;vertical-align:top}
      ul{margin:4px 0 4px 18px;padding:0} li{font-size:13px;margin:3px 0}
      .hdr{color:#667;font-size:12px;margin-bottom:12px}
      .green{color:rgb(0,128,0);font-weight:bold} .red{color:rgb(210,0,0);font-weight:bold} .purple{color:purple;font-weight:bold}
      .warn{color:#a15c00}
      .pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;font-weight:600}
      .pill.long{background:#e7f6ec;color:#1a7f37} .pill.short{background:#fdeaea;color:#b40426}
    </style></head><body>
    <h1>Reading the Dealer-Gamma (GEX) panel</h1>
    <div class="hdr">Five views, one story: where dealers are forced to hedge, and how that pushes price.${S != null ? ` Spot $${f(S)}.` : ""} Convention throughout: <b>dealers long calls / short puts</b>.</div>

    <section>
      <h2>First, the core idea</h2>
      <ul>
        <li><b>Gamma</b> = how fast a dealer's hedge (delta) changes as spot moves. Big gamma = big forced hedging.</li>
        <li><span class="green">Positive / long gamma</span> → dealers hedge <b>against</b> the move: <b>sell rallies, buy dips</b>. Stabilizing, vol-suppressing, <b>pins</b> price.</li>
        <li><span class="red">Negative / short gamma</span> → dealers hedge <b>with</b> the move: <b>sell dips, buy rallies</b>. Destabilizing, <b>amplifies</b> moves / trends.</li>
      </ul>
    </section>

    <section>
      <h2>Chart 1 — Net Dealer Gamma by Strike (bars)</h2>
      <p>Net dollar gamma per 1% move, at each strike, at the current spot. <span class="formula">GEX = (Γ<sub>call</sub>·OI<sub>call</sub> − Γ<sub>put</sub>·OI<sub>put</sub>) · 100 · S² · 0.01</span></p>
      <ul>
        <li><span class="green">Green bar</span> = call-dominated strike (stabilizing / resistance).</li>
        <li><span class="red">Red bar</span> = put-dominated strike (support, but destabilizing on a break).</li>
        <li><b>Tallest green = call wall</b> (overhead magnet / resistance). <b>Deepest red = put wall</b> (downside magnet / support).</li>
        <li>The tall bar tends to sit <b>at whatever strike spot is nearest</b> — it's gamma-driven, so it <i>hops</i> as price moves. That's your live pin magnet.</li>
      </ul>
    </section>

    <section>
      <h2>Chart 2 — Total GEX as spot moves (the dome/dip curve)</h2>
      <p>Sums total dealer gamma at every <i>hypothetical</i> spot from ~0.8× to 1.18× — it shows the regime you'd be in if price were there.</p>
      <ul>
        <li><span class="green">Dome above zero</span> = long-gamma zone → dips bought, rallies sold, <b>vol suppressed, pins to walls</b>.</li>
        <li><span class="red">Dip below zero</span> = short-gamma zone → moves amplified, <b>trending / air-pockets</b>.</li>
        <li><span class="purple">Flip (γ-flip)</span> = the zero-crossing = the regime boundary. <b>Above flip = stabilizing; below flip = destabilizing.</b> The single most important level on the board.</li>
        <li>Where the <b>spot line</b> sits on the curve tells you today's regime and how far the flip cushion is.</li>
      </ul>
    </section>

    <section>
      <h2>Chart 3 — Into-close hedge flow by strike (charm)</h2>
      <p><b>Charm</b> = delta decay. As time runs to the 4pm close (spot &amp; IV held fixed), option deltas drift, forcing dealers to re-hedge.</p>
      <ul>
        <li><span class="green">Green bar</span> = dealers must <b>buy</b> at that strike → <b>upward pull</b>.</li>
        <li><span class="red">Red bar</span> = dealers must <b>sell</b> → <b>downward pull</b>.</li>
        <li>The <b>charm badge</b> sums it: a positive $mm total is the mechanical <b>into-close pin drift up</b>; negative = down.</li>
        <li>Sign <b>flips as spot crosses a heavy strike</b> — either way it usually points <i>toward</i> the dominant pin. Strongest late-day, <b>fades to zero at the bell</b>.</li>
        <li>It's small ($mm total) — a quiet-tape drift, <b>not</b> a match for the gamma cascade if a real move starts.</li>
      </ul>
    </section>

    <section>
      <h2>The badges &amp; levels</h2>
      <table class="k">
        <tr><th>Item</th><th>Means</th></tr>
        <tr><td><span class="pill long">Long gamma</span></td><td>Stabilizing regime — fade moves, expect mean-reversion / pinning.</td></tr>
        <tr><td><span class="pill short">Short gamma</span></td><td>Destabilizing regime — respect trends, expect amplified moves.</td></tr>
        <tr><td><span class="purple">Flip</span></td><td>Regime boundary (zero-gamma). Reclaim/lose it = regime change.</td></tr>
        <tr><td><span class="green">Call wall</span></td><td>Biggest positive-gamma strike — resistance / upside pin.</td></tr>
        <tr><td><span class="red">Put wall</span></td><td>Biggest negative-gamma strike — support / downside pin; a break drops you toward the short-gamma trough.</td></tr>
      </table>
    </section>

    <section>
      <h2>Adjusted OI (the k slider)</h2>
      <ul>
        <li>Raw GEX uses <b>T-1 open interest</b>, which goes stale after a gap. The slider blends in today's volume: <span class="formula">adjusted OI = OI + k × today's volume</span></li>
        <li><b>k = 0</b> → pure settled OI. <b>k ≈ 0.3</b> → nowcast that drags the walls/flip toward where today's flow actually is.</li>
        <li>Trust the <b>wall/flip locations</b>, not the absolute $mm at high k (volume can dwarf OI on a busy/0DTE day).</li>
      </ul>
    </section>

    <section>
      <h2>The blue Expected-Move band (on charts 1 &amp; 2)</h2>
      <p>The shaded blue region = <b>spot ± the market's expected 1-day move</b>, from the nearest expiry's ATM straddle.</p>
      <p class="formula">Expected move ≈ 0.85 × (ATM call + ATM put)</p>
      <ul>
        <li>It's the market's own estimate of how far price is likely to travel by expiration (~1 standard deviation).</li>
        <li><b>Walls <i>inside</i> the band</b> → the market prices a move that can reach them → that side is genuinely at risk (riskier to sell).</li>
        <li><b>Walls <i>outside</i> the band</b> → the market doesn't expect price to get there → safer premium sale on that side.</li>
        <li>For a condor: the band tells you which short strike is <b>live</b> (inside) vs <b>safe</b> (outside).</li>
        <li>It <b>shrinks through the day</b> as theta bleeds — a 0DTE band collapses toward zero into the close.</li>
      </ul>
    </section>

    <section>
      <h2>Pin-evolution tracker (bottom chart)</h2>
      <p>A live session history of <b>spot · flip · call wall · put wall</b> (price axis) plus <b>total GEX</b> (right axis, <span class="green">blue = long</span> / <span class="red">red = short</span>).</p>
      <ul>
        <li><b>Walls converging on spot + total GEX rising</b> → the pin is <b>tightening</b> (safer to sell the range, esp. into the close).</li>
        <li><b>Spot approaching the flip / total GEX falling toward zero</b> → the pin is <b>fragile</b> — a regime flip is near.</li>
        <li><b>Walls migrating</b> (e.g. call wall stepping up as price rises) → the box is <b>moving</b>, not fixed — don't trust a static range.</li>
        <li>Accumulates while the page is open; <b>resets on refresh</b>.</li>
      </ul>
    </section>

    <section>
      <h2>How to trade each regime</h2>
      <ul>
        <li><span class="green">Long gamma (above flip):</span> range/pin bias. Sell premium, fade extremes, target the walls. Breakouts tend to fail.</li>
        <li><span class="red">Short gamma (below flip):</span> trend/momentum bias. Moves accelerate; dips get sold. Don't fade; respect the break.</li>
        <li><b>Walls</b> = pin targets &amp; where to anchor CCs (call wall) / CSPs (put wall).</li>
        <li><b>Flip</b> = your line in the sand. Losing it flips the whole character of the tape.</li>
      </ul>
    </section>

    <section>
      <h2 class="warn">Caveats</h2>
      <ul>
        <li class="warn">Assumes the <b>dealers long calls / short puts</b> convention — a simplification of true dealer positioning.</li>
        <li class="warn">Base OI is <b>T-1</b>; the k-slider is a heuristic nowcast, not measured order flow.</li>
        <li class="warn">Near-expiry ATM gamma <b>explodes</b> (T→0), so the tall bar can swing wildly in the last minutes.</li>
        <li class="warn">Charm is a small drift that <b>expires at 4pm</b> — never a backstop against a real directional break.</li>
        <li class="warn">Expected move uses the <b>0.85× straddle</b> rule of thumb; it's an estimate, and it collapses near expiry.</li>
        <li class="warn">The pin-evolution history is <b>client-side</b> — it starts empty and resets whenever you refresh the page.</li>
      </ul>
    </section>
    </body></html>`;
}

function openGexHelp() {
  const w = window.open("", "gexhelp", "width=860,height=940,scrollbars=yes");
  if (!w) return;
  w.document.open();
  w.document.write(gexHelpHTML(state.current));
  w.document.close();
}

const gexHelpLink = document.getElementById("gex-help-link");
if (gexHelpLink) gexHelpLink.addEventListener("click", (ev) => { ev.preventDefault(); openGexHelp(); });

// ── Intraday TSLA spot chart (separate Nasdaq feed, refreshed every 60s) ─
// Title shows both TSLA and SPY last + % change vs their prior close.
function spotTitleHTML(tslaLast, spyLast) {
  const seg = (name, last, prev) => {
    if (last == null) return `${name} —`;
    const px = Number(last).toFixed(2);
    if (!prev) return `${name} $${px}`;
    const chg = last - prev, pct = (chg / prev) * 100;
    const col = chg >= 0 ? "rgb(0,128,0)" : "rgb(210,0,0)";
    const sign = chg >= 0 ? "+" : "";
    return `${name} $${px} <span style="color:${col}">${sign}${pct.toFixed(2)}%</span>`;
  };
  return `${seg("TSLA", tslaLast, state.tslaPrev)} &nbsp;·&nbsp; ${seg("SPY", spyLast, state.spyPrev)}`;
}

function buildSpotChart(payload) {
  if (state.spot) { state.spot.destroy(); state.spot = null; }
  const prev = payload.prevClose;
  const spyPrev = payload.spy_prevClose;
  const spyLast = payload.spy_last;
  state.tslaPrev = prev;
  state.spyPrev = spyPrev;
  state.spot = Highcharts.chart("spot-chart", {
    chart: { type: "line", height: 220, spacingTop: 16, zoomType: "x" },
    title: {
      useHTML: true,
      text: spotTitleHTML(payload.last, spyLast),
      align: "left", style: { fontSize: "13px" },
    },
    credits: { enabled: false },
    legend: { enabled: true, itemStyle: { fontSize: "10px" } },
    xAxis: { type: "datetime", crosshair: true, min: payload.sessionStart || null, max: payload.sessionEnd || null },
    yAxis: [
      {
        title: { text: "TSLA", style: { color: "rgb(0,128,0)", fontSize: "10px" } },
        labels: { style: { color: "rgb(0,128,0)" } },
        plotLines: prev != null ? [{ value: prev, color: "#888", dashStyle: "Dash", width: 1, zIndex: 3,
          label: { text: `TSLA prev $${Number(prev).toFixed(2)}`, style: { fontSize: "9px", color: "#667" } } }] : [],
      },
      {
        title: { text: "SPY", style: { color: "#2962ff", fontSize: "10px" } },
        labels: { style: { color: "#2962ff" } }, opposite: true, gridLineWidth: 0,
        plotLines: spyPrev != null ? [{ value: spyPrev, color: "#9ab", dashStyle: "Dash", width: 1, zIndex: 3,
          label: { text: `SPY prev $${Number(spyPrev).toFixed(2)}`, align: "right", x: -4, style: { fontSize: "9px", color: "#7891bf" } } }] : [],
      },
    ],
    tooltip: { shared: true, xDateFormat: "%b %e, %H:%M", pointFormat: "<span style=\"color:{series.color}\">●</span> {series.name}: <b>${point.y:.2f}</b><br/>" },
    plotOptions: { series: { marker: { enabled: false }, lineWidth: 1.5 } },
    series: [
      { name: "TSLA", yAxis: 0, color: "rgb(0,128,0)", negativeColor: "rgb(210,0,0)", threshold: prev != null ? prev : null, data: payload.points },
      { name: "SPY", yAxis: 1, color: "#2962ff", data: payload.spy_points || [] },
    ],
  });
}

async function loadSpot() {
  try {
    const r = await fetch("/api/spot");
    const p = await r.json();
    if (!r.ok || p.error) return;
    buildSpotChart(p);
  } catch (e) { /* keep last chart */ }
}

// Stream the live last price and append points for a real-time spot chart.
function connectSpotWS() {
  if (!window.WebSocket) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}/ws/spot`); } catch (e) { return; }
  state.spotWS = ws;
  ws.onopen = () => {
    const sel = document.getElementById("spot-interval");
    if (sel) ws.send(JSON.stringify({ interval: Number(sel.value) }));
  };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.price == null || !state.spot) return;
    state.spot.setTitle({ text: spotTitleHTML(m.price, m.spy) }, false, false);
    // Streamed points render as dots filling toward the 4pm close.
    state.spot.series[0].addPoint({ x: m.t, y: m.price, marker: { enabled: true, radius: 2 } }, true, false);
    if (m.spy != null && state.spot.series[1]) {
      state.spot.series[1].addPoint({ x: m.t, y: m.spy, marker: { enabled: true, radius: 2 } }, true, false);
    }
    state._lastSpot = m.price;
  };
  ws.onclose = () => { if (!state.spotPaused) setTimeout(connectSpotWS, 3000); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

const spotIntervalSel = document.getElementById("spot-interval");
if (spotIntervalSel) {
  spotIntervalSel.addEventListener("change", () => {
    if (state.spotWS && state.spotWS.readyState === WebSocket.OPEN) {
      state.spotWS.send(JSON.stringify({ interval: Number(spotIntervalSel.value) }));
    }
  });
}

const spotPanel = document.getElementById("spot-panel");
let spotResyncTimer = null;

// Start/stop the spot feed so a collapsed panel stops hitting Nasdaq.
function startSpot() {
  state.spotPaused = false;
  loadSpot();
  if (!spotResyncTimer) spotResyncTimer = setInterval(loadSpot, 300000);
  connectSpotWS();
}
function stopSpot() {
  state.spotPaused = true;
  if (state.spotWS) { try { state.spotWS.close(); } catch (e) {} state.spotWS = null; }
  if (spotResyncTimer) { clearInterval(spotResyncTimer); spotResyncTimer = null; }
}

if (spotPanel) {
  spotPanel.addEventListener("toggle", () => {
    if (spotPanel.open) { startSpot(); if (state.spot) state.spot.reflow(); }
    else { stopSpot(); }
  });
}
if (!spotPanel || spotPanel.open) startSpot();

connectWS();
