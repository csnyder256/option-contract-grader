"use strict";

/* ---------- shared helpers ---------- */
function fmtMoney(x) {
  if (x === null || x === undefined) return "-";
  return "$" + Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(x) {
  if (x === null || x === undefined) return "-";
  return (Number(x) * 100).toFixed(1) + "%";
}
function el(id) { return document.getElementById(id); }

function renderInto(container, results) {
  container.innerHTML = "";
  for (const r of results) container.appendChild(renderCard(r));
}

function renderCard(r) {
  const card = document.createElement("div");
  const illiquid = (r.flags || []).some((f) => f.toLowerCase().includes("illiquid"));
  card.className = "card" + (illiquid ? " illiquid" : "");

  const title = `${r.underlying} ${fmtMoney(r.strike)} ${r.type === "call" ? "Call" : "Put"}`;
  const sub = `exp ${r.expiration} · ${r.dte} DTE · ${fmtMoney(r.premium_per_share)}/sh · ${fmtMoney(r.cost_per_contract)}/contract · break-even ${fmtMoney(r.break_even)}`;

  const chips = (r.sub_scores || [])
    .map((s) => `<span class="chip"><span class="ck">${s.label}</span><span class="cg fg-${s.grade}">${s.grade}</span><span>${Math.round(s.score)}</span></span>`)
    .join("");
  const detailRows = (r.sub_scores || [])
    .map((s) => `<tr><td class="g fg-${s.grade}">${s.grade}</td><td class="k">${s.label}</td><td>${s.explanation}<div class="m">${s.metric || ""}</div></td></tr>`)
    .join("");
  const flags = r.flags && r.flags.length ? `<div class="flags">⚠ ${r.flags.join(" · ")}</div>` : "";

  card.innerHTML = `
    <div class="badge grade-${r.overall_grade}">
      <span class="letter">${r.overall_grade}</span>
      <span class="num">${Math.round(r.overall_score)}</span>
    </div>
    <div class="body">
      <div class="title">${title}</div>
      <div class="sub">${sub}</div>
      <div class="meaning">${r.overall_meaning}</div>
      ${flags}
      <div class="subscores">${chips}</div>
      <details class="detail"><summary>Why this grade?</summary>
        <table class="detail-table">${detailRows}</table>
      </details>
    </div>`;
  return card;
}

function renderLegend(key) {
  if (!key) return;
  el("legend-body").innerHTML =
    "<table>" +
    key.map((b) => `<tr><td class="lg fg-${b.grade}">${b.grade}</td><td class="lr">${b.min}–${b.max}</td><td>${b.meaning}</td></tr>`).join("") +
    "</table>";
}

function notesHtml(notes) {
  return notes && notes.length ? `<div class="notes">⚠ ${notes.join(" ")}</div>` : "";
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    el("panel-" + t.dataset.tab).classList.add("active");
  });
});

/* ---------- single ticker ---------- */
const form = el("scan-form");
const scanBtn = el("scan-btn");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(form).entries());
  if (!d.ticker.trim()) return;

  scanBtn.disabled = true;
  el("status").className = "status";
  el("status").textContent = "Scanning " + d.ticker.toUpperCase() + "…";
  el("meta").innerHTML = "";
  el("results").innerHTML = "";

  const body = { ticker: d.ticker.trim().toUpperCase(), side: d.side, limit: 50 };
  if (d.expiration_from) body.expiration_from = d.expiration_from;
  if (d.expiration_to) body.expiration_to = d.expiration_to;
  if (d.premium_min !== "") body.premium_min = parseFloat(d.premium_min);
  if (d.premium_max !== "") body.premium_max = parseFloat(d.premium_max);

  try {
    const resp = await fetch("/scan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const payload = await resp.json();
    if (!resp.ok) throw new Error(payload.detail || "HTTP " + resp.status);
    const m = payload.meta;
    el("meta").innerHTML = [
      ["Underlying", fmtMoney(m.underlying_price)],
      ["Realized vol (HV)", fmtPct(m.historical_vol)],
      ["ATM IV", fmtPct(m.atm_iv)],
      ["IV Rank", m.iv_rank === null ? "warming up" : m.iv_rank.toFixed(0)],
      ["Scored", String(m.contracts_scored)],
      ["Env", m.environment],
    ].map(([k, v]) => `<span class="stat">${k}: <strong>${v}</strong></span>`).join("") + notesHtml(m.notes);
    renderLegend(m.grade_key);
    renderInto(el("results"), payload.results);
    el("status").textContent = payload.results.length
      ? payload.results.length + " contracts graded (best first)."
      : "No contracts matched your filters.";
  } catch (err) {
    el("status").className = "status error";
    el("status").textContent = "Error: " + err.message;
  } finally {
    scanBtn.disabled = false;
  }
});

