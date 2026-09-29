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
  const node = (tag, text, klass) => { const n=document.createElement(tag);if(text!=null)n.textContent=text;if(klass)n.className=klass;return n; };
  const grade=["A","B","C","D","F"].includes(r.overall_grade)?r.overall_grade:"F";
  const card=node("div",null,"card"+((r.flags||[]).some(f=>f.toLowerCase().includes("illiquid"))?" illiquid":""));
  const badge=node("div",null,"badge grade-"+grade);
  badge.append(node("span",grade,"letter"),node("span",String(Math.round(r.overall_score)),"num"));
  const body=node("div",null,"body");
  body.append(node("div",r.underlying+" "+fmtMoney(r.strike)+" "+(r.type==="call"?"Call":"Put"),"title"));
  body.append(node("div","exp "+r.expiration+" · "+r.dte+" DTE · "+fmtMoney(r.premium_per_share)+"/sh · "+fmtMoney(r.cost_per_contract)+"/contract · break-even "+fmtMoney(r.break_even),"sub"));
  body.append(node("div",r.overall_meaning,"meaning"));
  if(r.flags?.length)body.append(node("div","⚠ "+r.flags.join(" · "),"flags"));
  const chips=node("div",null,"subscores"),table=node("table",null,"detail-table");
  for(const score of r.sub_scores||[]){
    const sg=["A","B","C","D","F"].includes(score.grade)?score.grade:"F";
    const chip=node("span",null,"chip");chip.append(node("span",score.label,"ck"),node("span",sg,"cg fg-"+sg),node("span",String(Math.round(score.score))));chips.append(chip);
    const row=node("tr"),explanation=node("td",score.explanation);explanation.append(node("div",score.metric||"","m"));
    row.append(node("td",sg,"g fg-"+sg),node("td",score.label,"k"),explanation);table.append(row);
  }
  body.append(chips);const details=node("details",null,"detail");details.append(node("summary","Why this grade?"),table);
  const trace=r.score_trace;
  if(trace){
    details.append(node("p","Weighted score "+trace.weighted_score.toFixed(4)+" − liquidity penalty "+trace.liquidity_penalty.toFixed(4)+" = final "+trace.final_score.toFixed(4)+". "+(trace.interpretation||"No usable price: score forced to zero.")));
    const contributions=node("table",null,"detail-table"),head=node("tr");
    ["Dimension","Score","Weight","Contribution","Basis"].forEach(label=>head.append(node("th",label)));contributions.append(head);
    for(const c of trace.components){const row=node("tr");[c.key,c.score.toFixed(3),(c.normalized_weight*100).toFixed(1)+"%",c.contribution.toFixed(4),c.basis].forEach(v=>row.append(node("td",String(v))));contributions.append(row);}
    details.append(contributions,node("p","Liquidity gate: "+(trace.liquidity_gate?"triggered":"not triggered")+". Missing-input fallback scores are included in the weighted recipe; they are not observations."));
  }
  body.append(details);
  const simulate=node("button","Explore scenarios");simulate.type="button";simulate.addEventListener("click",()=>window.optionScenario?.open(r));body.append(simulate);
  const exportButton=node("button","Export this grade");exportButton.type="button";
  exportButton.addEventListener("click",()=>{
    const url=URL.createObjectURL(new Blob([JSON.stringify({schema:"option-contract-grader.grade",version:1,contract:r},null,2)+"\n"],{type:"application/json"}));
    const a=document.createElement("a");a.href=url;a.download="option-grade.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });body.append(exportButton);card.append(badge,body);return card;
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
