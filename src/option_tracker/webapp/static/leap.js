"use strict";

Highcharts.setOptions({ chart: { animation: false }, plotOptions: { series: { animation: false } } });

const REFRESH_MS = 15 * 60 * 1000;
let chart = null;

const fmt = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

function onPointClick() {
  const url = this.options.url;
  const title = document.getElementById("leap-detail-title");
  const hint = document.getElementById("leap-hint");
  const img = document.getElementById("leap-detail-img");
  title.textContent = `Option History  $${fmt(this.x, 0)} · ${this.series.name}`;
  if (!url) {
    hint.textContent = "No drill-down chart available for this contract.";
    img.style.display = "none";
    return;
  }
  hint.innerHTML = '<span class="spinner"></span>Loading chart…';
  img.style.display = "none";
  const proxied = "/api/leap/drilldown?url=" + encodeURIComponent(url);
  img.onload = () => { hint.textContent = ""; img.style.display = "block"; };
  img.onerror = () => { hint.textContent = "Image fetch failed."; img.style.display = "none"; };
  img.src = proxied;
}

function buildChart(payload) {
  const series = payload.series.map((s) => ({
    name: s.expiry,
    color: s.color,
    lineWidth: 1,
    data: s.points.map((p) => ({
      x: p.x,
      y: p.y,
      url: p.url,
      oi: p.oi,
      vol: p.vol,
      marker: { radius: Math.max(2, Math.min(30, p.radius)) },
    })),
  }));

  chart = Highcharts.chart("leap-chart", {
    chart: { type: "line", zoomType: "xy", height: 660 },
    title: { text: `TSLA LEAP Calls — theta by strike (updated ${payload.timestamp})` },
    credits: { enabled: false },
    xAxis: { title: { text: "Strike" }, crosshair: true },
    yAxis: { title: { text: "Call Last (Theta)" }, min: 0 },
    legend: { enabled: true, maxHeight: 90, itemStyle: { fontSize: "10px" } },
    tooltip: {
      useHTML: true,
      headerFormat: "",
      pointFormatter: function () {
        return (
          `<b>${this.series.name}</b><br/>` +
          `Strike: $${Highcharts.numberFormat(this.x, 0)}<br/>` +
          `Theta: ${fmt(this.y)}<br/>` +
          `Open Interest: ${Highcharts.numberFormat(this.oi, 0)}<br/>` +
          `Volume: ${Highcharts.numberFormat(this.vol, 0)}`
        );
      },
    },
    plotOptions: {
      series: {
        marker: { enabled: true, fillOpacity: 0.3 },
        states: { inactive: { opacity: 0.3 } },
        point: { events: { click: onPointClick } },
      },
    },
    series,
  });
}

async function load() {
  try {
    const resp = await fetch("/api/leap");
    const payload = await resp.json();
    if (!resp.ok || payload.error) throw new Error(payload.error || `HTTP ${resp.status}`);
    document.getElementById("status-bar").innerHTML =
      `<span class="muted">${payload.ticker} LEAP calls · ${payload.series.length} expiries · Updated ${payload.timestamp}</span>`;
    buildChart(payload);
  } catch (err) {
    document.getElementById("status-bar").innerHTML =
      `<span class="error">Failed to load LEAP data: ${err.message}</span>`;
  }
}

load();
setInterval(load, REFRESH_MS);