/* ---------- market sweep ---------- */
const marketForm = el("market-form");
const marketBtn = el("market-btn");
let marketResults = [];
let pollTimer = null;

function metricValue(r, key) {
  if (key === "overall") return r.overall_score;
  const s = (r.sub_scores || []).find((x) => x.key === key);
  return s ? s.score : -1;
}
function applyMarketSort() {
  const key = el("market-sort").value;
  const sorted = [...marketResults].sort((a, b) => metricValue(b, key) - metricValue(a, key));
  renderInto(el("market-results"), sorted);
}
el("market-sort").addEventListener("change", applyMarketSort);

function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function updateProgress(st) {
  const prog = el("market-progress");
  const bar = prog.querySelector(".bar");
  const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
  prog.hidden = st.status !== "running";
  bar.style.width = pct + "%";
  const phase = st.phase === "prices" ? "checking prices" : st.phase === "chains" ? "scanning chains" : st.phase || "";
  if (st.status === "running") {
    el("market-status").className = "status";
    el("market-status").textContent = `Sweeping… ${phase} ${st.done}/${st.total}`;
  }
}

async function pollMarket() {
  try {
    const st = await (await fetch("/market/status")).json();
    updateProgress(st);
    if (st.status === "done") {
      stopPoll();
      marketResults = st.results || [];
      el("market-meta").innerHTML = notesHtml(st.notes);
      el("market-sortbar").hidden = marketResults.length === 0;
      el("market-sort").value = "overall";
      applyMarketSort();
      el("market-status").textContent = marketResults.length
        ? `Top ${marketResults.length} across the market (best first).`
        : "No contracts matched your filters.";
      marketBtn.disabled = false;
    } else if (st.status === "error") {
      stopPoll();
      el("market-status").className = "status error";
      el("market-status").textContent = "Error: " + (st.error || "sweep failed");
      marketBtn.disabled = false;
    }
  } catch (err) {
    stopPoll();
    el("market-status").className = "status error";
    el("market-status").textContent = "Error: " + err.message;
    marketBtn.disabled = false;
  }
}

marketForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(marketForm).entries());
  const body = { side: d.side, limit: 50 };
  if (d.price_min !== "") body.price_min = parseFloat(d.price_min);
  if (d.price_max !== "") body.price_max = parseFloat(d.price_max);
  if (d.premium_min !== "") body.premium_min = parseFloat(d.premium_min);
  if (d.premium_max !== "") body.premium_max = parseFloat(d.premium_max);
  if (d.dte_from !== "") body.dte_from = parseInt(d.dte_from, 10);
  if (d.dte_to !== "") body.dte_to = parseInt(d.dte_to, 10);

  marketBtn.disabled = true;
  el("market-status").className = "status";
  el("market-status").textContent = "Starting sweep…";
  el("market-meta").innerHTML = "";
  el("market-results").innerHTML = "";
  el("market-sortbar").hidden = true;

  try {
    const resp = await fetch("/market/scan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const st = await resp.json();
    if (!resp.ok) throw new Error(st.detail || "HTTP " + resp.status);
    stopPoll();
    pollTimer = setInterval(pollMarket, 2000);
    pollMarket();
  } catch (err) {
    el("market-status").className = "status error";
    el("market-status").textContent = "Error: " + err.message;
    marketBtn.disabled = false;
  }
});

/* ---------- boot ---------- */
fetch("/key").then((r) => r.json()).then((d) => renderLegend(d.grade_key)).catch(() => {});
