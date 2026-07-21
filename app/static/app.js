/* martin_stock 프론트엔드 — REBYPRODUCT 재고관리 */
"use strict";

const $ = (id) => document.getElementById(id);
const NF = (n) => (n == null ? "—" : Number(Math.round(n * 1000) / 1000).toLocaleString("ko-KR"));
const PCT = (a, b) => (b > 0 ? (a / b * 100).toFixed(1) + "%" : "—");
// 로컬 시간대 기준 YYYY-MM-DD (toISOString은 UTC라 KST에서 날짜가 밀림 — 사용 금지)
const fmtISO = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const todayISO = () => fmtISO(new Date());
const DOW = ["일", "월", "화", "수", "목", "금", "토"];
const dowOf = (iso) => DOW[new Date(iso + "T00:00:00").getDay()];

let ROLE = null, USERNAME = null, DUTY = "all", MYDUTY = new Set();   // MYDUTY = 내 담당 코드 Set
// 금액 열람 권한: mat(자재 단가·금액) / prod(생산·출고·재고 금액) / labor(시급·노무비) / cost(원가 분석)
// admin은 서버가 전체를 내려줌 — 나머지는 admin이 사용자 탭에서 체크한 항목만
let MPERM = new Set();
function canM(key) { return ROLE === "admin" || MPERM.has(key); }
const MONEY_LABELS = { mat: "자재 단가·금액", prod: "생산·출고·재고 금액", labor: "시급·노무비", cost: "원가 분석" };
// 담당 — 사용자마다 여러 개 지정 가능. 전부 체크하면 '전체'(앞으로 담당이 늘어도 자동 포함)
const DUTY_LABELS = { production: "생산실적", shipment: "완제품 출고", usage: "자재 사용",
  staffing: "인원·가동", stock: "재고·입고", lot: "LOT 관리" };
const DUTY_KEYS = Object.keys(DUTY_LABELS);
/** 저장값('all'/'none'/'a,b') → 체크된 담당 Set */
function dutySet(duty) {
  const d = (duty || "all").trim();
  if (d === "all") return new Set(DUTY_KEYS);
  if (d === "none" || !d) return new Set();
  return new Set(d.split(",").filter(k => DUTY_KEYS.includes(k)));
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail || msg; } catch (e) { /* raw text */ }
    toast(String(msg).slice(0, 180));
    throw new Error(path);
  }
  return r.json();
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), 2600);
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ── 마스터 캐시 ─────────────────────── */
const M = { product: [], raw: [], sub: [], partner: [], staff: [], line: [] };
// 포장 세트 (다대다) — [{name, members:[{id,name,pack_count}]}]. 한 자재가 여러 세트에 속할 수 있다.
let PACKSETS = [];
async function loadPackSets() {
  try { PACKSETS = await api("/api/packsets"); } catch (e) { PACKSETS = []; }
}
const packSetMembers = name => (PACKSETS.find(s => s.name === name) || {}).members || [];
const packSetsOf = mid => PACKSETS.filter(s => s.members.some(m => m.id === mid)).map(s => s.name);
async function loadMasters() {
  const keys = Object.keys(M);
  const res = await Promise.all(keys.map(k => api("/api/masters/" + k)));
  keys.forEach((k, i) => (M[k] = res[i]));
  // 라인 표시명: 번호 + 라인명 / 공정 (동명 라인 구분용)
  M.line.forEach((l, i) => (l.disp = `${i + 1}. ${l.name}${l.process ? " / " + l.process : ""}`));
  await loadPackSets();
}
const productById = (id) => M.product.find(p => p.id === id);
const materialById = (id) => M.raw.concat(M.sub).find(m => m.id === id);
async function reloadMaster(t) {
  M[t] = await api("/api/masters/" + t);
  if (t === "line")
    M.line.forEach((l, i) => (l.disp = `${i + 1}. ${l.name}${l.process ? " / " + l.process : ""}`));
  if (t === "product") { try { ANA.raw = null; } catch (e) {} }   // 분석 캐시 무효화
  if (t === "product" || t === "raw" || t === "sub") { try { COSTS = null; } catch (e) {} }   // 원가 캐시 무효화
  // 안전재고·재고를 바꾸면 사이드바 '발주 필요' 알림도 따라 갱신
  if (t === "raw" || t === "sub") { try { loadLowStock(); } catch (e) {} }
}

/* ── 네비게이션 ─────────────────────── */
const TITLES = { dash: "대시보드", prod: "생산 현황", ship: "출고 현황", entry: "일일 입력", lot: "LOT 관리", items: "기준정보 관리", ana: "분석", lookup: "기록 조회" };
/* 표 검색(필터)은 화면·탭을 옮기면 초기화한다.
   남아 있으면 다른 화면에서 '등록된 항목이 없습니다'만 보여 데이터가 없는 것처럼 오해하게 된다.
   ※ 행 추가용 검색(qaProd 등)은 필터가 아니라 입력칸이므로 대상 아님. */
function resetSearches() {
  // [입력칸 id, 함께 비울 상태] — prodFilter·anaRotFilter는 DOM 값을 직접 읽으므로 상태 없음
  [["mFilter", () => { mFilter = ""; }],
   ["bomProdSearch", () => { BOM.q = ""; }],
   ["lotFilter", () => { LOT.q = ""; }],
   ["shipHistFilter", () => { LOT.shipQ = ""; }],
   ["dispHistFilter", () => { LOT.dispQ = ""; }],
   ["shipFilter", () => { SHIP.q = ""; }],
   ["prodFilter", null], ["anaRotFilter", null],
  ].forEach(([id, clear]) => {
    const el = $(id);
    if (el) el.value = "";
    if (clear) clear();
  });
}
$("nav").addEventListener("click", e => {
  const b = e.target.closest("button[data-scr]"); if (!b) return;
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".screen").forEach(s => s.classList.toggle("on", s.id === "scr-" + b.dataset.scr));
  $("scrTitle").textContent = TITLES[b.dataset.scr];
  resetSearches();
  const fn = { dash: loadDash, prod: loadProd, ship: loadShip, entry: openEntry, lot: loadLot, items: renderMasters, ana: loadAna, lookup: () => lkCal.render() }[b.dataset.scr];
  if (fn) fn();
});
// 화면 내 버튼(예: 대시보드 배너)에서 다른 화면으로 이동
document.addEventListener("click", e => {
  const g = e.target.closest("[data-goscr]"); if (!g) return;
  const nb = document.querySelector(`#nav button[data-scr="${g.dataset.goscr}"]`);
  if (nb) nb.click();
});

/* ── Chart.js 설정 (martin_data 대시보드 방식, 로컬 번들) ── */
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.font.size = 11.5;
Chart.defaults.color = "#75726C";
Chart.defaults.borderColor = "#ECEAE5";
Chart.defaults.plugins.tooltip.backgroundColor = "#121212";
Chart.defaults.plugins.tooltip.padding = 10;
Chart.defaults.plugins.tooltip.cornerRadius = 8;
Chart.defaults.plugins.tooltip.boxPadding = 4;
Chart.defaults.plugins.legend.display = false;

const CHARTS = {};
function mkChart(el, cfg) {
  if (CHARTS[el.id]) CHARTS[el.id].destroy();
  CHARTS[el.id] = new Chart(el, cfg);
  return CHARTS[el.id];
}
const AXIS_FMT = v => Math.abs(v) >= 10000
  ? (v / 10000).toLocaleString("ko-KR", { maximumFractionDigits: 1 }) + "만"
  : v.toLocaleString("ko-KR");
function zoomOpts(on) {
  if (!on || !window.ChartZoom && !Chart.registry.plugins.get("zoom")) return undefined;
  return { zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: "x" },
           pan: { enabled: true, mode: "x", modifierKey: null } };
}
function baseOptions(cfg, nSets) {
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 350 },
    interaction: { mode: "index", intersect: false },
    onClick: (ev, els, chart) => {
      if (!cfg.onClick) return;
      const p = chart.getElementsAtEventForMode(ev, "index", { intersect: false }, true);
      if (p.length) cfg.onClick(p[0].index);
    },
    plugins: {
      legend: { display: nSets > 1, position: "top", align: "end",
        labels: { boxWidth: 10, boxHeight: 10, borderRadius: 2, useBorderRadius: true } },
      tooltip: { callbacks: { label: c =>
        ` ${c.dataset.label || ""} ${Number(c.parsed.y).toLocaleString("ko-KR")}` } },
      zoom: zoomOpts(cfg.zoom),
    },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 16, maxRotation: 0 } },
      y: { beginAtZero: true, border: { display: false },
           ticks: { maxTicksLimit: 5, callback: v => (cfg.fmtAxis || AXIS_FMT)(v) } },
    },
  };
}

/* 막대 차트 (스택 + 계획 점선) — 기존 시그니처 유지 */
function barChart(el, cfg) {
  const stacked = cfg.series.length > 1;
  const datasets = cfg.series.map(s => ({
    type: "bar", label: s.name || "수량", data: s.values,
    backgroundColor: s.color, borderRadius: 5, maxBarThickness: 34,
    stack: stacked ? "s" : undefined,
  }));
  if (cfg.plan && cfg.plan.some(v => v > 0)) {
    datasets.push({ type: "line", label: "계획", data: cfg.plan, borderColor: "#C2372C",
      borderDash: [5, 4], borderWidth: 1.8, pointRadius: 0, stepped: "middle" });
  }
  const options = baseOptions(cfg, datasets.length);
  options.scales.x.stacked = stacked;
  options.scales.y.stacked = stacked;
  return mkChart(el, { data: { labels: cfg.labels, datasets }, options });
}

/* 선 차트 — 기존 시그니처 유지 (dash/fill 지원) */
function lineChart(el, cfg) {
  const datasets = cfg.series.map(s => ({
    type: "line", label: s.name || "", data: s.values,
    borderColor: s.color, borderWidth: 2, tension: 0.25,
    backgroundColor: s.fill || "transparent", fill: !!s.fill,
    borderDash: s.dash ? s.dash.split(" ").map(Number) : undefined,
    pointRadius: cfg.labels.length > 40 ? 0 : 2.5,
    pointHoverRadius: 4.5, pointBackgroundColor: s.color,
  }));
  return mkChart(el, { data: { labels: cfg.labels, datasets },
    options: baseOptions(cfg, datasets.length) });
}

/* ── 달력 컴포넌트 ───────────────────── */
function Calendar(prefix, onPick) {
  const self = {
    ym: todayISO().slice(0, 7), sel: null, dates: new Set(),
    async render() {
      const data = await api("/api/calendar?ym=" + self.ym);
      self.dates = new Set(data.dates);
      const [y, m] = self.ym.split("-").map(Number);
      $(prefix + "Lbl").textContent = `${y}년 ${m}월`;
      const first = new Date(y, m - 1, 1).getDay();
      const nd = new Date(y, m, 0).getDate();
      const today = todayISO();
      let h = "";
      for (let i = 0; i < first; i++) h += "<span></span>";
      for (let d = 1; d <= nd; d++) {
        const iso = `${self.ym}-${String(d).padStart(2, "0")}`;
        const cls = [
          self.dates.has(iso) ? "has" : "",
          iso === today ? "today" : "",
          iso === self.sel ? "sel" : "",
          iso > today && !self.dates.has(iso) ? "off" : "",
        ].filter(Boolean).join(" ");
        h += `<button class="${cls}" data-d="${iso}">${d}</button>`;
      }
      $(prefix + "Days").innerHTML = h;
    },
    move(k) {
      const [y, m] = self.ym.split("-").map(Number);
      const d = new Date(y, m - 1 + k, 1);
      self.ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      self.render();
    },
  };
  $(prefix + "Prev").onclick = () => self.move(-1);
  $(prefix + "Next").onclick = () => self.move(1);
  $(prefix + "Days").addEventListener("click", e => {
    const b = e.target.closest("button[data-d]"); if (!b) return;
    self.sel = b.dataset.d; self.render(); onPick(b.dataset.d);
  });
  return self;
}

/* ── 공용 날짜 선택 달력 ─────────────────
   어느 화면이든 class "datepick" 입력을 클릭하면 같은 달력 팝업이 뜬다.
   값(YYYY-MM-DD)은 input.value에 그대로 넣고 input·change 이벤트를 발생시켜
   기존 로직(wireEntryTable·검색 재실행 등)과 연동된다. */
const DP_DOW = ["일", "월", "화", "수", "목", "금", "토"];
let dpPop = null, dpTarget = null, dpYM = null;
function dpEnsure() {
  if (dpPop) return;
  dpPop = document.createElement("div");
  dpPop.id = "dpPop"; dpPop.className = "dp-pop";
  document.body.appendChild(dpPop);
}
function dpValidISO(v) { return /^\d{4}-\d{2}-\d{2}$/.test(v || "") ? v : ""; }
function dpOpen(input) {
  dpEnsure();
  dpTarget = input;
  const cur = dpValidISO(input.value) || todayISO();
  dpYM = cur.slice(0, 7);
  dpRender();
  const r = input.getBoundingClientRect();
  const w = 240, h = 286;
  let left = r.left, top = r.bottom + 4;
  if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);   // 아래 공간 부족 시 위로
  dpPop.style.left = Math.max(8, left) + "px";
  dpPop.style.top = top + "px";
  dpPop.classList.add("on");
}
function dpRender() {
  const sel = dpValidISO(dpTarget && dpTarget.value);
  const [y, m] = dpYM.split("-").map(Number);
  const first = new Date(y, m - 1, 1).getDay();
  const nd = new Date(y, m, 0).getDate();
  const today = todayISO();
  let h = `<div class="dp-head"><button data-dpnav="-1" type="button">◀</button>
      <span>${y}년 ${m}월</span><button data-dpnav="1" type="button">▶</button></div>
    <div class="dp-dow">${DP_DOW.map(d => `<span>${d}</span>`).join("")}</div><div class="dp-days">`;
  for (let i = 0; i < first; i++) h += "<span></span>";
  for (let d = 1; d <= nd; d++) {
    const iso = `${dpYM}-${String(d).padStart(2, "0")}`;
    const cls = [iso === sel ? "sel" : "", iso === today ? "today" : ""].filter(Boolean).join(" ");
    h += `<button type="button" class="${cls}" data-dpd="${iso}">${d}</button>`;
  }
  h += `</div><div class="dp-foot"><button type="button" data-dpd="${today}">오늘</button>
    <button type="button" data-dpclear>지움</button></div>`;
  dpPop.innerHTML = h;
}
function dpSet(v) {
  if (dpTarget) {
    dpTarget.value = v;
    dpTarget.dispatchEvent(new Event("input", { bubbles: true }));
    dpTarget.dispatchEvent(new Event("change", { bubbles: true }));
  }
  dpClose();
}
function dpClose() { if (dpPop) dpPop.classList.remove("on"); dpTarget = null; }
document.addEventListener("click", e => {
  const inp = e.target.closest("input.datepick");
  if (inp) { dpOpen(inp); e.stopPropagation(); return; }
  if (dpPop && e.target.closest("#dpPop")) {
    const nav = e.target.closest("[data-dpnav]");
    if (nav) {
      const [y, m] = dpYM.split("-").map(Number);
      const d = new Date(y, m - 1 + Number(nav.dataset.dpnav), 1);
      dpYM = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      dpRender(); return;
    }
    const day = e.target.closest("[data-dpd]");
    if (day) { dpSet(day.dataset.dpd); return; }
    if (e.target.closest("[data-dpclear]")) { dpSet(""); return; }
    return;
  }
  dpClose();
});
window.addEventListener("resize", dpClose);

/* ══ 대시보드 ══════════════════════════ */
const DONUT_PALETTE = ["#8B5E34", "#C4841D", "#3E7A50", "#5B7C99", "#B0ADA6", "#C2372C", "#9C6644", "#7A9E7E", "#D4A24C", "#6B7280"];
async function loadDash() {
  const d = await api("/api/dashboard");
  const k = d.kpi;
  const lowTotal = k.low_raw + k.low_sub;
  const admin = canM("prod");           // 생산·출고·재고 금액 열람
  const canLabor = canM("labor");       // 노무비 열람

  // ── 1) 오늘 입력 상태 배너
  const dow = dowOf(d.today);
  if (d.today_entered) {
    $("dashTodayBanner").innerHTML = `<div class="today-banner done">
      <span class="tb-emo">✅</span>
      <div class="tb-txt"><b>오늘 (${d.today.slice(5)}, ${dow}) 입력 완료</b>
        <div class="tb-sub">오늘 생산 기록이 등록되어 있습니다</div></div>
      <button class="btn sm" data-goscr="entry">일일 입력 열기</button></div>`;
  } else {
    const lastTxt = d.last_day ? `마지막 입력 ${d.last_day.slice(5)} (${dowOf(d.last_day)})` : "아직 입력 기록이 없습니다";
    $("dashTodayBanner").innerHTML = `<div class="today-banner wait">
      <span class="tb-emo">📝</span>
      <div class="tb-txt"><b>오늘 (${d.today.slice(5)}, ${dow}) 아직 입력 전이에요</b>
        <div class="tb-sub">${lastTxt}</div></div>
      <button class="btn primary sm" data-goscr="entry">＋ 오늘 입력하기</button></div>`;
  }

  // ── KPI 그리드 (2·3) 달성률·불량률·완제품재고 + (4) 금액(admin) + (6) 가동률
  const a = d.ach, good = a.prod - a.defect;
  const rate = a.plan > 0 ? Math.round(a.prod / a.plan * 100) : null;
  const dRate = a.prod > 0 ? (a.defect / a.prod * 100) : 0;
  const util = d.util;
  const kpis = [];
  kpis.push(`<div class="kpi ${lowTotal ? "alert" : ""}"><div class="lbl"><span class="ki">📦</span>부족 자재</div>
    <div class="val num">${lowTotal}<small>종</small></div>
    <div class="delta">원재료 ${k.low_raw} · 부재료 ${k.low_sub}</div></div>`);
  kpis.push(`<div class="kpi"><div class="lbl"><span class="ki">🍩</span>최근 기록일 생산</div>
    <div class="val num">${NF(k.last_prod)}<small>개</small></div>
    <div class="delta num">${d.lastday.date || "—"}</div></div>`);
  kpis.push(`<div class="kpi"><div class="lbl"><span class="ki">🚚</span>최근 기록일 출고</div>
    <div class="val num">${NF(k.last_ship)}<small>개</small></div>
    <div class="delta">${d.lastday.ship.length}건</div></div>`);
  kpis.push(`<div class="kpi ${rate != null && rate < 100 ? "warn-kpi" : (rate != null ? "ok-kpi" : "")}" title="달성률 = 생산 ${NF(a.prod)} ÷ 계획 ${NF(a.plan)} · 불량률 = 불량 ${NF(a.defect)} ÷ 생산 ${NF(a.prod)}"><div class="lbl"><span class="ki">🎯</span>달성률 <span class="sub" style="font-weight:500">최근일</span></div>
    <div class="val num">${rate != null ? rate + "<small>%</small>" : "—"}</div>
    <div class="delta">양품 ${NF(good)} · 불량률 ${dRate.toFixed(1)}%</div></div>`);
  kpis.push(`<div class="kpi ${d.prod_low_cnt ? "warn-kpi" : ""}"><div class="lbl"><span class="ki">🏭</span>완제품 재고</div>
    <div class="val num">${NF(Math.round(d.prod_stock_qty))}<small>개</small></div>
    <div class="delta">${d.prod_low_cnt ? "부족 " + d.prod_low_cnt + "종" : "안전재고 이상"}${admin && d.prod_stock_amt != null ? " · ₩" + NF(Math.round(d.prod_stock_amt)) : ""}</div></div>`);
  if (admin && d.money) {
    kpis.push(`<div class="kpi"><div class="lbl"><span class="ki">💰</span>이번달 생산금액 <span class="sub" style="font-weight:500">${d.money.label}</span></div>
      <div class="val num">₩${NF(Math.round(d.money.prod))}</div>
      <div class="delta">출고금액 ₩${NF(Math.round(d.money.ship))}</div></div>`);
  }
  kpis.push(`<div class="kpi" title="가동률 = 실가동 시간 합 ÷ 정상가동 시간 합 (물리 라인 기준 · 공정은 대표 라인으로 묶어 최대값)"><div class="lbl"><span class="ki">👷</span>가동률 <span class="sub" style="font-weight:500">최근일</span></div>
    <div class="val num">${util.rate != null ? util.rate + "<small>%</small>" : "—"}</div>
    <div class="delta">투입 ${NF(util.headcount)}명 · ${util.lines}라인${canLabor && util.labor != null ? " · 노무비 ₩" + NF(Math.round(util.labor)) : ""}</div></div>`);
  kpis.push(`<div class="kpi"><div class="lbl"><span class="ki">🗓️</span>누적 기록</div>
    <div class="val num">${NF(k.days)}<small>일</small></div>
    <div class="delta">활성 제품 ${k.products}종</div></div>`);
  $("dashKpis").innerHTML = kpis.join("");

  // ── 8) 이번주 생산 TOP 제품 (썸네일 랭킹)
  $("topLbl").textContent = d.week ? `${d.week[0].slice(5)} ~ ${d.week[1].slice(5)}` : "";
  const maxTop = Math.max(1, ...(d.top_prod || []).map(r => r.qty));
  const medals = ["🥇", "🥈", "🥉"];
  $("dashTop").innerHTML = (d.top_prod && d.top_prod.length) ? d.top_prod.map((r, i) => `
    <div class="rank-row">
      <span class="rk ${i < 3 ? "top" : ""}">${medals[i] || (i + 1)}</span>
      ${r.image ? `<img class="rthumb" src="/image/${encodeURIComponent(r.image)}">`
        : `<span class="rthumb ph">🍞</span>`}
      <div style="flex:1; min-width:0">
        <div class="rname">${esc(r.name)}</div>
        <div class="rbar" style="width:${Math.max(6, Math.round(r.qty / maxTop * 100))}%"></div>
      </div>
      <span class="rqty num">${NF(r.qty)}개</span>
    </div>`).join("") : '<div class="auto">이번주 생산 기록이 없습니다</div>';

  const cnt = $("navLowCnt");
  cnt.style.display = lowTotal ? "" : "none"; cnt.textContent = lowTotal;
  // LOT 관리 배지 = 임박(≤7일)+만료 LOT 수 (대시보드 로드 시에도 갱신)
  const lw = d.lot_warn || 0;
  $("navLotCnt").style.display = lw > 0 ? "" : "none"; $("navLotCnt").textContent = lw;

  const labels = d.trend.map(r => r.date.slice(5).replace("-", "/"));
  lineChart($("dashTrend"), {
    labels,
    series: [
      { name: "생산", values: d.trend.map(r => r.prod), color: "#121212", fill: "rgba(18,18,18,.06)" },
      { name: "출고", values: d.trend.map(r => r.ship), color: "#B0ADA6" },
    ],
    zoom: true,
    onClick: i => { $("dashTrendD").innerHTML =
      `<b>${d.trend[i].date} (${dowOf(d.trend[i].date)})</b> — 생산 <b>${NF(d.trend[i].prod)}개</b> · 출고 <b>${NF(d.trend[i].ship)}개</b>`; },
  });

  $("lowBody").innerHTML = d.low.length ? d.low.map(r => `
    <tr><td><b>${esc(r.name)}</b></td>
      <td><span class="chip cat">${r.kind === "raw" ? "원재료" : "부재료"}</span></td>
      <td class="r" style="color:var(--crit); font-weight:700">${NF(r.stock)} ${esc(r.unit)}</td>
      <td class="r">${r.safety_stock > 0 ? NF(r.safety_stock) : '<span class="auto">미설정</span>'}</td>
      <td>${esc(r.order_date) || "—"}</td><td class="auto">${r.date}</td></tr>`).join("")
    : `<tr><td colspan="6" class="auto">부족 자재가 없습니다. 안전재고는 기준정보에서 설정합니다.</td></tr>`;

  $("expLbl").textContent = (d.lot_expired ? `만료 ${d.lot_expired} · ` : "") + (d.lot_date ? `${d.lot_date} 기준` : "");
  $("expList").innerHTML = d.expiry.length ? d.expiry.map(r => {
    const expired = r.days_left != null && r.days_left < 0;
    const urgent = r.days_left != null && r.days_left <= 7;
    const badge = expired
      ? `<span class="chip crit">만료 D+${Math.abs(r.days_left)}</span>`
      : `<span class="q num" style="color:${urgent ? "var(--crit)" : "var(--ink)"}">${NF(r.qty)}개 · D-${r.days_left}</span>`;
    return `<div class="feed-item"><span>${expired ? "⚠️ " : ""}<b>${esc(r.name)}</b>
      <span class="auto" style="font-size:11.5px"> ${r.made_date ? "생산 " + r.made_date.slice(5) : ""} · 소비 ${r.expiry.slice(5)}${expired ? " · " + NF(r.qty) + "개" : ""}</span></span>
      ${badge}</div>`;
  }).join("") : '<div class="auto">소비기한 데이터 없음</div>';

  // ── 5) 거래처별 출고 비중 (도넛)
  const sp = d.ship_partner || [];
  $("partnerLbl").textContent = d.month_label ? `${d.month_label} 기준` : "";
  if (sp.length) {
    const top = sp.slice(0, 9);
    const etcQty = sp.slice(9).reduce((s, r) => s + r.qty, 0);
    if (etcQty > 0) top.push({ partner: "기타", qty: etcQty });
    const totQ = top.reduce((s, r) => s + r.qty, 0) || 1;
    mkChart($("dashPartner"), {
      type: "doughnut",
      data: { labels: top.map(r => r.partner), datasets: [{ data: top.map(r => r.qty),
        backgroundColor: top.map((_, i) => DONUT_PALETTE[i % DONUT_PALETTE.length]),
        borderColor: "#fff", borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "60%",
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => ` ${c.label}: ${NF(c.parsed)}개 (${Math.round(c.parsed / totQ * 100)}%)` } } } },
    });
    $("dashPartnerLeg").innerHTML = top.map((r, i) => `<div class="dl-row">
      <span class="dl-dot" style="background:${DONUT_PALETTE[i % DONUT_PALETTE.length]}"></span>
      <span class="dl-name">${esc(r.partner)}</span>
      <span class="dl-val num">${NF(r.qty)}개 · ${Math.round(r.qty / totQ * 100)}%</span></div>`).join("");
  } else {
    if (CHARTS.dashPartner) { CHARTS.dashPartner.destroy(); delete CHARTS.dashPartner; }
    $("dashPartnerLeg").innerHTML = '<div class="auto">이번달 출고 기록이 없습니다</div>';
  }

  // ── 3) 완제품 재고 부족
  $("prodLowLbl").textContent = d.prod_low.length ? `${d.prod_low.length}종` : "";
  $("dashProdLow").innerHTML = d.prod_low.length ? d.prod_low.map(r => `
    <div class="plow-row">
      ${r.image ? `<img class="pthumb" src="/image/${encodeURIComponent(r.image)}">`
        : `<span class="pthumb ph">🍞</span>`}
      <span class="pname">${esc(r.name)}</span>
      <span class="pst num">${NF(Math.round(r.stock))}개</span>
      <span class="psafe num">/ 안전 ${NF(r.safety)}</span>
    </div>`).join("") : '<div class="auto">안전재고 미달 완제품이 없습니다. (안전재고는 기준정보 제품에서 설정)</div>';

  $("lastdayLbl").textContent = d.lastday.date ? `${d.lastday.date} (${dowOf(d.lastday.date)})` : "";
  const feed = [];
  d.lastday.prod.slice(0, 6).forEach(r => feed.push(
    `<div class="feed-item"><span><b>${esc(r.name)}</b> 생산</span><span class="q in num">+${NF(r.prod_qty)}</span></div>`));
  d.lastday.ship.slice(0, 6).forEach(r => feed.push(
    `<div class="feed-item"><span><b>${esc(r.name)}</b> 출고${r.partner ? " · " + esc(r.partner) : ""}</span><span class="q out num">−${NF(r.qty)}</span></div>`));
  $("lastdayFeed").innerHTML = feed.join("") || '<div class="auto">기록 없음</div>';
}

/* ══ 생산 현황 ═════════════════════════ */
const prodState = { mode: "d", date: "", dates: [], sel: -1 };
$("prodTabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-pt]"); if (!b) return;
  document.querySelectorAll("#prodTabs button").forEach(x => x.classList.toggle("on", x === b));
  prodState.mode = b.dataset.pt; prodState.sel = -1; loadProd();
});
$("prodPrev").onclick = () => prodNav(-1);
$("prodNext").onclick = () => prodNav(1);

// 날짜 라벨 클릭 → 달력 팝업 (기록 있는 날 초록 점)
const prodCal = Calendar("pcal", d => {
  prodState.date = d;
  prodState.sel = -1;
  hideProdCal();
  loadProd();
});
function hideProdCal() { $("prodCalPop").style.display = "none"; }
$("prodLbl").addEventListener("click", () => {
  const pop = $("prodCalPop");
  if (pop.style.display === "none") {
    prodCal.ym = (prodState.date || todayISO()).slice(0, 7);
    prodCal.sel = prodState.date;
    prodCal.render();
    pop.style.display = "";
  } else hideProdCal();
});
document.addEventListener("click", e => {
  if (!e.target.closest("#prodCalPop") && !e.target.closest("#prodLbl")) hideProdCal();
});
function prodNav(k) {
  const s = prodState;
  if (s.mode === "d") {
    const i = s.dates.indexOf(s.date);
    const ni = i < 0 ? s.dates.length - 1 : i + k;
    if (ni >= 0 && ni < s.dates.length) s.date = s.dates[ni];
  } else {
    const d = new Date(s.date + "T00:00:00");
    if (s.mode === "w") d.setDate(d.getDate() + 7 * k);
    if (s.mode === "m") d.setMonth(d.getMonth() + k);
    if (s.mode === "y") d.setFullYear(d.getFullYear() + k);
    s.date = fmtISO(d);
  }
  s.sel = -1; loadProd();
}
// 생산현황 KPI 요약 칩 (기간 탭마다 갱신)
function prodKpis(items) {
  $("prodKpis").innerHTML = items
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([t, v, c]) => `<span class="pchip num" style="${c ? "color:" + c + "; border-color:" + c : ""}">${t} <b style="margin-left:4px">${v}</b></span>`).join("");
}
async function loadProd() {
  const s = prodState;
  if (s.mode !== "d" && !s.date) s.date = todayISO();
  const d = await api(`/api/prodstatus?mode=${s.mode}&date=${s.date}`);
  const box = $("prodChartBox"), det = $("prodChartD");
  if (s.mode === "d") {
    s.dates = d.dates; s.date = d.date;
    $("prodLbl").textContent = `${d.date} (${dowOf(d.date)})`;
    box.style.display = "none"; det.style.display = "none";
    renderDailyBars(d.rows);
    $("prodHead").innerHTML = `<tr><th>제품명</th><th class="r">계획</th><th class="r">생산</th>
      <th class="r">달성률</th><th class="r">불량</th><th class="r">양품</th><th class="r">출고</th><th class="r">생산금액(원)</th></tr>`;
    let tp = 0, tq = 0, td = 0, ts = 0, ta = 0;
    $("prodBody").innerHTML = d.rows.map(r => {
      const good = r.prod_qty - r.defect_qty;
      const amt = r.amount || 0;   // 거래처 분배 반영 생산금액 (서버 계산)
      tp += r.plan_qty; tq += r.prod_qty; td += r.defect_qty; ts += r.ship; ta += amt;
      const nameCell = r.product_id
        ? `<button class="uselink" data-pstat-pid="${r.product_id}" title="LOT·소비기한·거래처 상세 보기"><b>${esc(r.name)}</b></button>`
        : `<b>${esc(r.name)}</b>`;
      return `<tr><td>${nameCell}</td><td class="r">${r.plan_qty ? NF(r.plan_qty) : "—"}</td>
        <td class="r">${NF(r.prod_qty)}</td><td class="r">${PCT(r.prod_qty, r.plan_qty)}</td>
        <td class="r">${NF(r.defect_qty)}${r.defect_reason ? `<div class="auto" style="font-size:10.5px; color:#98670F;">🏷 ${esc(r.defect_reason)}</div>` : ""}</td>
        <td class="r">${NF(good)}</td>
        <td class="r">${NF(r.ship)}</td><td class="r">${r.priced ? NF(amt) : '<span class="auto">단가 미설정</span>'}</td></tr>`;
    }).join("") + (d.rows.length ? `<tr style="font-weight:700"><td>합계</td><td class="r">${tp ? NF(tp) : "—"}</td>
      <td class="r">${NF(tq)}</td><td class="r">${PCT(tq, tp)}</td><td class="r">${NF(td)}</td>
      <td class="r">${NF(tq - td)}</td><td class="r">${NF(ts)}</td><td class="r">${ta ? NF(ta) : "—"}</td></tr>`
      : `<tr><td colspan="8" class="auto">이 날짜의 생산 기록이 없습니다</td></tr>`);
    const rate = tp > 0 ? Math.round(tq / tp * 100) : null;
    prodKpis([
      ["🗂 계획", tp ? NF(tp) : "—"],
      ["🍩 생산", NF(tq)],
      ["🎯 달성률", rate != null ? rate + "%" : "—", rate != null && rate < 100 ? "#B45309" : (rate != null ? "var(--ok)" : "")],
      ["🚫 불량", `${NF(td)} (${tq > 0 ? (td / tq * 100).toFixed(1) : 0}%)`, td > 0 ? "var(--crit)" : ""],
      ["🚚 출고", NF(ts)],
      ta ? ["💰 생산금액", NF(Math.round(ta)) + "원"] : null,
    ].filter(Boolean));
    applyProdFilter();
    loadProdReport();
    return;
  }
  $("prodAbars").style.display = "none";
  box.style.display = ""; det.style.display = "";
  if (s.mode === "w") {
    $("prodLbl").textContent = `${d.start} ~ ${d.end}`;
    const prods = [...new Set(d.rows.map(r => r.name))];
    const byDay = d.days.map(day => d.rows.filter(r => r.date === day).reduce((s2, r) => s2 + r.q, 0));
    barChart($("prodChart"), {
      labels: d.days.map(x => DOW[new Date(x + "T00:00:00").getDay()]),
      series: [{ values: byDay, color: "#121212" }], sel: s.sel,
      tipHtml: i => `<span class="tt">${d.days[i]}</span>생산 <b>${NF(byDay[i])}개</b>`,
      onClick: i => { s.sel = s.sel === i ? -1 : i; loadProdWeekDetail(d, i); },
    });
    $("prodHead").innerHTML = `<tr><th>제품명</th>${d.days.map(x =>
      `<th class="r">${DOW[new Date(x + "T00:00:00").getDay()]}</th>`).join("")}<th class="r">주간합계</th></tr>`;
    $("prodBody").innerHTML = prods.map(p => {
      const vals = d.days.map(day => (d.rows.find(r => r.name === p && r.date === day) || {}).q || 0);
      return `<tr><td><b>${esc(p)}</b></td>${vals.map(v => `<td class="r">${v ? NF(v) : "·"}</td>`).join("")}
        <td class="r" style="font-weight:700">${NF(vals.reduce((a, b) => a + b, 0))}</td></tr>`;
    }).join("") + (prods.length ? `<tr style="font-weight:700"><td>합계</td>${byDay.map(v =>
      `<td class="r">${NF(v)}</td>`).join("")}<td class="r">${NF(byDay.reduce((a, b) => a + b, 0))}</td></tr>`
      : `<tr><td colspan="9" class="auto">이 주의 생산 기록이 없습니다</td></tr>`);
    const wTot = byDay.reduce((a, b) => a + b, 0), wDays = byDay.filter(v => v > 0).length;
    prodKpis([
      ["🍩 주간 생산", NF(wTot)],
      ["📦 제품", prods.length + "종"],
      ["🗓 생산일", wDays + "일"],
      wDays ? ["📊 일평균", NF(Math.round(wTot / wDays))] : null,
    ].filter(Boolean));
  } else if (s.mode === "m") {
    $("prodLbl").textContent = d.month.replace("-", "년 ") + "월";
    barChart($("prodChart"), {
      labels: d.rows.map(r => String(Number(r.date.slice(8)))),
      series: [{ values: d.rows.map(r => r.prod), color: "#121212" }],
      plan: d.rows.map(r => r.plan), sel: s.sel,
      tipHtml: i => `<span class="tt">${d.rows[i].date}</span>생산 <b>${NF(d.rows[i].prod)}</b><br>출고 <b>${NF(d.rows[i].ship)}</b>`,
      onClick: i => { s.sel = s.sel === i ? -1 : i;
        const r = d.rows[i];
        $("prodChartD").innerHTML = `<b>${r.date} (${dowOf(r.date)})</b> — 생산 <b>${NF(r.prod)}개</b>
          · 불량 <b>${NF(r.defect)}개</b> · 출고 <b>${NF(r.ship)}개</b>
          ${r.amount ? ` · 생산금액 <b>${NF(r.amount)}원</b>` : ""}`;
        loadProd(); },
    });
    $("prodHead").innerHTML = `<tr><th>일자</th><th>요일</th><th class="r">생산수량</th>
      <th class="r">불량</th><th class="r">출고수량</th><th class="r">생산금액(원)</th></tr>`;
    let tq = 0, td2 = 0, ts = 0, ta = 0;
    $("prodBody").innerHTML = d.rows.map(r => {
      tq += r.prod; td2 += r.defect; ts += r.ship; ta += r.amount || 0;
      return `<tr><td class="r">${Number(r.date.slice(8))}</td><td>${dowOf(r.date)}</td>
        <td class="r">${NF(r.prod)}</td><td class="r">${NF(r.defect)}</td>
        <td class="r">${NF(r.ship)}</td><td class="r">${r.amount ? NF(r.amount) : "—"}</td></tr>`;
    }).join("") + (d.rows.length ? `<tr style="font-weight:700"><td></td><td>월계</td><td class="r">${NF(tq)}</td>
      <td class="r">${NF(td2)}</td><td class="r">${NF(ts)}</td><td class="r">${ta ? NF(ta) : "—"}</td></tr>`
      : `<tr><td colspan="6" class="auto">이 달의 생산 기록이 없습니다</td></tr>`);
    const mPlan = d.rows.reduce((a, r) => a + (r.plan || 0), 0);
    const mRate = mPlan > 0 ? Math.round(tq / mPlan * 100) : null;
    prodKpis([
      ["🍩 월간 생산", NF(tq)],
      mRate != null ? ["🎯 달성률", mRate + "%", mRate < 100 ? "#B45309" : "var(--ok)"] : null,
      ["🚫 불량", `${NF(td2)} (${tq > 0 ? (td2 / tq * 100).toFixed(1) : 0}%)`, td2 > 0 ? "var(--crit)" : ""],
      ["🚚 출고", NF(ts)],
      ta ? ["💰 생산금액", NF(Math.round(ta)) + "원"] : null,
    ].filter(Boolean));
  } else {
    $("prodLbl").textContent = d.year + "년";
    barChart($("prodChart"), {
      labels: d.rows.map(r => Number(r.ym.slice(5)) + "월"),
      series: [{ values: d.rows.map(r => r.prod), color: "#121212" }], sel: s.sel,
      fmtAxis: v => (v >= 10000 ? (v / 10000).toFixed(0) + "만" : NF(Math.round(v))),
      tipHtml: i => `<span class="tt">${d.rows[i].ym}</span>생산 <b>${NF(d.rows[i].prod)}</b><br>출고 <b>${NF(d.rows[i].ship)}</b>`,
      onClick: i => { s.sel = s.sel === i ? -1 : i;
        const r = d.rows[i], pv = i > 0 ? d.rows[i - 1].prod : 0;
        $("prodChartD").innerHTML = `<b>${r.ym}</b> — 생산 <b>${NF(r.prod)}개</b> · 불량 <b>${NF(r.defect)}개</b>
          · 출고 <b>${NF(r.ship)}개</b>${pv ? ` · 전월 대비 <b>${((r.prod - pv) / pv * 100).toFixed(1)}%</b>` : ""}`;
        loadProd(); },
    });
    $("prodHead").innerHTML = `<tr><th>월</th><th class="r">생산수량</th><th class="r">불량</th>
      <th class="r">출고수량</th><th class="r">생산금액(원)</th><th class="r">전월 대비</th></tr>`;
    let tq = 0, td2 = 0, ts = 0, ta = 0;
    $("prodBody").innerHTML = d.rows.map((r, i) => {
      tq += r.prod; td2 += r.defect; ts += r.ship; ta += r.amount || 0;
      const pv = i > 0 ? d.rows[i - 1].prod : 0;
      const diff = pv ? ((r.prod - pv) / pv * 100) : null;
      return `<tr><td><b>${Number(r.ym.slice(5))}월</b></td><td class="r">${NF(r.prod)}</td>
        <td class="r">${NF(r.defect)}</td><td class="r">${NF(r.ship)}</td>
        <td class="r">${r.amount ? NF(r.amount) : "—"}</td>
        <td class="r" style="color:${diff == null ? "inherit" : diff >= 0 ? "var(--ok)" : "var(--crit)"}">
          ${diff == null ? "—" : (diff >= 0 ? "+" : "") + diff.toFixed(1) + "%"}</td></tr>`;
    }).join("") + (d.rows.length ? `<tr style="font-weight:700"><td>연간 누계</td><td class="r">${NF(tq)}</td>
      <td class="r">${NF(td2)}</td><td class="r">${NF(ts)}</td><td class="r">${ta ? NF(ta) : "—"}</td><td></td></tr>`
      : `<tr><td colspan="6" class="auto">기록이 없습니다</td></tr>`);
    prodKpis([
      ["🍩 연간 생산", NF(tq)],
      ["🚫 불량", `${NF(td2)} (${tq > 0 ? (td2 / tq * 100).toFixed(1) : 0}%)`, td2 > 0 ? "var(--crit)" : ""],
      ["🚚 출고", NF(ts)],
      ta ? ["💰 생산금액", NF(Math.round(ta)) + "원"] : null,
    ].filter(Boolean));
  }
  applyProdFilter();
  loadProdReport();
}
/* 생산현황 표 검색 (생산실적 + 보고서 섹션 전체, 합계 행은 항상 표시) */
function applyProdFilter() {
  const q = ($("prodFilter").value || "").trim().toLowerCase();
  document.querySelectorAll("#scr-prod tbody tr").forEach(tr => {
    const isTotal = /합계|월계|누계/.test(tr.cells[0] ? tr.cells[0].textContent + (tr.cells[1] ? tr.cells[1].textContent : "") : "");
    tr.style.display = !q || isTotal || tr.textContent.toLowerCase().includes(q) ? "" : "none";
  });
}
$("prodFilter").addEventListener("input", applyProdFilter);

/* 표 → CSV(엑셀) 다운로드 — 검색으로 숨긴 행은 제외, 한글 엑셀용 BOM 포함 */
function tableToCsv(headEl, bodyEl, filename) {
  const cell = td => `"${td.textContent.replace(/\s+/g, " ").trim().replace(/"/g, '""')}"`;
  const lines = [];
  headEl.querySelectorAll("tr").forEach(tr => lines.push([...tr.cells].map(cell).join(",")));
  bodyEl.querySelectorAll("tr").forEach(tr => {
    if (tr.style.display === "none") return;
    lines.push([...tr.cells].map(cell).join(","));
  });
  if (lines.length <= 1) return toast("내보낼 데이터가 없습니다");
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
  toast(`📄 ${filename} 저장됨`);
}
const csvName = (prefix, label) => `${prefix}_${label.replace(/[\\/:*?"<>| ]/g, "_")}.csv`;
$("prodCsv").onclick = () => tableToCsv($("prodHead"), $("prodBody"), csvName("생산현황", $("prodLbl").textContent));
// 생산실적(섹션1) 제품명 클릭 → 재고현황과 동일한 LOT 상세 팝업
$("prodBody").addEventListener("click", e => {
  const b = e.target.closest("[data-pstat-pid]");
  if (b) { e.preventDefault(); openStockLotByPid(+b.dataset.pstatPid); }
});

/* 생산 현황 보고서 섹션 2~5 (원부자재/인원·가동/완제품 재고/특이사항) */
function psec(no, title, sub, bodyHtml, empty) {
  return `<div class="psec ${empty ? "closed" : ""}">
    <div class="psec-h" data-ptoggle>${no}. ${title} <span class="sub">${sub || ""}</span></div>
    <div class="psec-b">${bodyHtml}</div></div>`;
}
// 생산현황 재고 표 — 소비기한 셀 + 제품 클릭 상세용
let PR_STOCK = [];
function ddaySpan(expiry) {
  // 소비기한을 D-day 배지로 (≤7 빨강, ≤14 주황, 그 외 회색)
  if (!expiry) return '<span class="auto">기한미상</span>';
  const days = Math.round((new Date(expiry + "T00:00:00") - new Date(new Date().toDateString())) / 86400000);
  const col = days < 0 ? "var(--crit)" : days <= 7 ? "var(--crit)" : days <= 14 ? "var(--warn)" : "var(--ink)";
  const lbl = days < 0 ? `만료 D+${-days}` : `D-${days}`;
  return `<span style="color:${col}${days <= 14 ? ";font-weight:700" : ""}">${expiry.slice(2)} <span class="auto" style="color:inherit;font-weight:inherit">${lbl}</span></span>`;
}
function stockExpCell(r) {
  if (!r.exp_min) return '<span class="auto">—</span>';
  const one = ddaySpan(r.exp_min);
  // 여러 소비기한이 섞이면 '외 N종' 힌트
  const n = (r.lots || []).filter(l => l.expiry).map(l => l.expiry).filter((v, i, a) => a.indexOf(v) === i).length;
  return one + (n > 1 ? ` <span class="auto">외 ${n - 1}</span>` : "");
}
async function loadProdReport() {
  const s = prodState;
  const d = await api(`/api/prodreport?mode=${s.mode}&date=${s.date}`);
  const [a, b] = d.range;
  const rangeLbl = a === b ? a : `${a} ~ ${b}`;

  // 2) 원부자재 소모
  let mBody;
  if (!d.materials.length) {
    mBody = '<div class="auto">기간 내 자재 사용 기록이 없습니다</div>';
  } else {
    let tU = 0, tA = 0;
    mBody = `<div class="tbl-wrap"><table>
      <thead><tr><th>자재명</th><th>단위</th><th class="r">기초재고</th><th class="r">기간입고</th>
        <th class="r">기간사용</th><th class="r">기말재고</th><th class="r">단가(원)</th><th class="r">사용금액(원)</th></tr></thead>
      <tbody class="num">` + d.materials.map(r => {
        const amt = r.used * (r.unit_price || 0);
        tU += r.used; tA += amt;
        return `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.unit)}</td>
          <td class="r">${NF(r.open)}</td><td class="r">${NF(r.inq)}</td>
          <td class="r" style="font-weight:700">${NF(r.used)}</td><td class="r">${NF(r.close)}</td>
          <td class="r">${r.unit_price ? NF(r.unit_price) : "—"}</td>
          <td class="r">${r.unit_price ? NF(Math.round(amt)) : "—"}</td></tr>`;
      }).join("") + `<tr style="font-weight:700"><td>합계</td><td></td><td></td><td></td>
        <td class="r">${NF(Math.round(tU * 100) / 100)}</td><td></td><td></td>
        <td class="r">${tA ? NF(Math.round(tA)) : "—"}</td></tr>
      </tbody></table></div>`;
  }

  // 3) 인원·가동 — 같은 라인명 = 한 물리 라인 (공정 행들을 라인으로 묶어 집계)
  //    라인 실가동/정상가동 = 공정 중 최댓값 (공정이 몇 개든 라인은 하루 8시간 돈 것)
  let sBody;
  if (!d.staffing.length) {
    sBody = '<div class="auto">기간 내 인원·가동 기록이 없습니다 — 일일 입력 &gt; 인원·가동에서 기록하면 여기 집계됩니다</div>';
  } else if (s.mode === "d") {
    const byL = {};
    d.staffing.forEach(r => (byL[r.line] = byL[r.line] || []).push(r));
    sBody = `<div class="tbl-wrap"><table>
      <thead><tr><th>라인 / 공정</th><th class="r">인원</th><th class="r">실가동(h)</th><th class="r">가동률</th><th>정지사유</th><th class="r">노무비(원)</th></tr></thead>
      <tbody class="num">` + Object.entries(byL).map(([line, rs]) => {
        const head = rs.reduce((a, r) => a + r.headcount, 0);
        const wh = Math.max(...rs.map(r => r.work_hours || 0));
        const std = Math.max(...rs.map(r => r.std_hours || 0));
        const labor = rs.reduce((a, r) => a + (r.labor || 0), 0);
        const hasLabor = rs.some(r => r.labor != null);
        const stops = rs.map(r => r.stop_reason).filter(Boolean).join(" · ");
        if (rs.length === 1) {   // 공정 1개 라인은 한 줄로
          const r = rs[0];
          return `<tr><td><b>${esc(line)}</b>${r.process ? ` <span class="auto" style="font-size:11px">/ ${esc(r.process)}</span>` : ""}</td>
            <td class="r">${NF(r.headcount)}</td><td class="r">${NF(r.work_hours)}</td>
            <td class="r">${PCT(r.work_hours, r.std_hours)}</td>
            <td>${esc(r.stop_reason) || "—"}</td>
            <td class="r">${r.labor != null ? NF(Math.round(r.labor)) : "—"}</td></tr>`;
        }
        return `<tr style="font-weight:700; background:var(--bg)">
          <td>${esc(line)} <span class="auto" style="font-weight:500; font-size:11px">공정 ${rs.length}개</span></td>
          <td class="r">${NF(head)}</td><td class="r">${NF(wh)}</td>
          <td class="r">${PCT(wh, std)}</td>
          <td style="font-weight:500">${esc(stops) || "—"}</td>
          <td class="r">${hasLabor ? NF(Math.round(labor)) : "—"}</td></tr>` +
          rs.map(r => `<tr><td class="auto" style="padding-left:24px">└ ${esc(r.process || "공정 미지정")}</td>
            <td class="r">${NF(r.headcount)}</td><td class="r">${NF(r.work_hours)}</td>
            <td class="r auto">${PCT(r.work_hours, r.std_hours)}</td>
            <td class="auto">${esc(r.stop_reason) || ""}</td>
            <td class="r auto">${r.labor != null ? NF(Math.round(r.labor)) : "—"}</td></tr>`).join("");
      }).join("") + "</tbody></table></div>";
  } else {
    const by = {};
    d.staffing.forEach(r => {
      const g = by[r.line] = by[r.line] || { dates: {}, head: 0, labor: 0, procs: new Set() };
      const dd = g.dates[r.date] = g.dates[r.date] || { wh: 0, std: 0 };
      dd.wh = Math.max(dd.wh, r.work_hours || 0);     // 같은 날 공정들 = 라인 실가동은 최댓값
      dd.std = Math.max(dd.std, r.std_hours || 0);
      g.head += r.headcount; g.labor += r.labor || 0;  // 인원·노무비는 공정 합
      if (r.process) g.procs.add(r.process);
    });
    sBody = `<div class="tbl-wrap"><table>
      <thead><tr><th>라인</th><th class="r">기록일수</th><th class="r">일평균 인원</th>
        <th class="r">총 가동(h)</th><th class="r">평균 가동률</th><th class="r">노무비(원)</th></tr></thead>
      <tbody class="num">` + Object.entries(by).map(([line, g]) => {
        const days = Object.keys(g.dates).length;
        const hours = Object.values(g.dates).reduce((a, x) => a + x.wh, 0);
        const std = Object.values(g.dates).reduce((a, x) => a + x.std, 0);
        return `<tr><td><b>${esc(line)}</b>${g.procs.size > 1 ? ` <span class="auto" style="font-size:11px">공정 ${g.procs.size}개</span>` : ""}</td>
        <td class="r">${days}</td><td class="r">${NF(Math.round(g.head / days * 10) / 10)}</td>
        <td class="r">${NF(hours)}</td><td class="r">${PCT(hours, std)}</td>
        <td class="r">${g.labor ? NF(Math.round(g.labor)) : "—"}</td></tr>`;
      }).join("") + "</tbody></table></div>";
  }

  // 4) 완제품 재고현황
  PR_STOCK = d.stock;
  let kBody;
  if (!d.stock.length) {
    kBody = '<div class="auto">기간 내 재고 변동이 없습니다</div>';
  } else {
    let tO = 0, tP = 0, tS = 0, tD = 0, tC = 0, tAmt = 0;
    kBody = `<div class="tbl-wrap"><table>
      <thead><tr><th>제품명</th><th class="r">기초재고</th><th class="r">생산입고</th><th class="r">출고</th>
        <th class="r">폐기</th><th class="r">기말재고</th><th>소비기한</th><th class="r">단가(원)</th><th class="r">재고금액(원)</th></tr></thead>
      <tbody class="num">` + d.stock.map((r, i) => {
        const disp = r.disp || 0;
        const close = r.open + r.prod - r.ship - disp;
        const amt = r.amount || 0;   // 거래처별 단가 반영 재고금액 (서버 계산)
        tO += r.open; tP += r.prod; tS += r.ship; tD += disp; tC += close; tAmt += amt;
        const hasLots = (r.lots || []).length > 0;
        const nameCell = hasLots
          ? `<button class="uselink" data-plot="${i}" title="LOT·소비기한·금액 상세 보기"><b>${esc(r.name)}</b></button>`
          : `<b>${esc(r.name)}</b>`;
        // 거래처별 단가가 섞여 기본단가×수량과 다르면 금액 옆에 * 표시(제품 클릭 시 상세)
        const mixed = r.amount != null && r.unit_price && Math.abs(amt - close * r.unit_price) > 1;
        return `<tr><td>${nameCell}</td><td class="r">${NF(r.open)}</td>
          <td class="r">${NF(r.prod)}</td><td class="r">${NF(r.ship)}</td>
          <td class="r" ${disp > 0 ? 'style="color:var(--crit)"' : ""}>${disp ? NF(disp) : "—"}</td>
          <td class="r" style="font-weight:700; ${close < 0 ? "color:var(--crit)" : ""}">${NF(close)}</td>
          <td class="auto" style="font-weight:400">${close > 0 ? stockExpCell(r) : "—"}</td>
          <td class="r">${r.unit_price ? NF(r.unit_price) : "—"}</td>
          <td class="r" ${mixed ? 'title="거래처별 단가가 반영된 금액 — 제품명 클릭 시 LOT별 상세"' : ""}>${r.amount != null ? NF(amt) + (mixed ? " *" : "") : "—"}</td></tr>`;
      }).join("") + `<tr style="font-weight:700"><td>합계</td><td class="r">${NF(tO)}</td>
        <td class="r">${NF(tP)}</td><td class="r">${NF(tS)}</td><td class="r">${tD ? NF(tD) : "—"}</td><td class="r">${NF(tC)}</td>
        <td></td><td></td><td class="r">${tAmt ? NF(Math.round(tAmt)) : "—"}</td></tr></tbody></table></div>`;
  }

  // 4) 용역 정산 — 업체별 × 날짜별 인원(남/여)·시간·노무비. 월간 탭 = 월 정산서
  const ar = d.agency_report || [];
  let aBody;
  if (!ar.length) {
    aBody = '<div class="auto">기간 내 용역 투입 기록이 없습니다 — 일일 입력 &gt; 인원·가동에서 [＋ 용역]으로 추가하고 업체를 지정하면 여기 집계됩니다</div>';
  } else {
    const byP = {};
    ar.forEach(r => {
      const g = byP[r.partner] = byP[r.partner] || { rows: [], cnt: 0, male: 0, female: 0, hours: 0, labor: 0 };
      g.rows.push(r); g.cnt += r.cnt; g.male += r.male; g.female += r.female;
      g.hours += r.hours; g.labor += r.labor || 0;
    });
    const hasLabor = ar.some(r => r.labor != null);
    aBody = `<div class="tbl-wrap"><table>
      <thead><tr><th>용역 업체</th><th>날짜</th><th class="r">인원</th><th class="r">남</th><th class="r">여</th>
        <th class="r">총 시간(h)</th>${hasLabor ? '<th class="r">노무비(원)</th>' : ""}</tr></thead>
      <tbody class="num">` + Object.entries(byP).map(([partner, g]) => {
        const detail = g.rows.map(r => `<tr><td class="auto"></td><td>${r.date}</td>
          <td class="r">${NF(r.cnt)}명</td><td class="r">${r.male || "·"}</td><td class="r">${r.female || "·"}</td>
          <td class="r">${NF(r.hours)}</td>${hasLabor ? `<td class="r">${r.labor != null ? NF(Math.round(r.labor)) : "—"}</td>` : ""}</tr>`).join("");
        return `<tr style="font-weight:700; background:var(--bg)">
          <td>🏢 ${esc(partner)}</td><td class="auto" style="font-weight:500">${g.rows.length}일</td>
          <td class="r">연 ${NF(g.cnt)}명</td><td class="r">${g.male || "·"}</td><td class="r">${g.female || "·"}</td>
          <td class="r">${NF(g.hours)}</td>${hasLabor ? `<td class="r">${g.labor ? NF(Math.round(g.labor)) : "—"}</td>` : ""}</tr>` + detail;
      }).join("") + (() => {
        const tc = ar.reduce((s, r) => s + r.cnt, 0), th2 = ar.reduce((s, r) => s + r.hours, 0);
        const tl = ar.reduce((s, r) => s + (r.labor || 0), 0);
        return `<tr style="font-weight:800"><td>합계</td><td></td><td class="r">연 ${NF(tc)}명</td>
          <td class="r">${NF(ar.reduce((s, r) => s + r.male, 0))}</td><td class="r">${NF(ar.reduce((s, r) => s + r.female, 0))}</td>
          <td class="r">${NF(th2)}</td>${hasLabor ? `<td class="r">${tl ? NF(Math.round(tl)) : "—"}</td>` : ""}</tr>`;
      })() + "</tbody></table></div>";
  }

  // 5) 특이사항
  const memoBody = d.memos.length
    ? d.memos.map(m => `<div class="feed-item"><span class="auto num" style="flex:none">${m.date.slice(5)}</span>
        <span><b>${esc(m.src)}</b> — ${esc(m.txt)}</span></div>`).join("")
    : '<div class="auto">기간 내 특이사항이 없습니다</div>';

  $("prodSections").innerHTML =
    psec(2, "원부자재 소모현황", `${rangeLbl} · 사용/입고 있는 자재만`, mBody, !d.materials.length) +
    psec(3, "인원 및 라인 가동 현황", rangeLbl, sBody, !d.staffing.length) +
    psec(4, "용역 정산 (업체별)", `${rangeLbl} · 월간 탭 = 월 정산서 · 업체는 용역 칩에서 지정`, aBody, !ar.length) +
    psec(5, "완제품 재고현황", `${rangeLbl} · 기말 = 기초 + 생산 − 출고 − 폐기`, kBody, !d.stock.length) +
    psec(6, "특이사항", `${rangeLbl} · 메모 · 수불부 비고 · 정지사유`, memoBody, !d.memos.length);
  applyProdFilter();
}
$("prodSections").addEventListener("click", e => {
  const lot = e.target.closest("[data-plot]");
  if (lot) { e.preventDefault(); openStockLot(+lot.dataset.plot); return; }
  const h = e.target.closest("[data-ptoggle]");
  if (h) h.closest(".psec").classList.toggle("closed");
});

/* 완제품 재고현황 제품 클릭 → 현재 LOT(생산일·소비기한·거래처·금액·D-day) 상세 팝업 (anaOverlay 재사용) */
function openStockLot(idx) {
  const r = PR_STOCK[idx]; if (!r) return;
  const lots = r.lots || [];
  const close = r.open + r.prod - r.ship - (r.disp || 0);
  const admin = r.amount != null;   // 금액 열람 권한이면 amount가 채워져 있음
  $("anaPTitle").textContent = r.name;
  $("anaPHint").textContent =
    `기말재고 ${NF(close)}개 · LOT ${lots.length}건` +
    (admin ? ` · 재고금액 ${NF(r.amount || 0)}원` : "");
  const packCell = l => {
    if (l.pack_count && l.boxes != null)
      return `${NF(l.pack_count)}개입 <b>${NF(l.boxes)}박스</b>`;
    if (l.pack_name) return `<span class="auto">📦 ${esc(l.pack_name)}</span>`;
    return '<span class="auto">—</span>';
  };
  $("anaPBody").innerHTML = lots.length ? `<div class="tbl-wrap"><table>
    <thead><tr><th>생산일</th><th class="r">수량</th><th>포장(박스)</th><th>소비기한</th><th>거래처</th>${admin ? '<th class="r">단가</th><th class="r">금액(원)</th>' : ""}</tr></thead>
    <tbody class="num">${lots.map(l => `<tr>
      <td>${l.made || '<span class="auto">미상</span>'}${l.no ? ` <span class="chip cat">#${l.no}</span>` : ""}</td>
      <td class="r" style="font-weight:700">${NF(l.qty)}</td>
      <td>${packCell(l)}</td>
      <td>${ddaySpan(l.expiry)}</td>
      <td>${l.partner ? esc(l.partner) : '<span class="auto">미지정</span>'}</td>
      ${admin ? `<td class="r">${l.price ? NF(l.price) : "—"}</td><td class="r">${l.amount != null ? NF(l.amount) : "—"}</td>` : ""}</tr>`).join("")}
    </tbody></table></div>
    <div class="auto" style="margin-top:8px">${admin ? "금액은 거래처별 단가를 반영합니다(거래처 미지정 LOT은 기본 단가). " : ""}소비기한이 임박(D-14 이하)한 LOT은 주황·빨강으로 표시됩니다. 생산일별 소비기한은 LOT 관리 화면에서 조정할 수 있습니다.</div>`
    : '<div class="auto">현재 재고 LOT이 없습니다</div>';
  $("anaOverlay").classList.add("on");
}
/* 생산실적(섹션1) 제품 클릭 → 같은 LOT 팝업. PR_STOCK(재고현황)에서 제품으로 찾는다 */
function openStockLotByPid(pid) {
  const idx = (PR_STOCK || []).findIndex(r => r.id === pid);
  if (idx < 0) { toast("이 제품은 현재 재고 LOT이 없어 상세를 표시할 수 없습니다"); return; }
  openStockLot(idx);
}

function renderDailyBars(rows) {
  const el = $("prodAbars");
  const act = rows.filter(r => r.prod_qty > 0).sort((a, b) => b.prod_qty - a.prod_qty);
  if (!act.length) { el.style.display = "none"; return; }
  const hasPlan = act.some(r => r.plan_qty > 0);
  const maxProd = Math.max(...act.map(r => r.prod_qty));
  const top = act.slice(0, 12);
  let h = `<div style="font-size:12px; font-weight:800; color:var(--muted); margin-bottom:4px;">
    ${hasPlan ? "계획 대비 달성률 — 우측 끝 = 계획 100%" : "제품별 생산량 비율 — 계획수량을 입력하면 달성률로 표시됩니다"}</div>`;
  h += top.map(r => {
    if (r.plan_qty > 0) {
      const pct = r.prod_qty / r.plan_qty * 100;
      const color = pct < 90 ? "var(--crit)" : pct < 100 ? "var(--ink)" : "var(--ok)";
      const gap = r.prod_qty - r.plan_qty;
      return `<div class="abar-row num">
        <span class="abar-name" title="${esc(r.name)}">${esc(r.name)}</span>
        <span class="abar-track"><span class="abar-fill" style="width:${Math.min(pct, 100)}%; background:${color};"></span></span>
        <span class="abar-pct" style="color:${color}">${pct.toFixed(1)}%
          <small>${gap >= 0 ? "+" + NF(gap) + " 초과" : NF(-gap) + " 미달"}</small></span></div>`;
    }
    return `<div class="abar-row num">
      <span class="abar-name" title="${esc(r.name)}">${esc(r.name)}</span>
      <span class="abar-track"><span class="abar-fill" style="width:${(r.prod_qty / maxProd * 100).toFixed(1)}%; background:var(--ink);"></span></span>
      <span class="abar-pct">${NF(r.prod_qty)} <small>개</small></span></div>`;
  }).join("");
  if (act.length > top.length)
    h += `<div class="hint" style="margin-top:6px;">외 ${act.length - top.length}개 제품 — 아래 표 참조</div>`;
  el.innerHTML = h;
  el.style.display = "";
}

function loadProdWeekDetail(d, i) {
  const day = d.days[i];
  const rows = d.rows.filter(r => r.date === day && r.q > 0).sort((a, b) => b.q - a.q);
  $("prodChartD").innerHTML = rows.length
    ? `<b>${day} (${dowOf(day)})</b> — ` + rows.map(r => `${esc(r.name)} <b>${NF(r.q)}</b>`).join(" · ")
    : `<b>${day}</b> — 생산 없음`;
  loadProd();
}

/* ══ 일일 입력 ═════════════════════════ */
const E = { date: null, prod: [], ship: [], mat: [], matIn: [], staff: [], usage: [], prevStock: {}, prevMaterials: [], prevDate: null, uratio: {}, shipLots: {} };
let _usageTimer = null;
function renderUsageSoon() {   // 생산실적 재렌더 시 그룹 헤더(수량) 동기화
  clearTimeout(_usageTimer);
  _usageTimer = setTimeout(renderUsage, 80);
}
const entryCal = Calendar("cal", d => loadDay(d));
function openEntry() {
  // 첫 진입 시 오늘 날짜 자동 선택·로드
  if (!E.date) {
    entryCal.sel = todayISO();
    entryCal.ym = entryCal.sel.slice(0, 7);
    loadDay(entryCal.sel);
  }
  entryCal.render();
}
$("btnCopyPrev").onclick = () => {
  if (!E.prevMaterials.length) return toast("직전 기록일 자재가 없습니다");
  entryTab = "stock"; renderEntryTabs();   // 실사는 재고 탭에 있음
  const have = new Set(E.mat.map(r => r.material_id));
  E.prevMaterials.forEach(pm => {
    if (!have.has(pm.material_id))
      E.mat.push({ material_id: pm.material_id, prev_qty: pm.real_qty, in_qty: "", real_qty: "", order_date: "", order_qty: "" });
  });
  renderMat(); toast(`${E.prevDate} 자재 ${E.prevMaterials.length}종 불러옴`);
};

/* 일일 입력 탭: 생산 입력 / 재고 · 입고 — 담당자별 화면·저장 분리 */
let entryTab = "prod";
const ENTRY_TAB_HINT = {
  prod: "생산실적 · 완제품 출고 · 자재 사용 · 인원·가동 · 특이사항 — [생산 입력 저장]은 이 항목들만 저장합니다",
  stock: "원부자재 입고 · 재고 실사 — [재고 · 입고 저장]은 이 항목들만 저장합니다 (생산 입력 데이터와 완전히 독립)",
};
function renderEntryTabs() {
  document.querySelectorAll("#entryTabs button").forEach(b =>
    b.classList.toggle("on", b.dataset.et === entryTab));
  document.querySelectorAll("[data-esec]").forEach(el => {
    el.style.display = el.dataset.esec === entryTab ? "" : "none";
  });
  $("entryTabHint").textContent = ENTRY_TAB_HINT[entryTab];
}
$("entryTabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-et]"); if (!b) return;
  entryTab = b.dataset.et; renderEntryTabs();
});
renderEntryTabs();

/* API staffing 행 → 편집 상태. 용역 = 개인별 [{h(시간), w(시급)}] — staffing_agency 상세가 있으면
   그대로, 없으면(구 데이터) 집계값에서 복원(시간 균등 분배, 시급은 라인 공통값). */
function mapStaffRow(r) {
  let ag = [];
  try { ag = JSON.parse(r.agency || "[]"); } catch (e) { ag = []; }
  if (ag.length) {
    ag = ag.map(a => ({ h: a.h || "", w: a.w == null ? "" : a.w, g: a.g || "", pid: a.pid || null }));
  } else {
    const n = Number(r.agency_count) || 0, th = Number(r.agency_hours) || 0;
    const perH = n ? Math.round(th / n * 100) / 100 : "";
    ag = Array.from({ length: n }, () => ({ h: perH || "", w: r.agency_wage ?? "", g: "", pid: null }));
  }
  return { line_id: r.line_id, headcount: r.headcount,
    agency: ag, agency_wage: r.agency_wage ?? "",
    target_hours: r.target_hours || "", work_hours: r.work_hours, stop_reason: r.stop_reason || "",
    members: JSON.parse(r.members || "[]").map(m => ({ id: m.id, h: m.h || "" })) };
}
async function loadDay(date) {
  const d = await api("/api/day/" + date);
  E.date = date;
  E.prod = d.production.map(r => ({ product_id: r.product_id, line_id: r.line_id, batches: r.batches || "", plan_qty: r.plan_qty || "", prod_qty: r.prod_qty || "", defect_qty: r.defect_qty || "",
    defect_reason: r.defect_reason || "",
    lotSplits: JSON.parse(r.lot_splits || "[]").map(s => ({ qty: s.qty, expiry: s.expiry, pack_mid: s.pack_mid || null, pack_set: s.pack_set || "", partner_id: s.partner_id || null })),
    prodSplits: JSON.parse(r.prod_splits || "[]").filter(s => Number(s.qty) > 0)
      .map(s => ({ partner_id: s.partner_id, qty: s.qty })),
    expiry: r.expiry || "" }));
  E.ship = d.shipment.map(r => ({ product_id: r.product_id, partner_id: r.partner_id, qty: r.qty, prod_date: r.prod_date || "", lotExpiry: r.expiry || "", lotNo: r.lot_no || 0 }));
  E.shipBase = {};   // 이 날짜에 이미 저장돼 있던 출고 합 (제품별) — 가용재고 계산에 되더함
  d.shipment.forEach(r => { E.shipBase[r.product_id] = (E.shipBase[r.product_id] || 0) + Number(r.qty || 0); });
  E.shipLots = {};   // 날짜별 출고 가능 LOT 캐시 (제품 선택 시 lazy 로드)
  E.mat = d.materials.filter(r => r.src !== "auto")
    .map(r => ({ material_id: r.material_id, prev_qty: r.prev_qty, in_qty: r.in_qty || "", real_qty: r.real_qty, order_date: r.order_date || "", order_qty: r.order_qty || "" }));
  E.autoMat = d.materials.filter(r => r.src === "auto");
  E.matIn = (d.mat_in || []).map(r => ({ material_id: r.material_id, qty: r.qty, expiry: r.expiry || "", note: r.note || "" }));
  // 발주됐는데 아직 입고 안 된 자재 → 입고 카드에 자동 제안 (입고량 비워두면 저장 안 됨)
  (d.pending_orders || []).forEach(o => {
    if (!E.matIn.some(x => x.material_id === o.material_id))
      E.matIn.push({ material_id: o.material_id, qty: "", expiry: "",
        note: `발주 ${o.rec_date.slice(5)}${o.order_qty ? " · " + NF(o.order_qty) + (o.unit || "") : ""}${o.order_date ? " · " + o.order_date : ""}` });
  });
  E.lots = d.lots || [];
  E.usage = (d.usage || []).map(u => ({ product_id: u.product_id, material_id: u.material_id, qty: u.qty, block: u.block || "" }));
  E.uratio = {};   // 이 날짜에서 '배합 선택'으로 적용한 배율 (표시 유지용)
  E.uSrc = {};     // "pid|block" → {srcPid, srcBlock} — 다른 제품 배합을 가져와 쓰는 블록
  E.staff = d.staffing.map(mapStaffRow);
  E.prevStock = d.prev_stock; E.prevMaterials = d.prev_materials; E.prevDate = d.prev_date;
  snapshotSaved();   // 이 날짜의 '저장된 값' 스냅샷 — 키패드가 이전 값을 보여주는 근거 (DB 추가 불필요)
  E.photos = d.photos || []; E.prevProdDate = d.prev_prod_date;
  $("eMemo").value = d.memo || "";
  E.version = d.version ?? null;   // 동시 편집 충돌 감지용 (저장 시 서버가 비교)
  E.notifiedVer = d.version ?? null;   // 이 버전까지는 '갱신됨' 알림을 이미 반영한 상태
  $("entryStateTitle").textContent = `${date} (${dowOf(date)}) 상태`;
  E.stateBase = d.exists
    ? `<span class="chip ok">저장된 기록</span> 수정 후 저장하면 덮어씁니다.<br>생산 ${d.production.length}건 · 출고 ${d.shipment.length}건 · 자재 ${d.materials.length}건`
    : `<span class="chip warn">미입력</span> 새 기록을 작성합니다.`;
  E.viewers = d.viewers || [];
  renderEntryViewers();
  await ensureBomAll();   // 자재 사용의 블록 판정·배합수 역산에 배합비 필요
  renderAll();
}
/* ── 저장된 값 스냅샷 — 숫자칸 수정 시 '이전 저장값'을 보여주기 위함 ──
   서버에서 막 불러온 값이 곧 '마지막 저장값'이므로 DB에 따로 기록할 필요가 없다.
   저장하면 loadDay가 다시 돌면서 스냅샷도 새 값으로 갱신된다. */
function snapshotSaved() {
  const keyOf = {
    eMat: r => "m" + r.material_id,
    eMatIn: r => "i" + r.material_id,
    eProd: r => "p" + r.product_id,
    eShip: r => "s" + r.product_id + "|" + (r.partner_id || "") + "|" + (r.prod_date || ""),
    eUsage: r => "u" + r.product_id + "|" + r.material_id + "|" + (r.block || ""),
    eStaff: r => "t" + r.line_id,
  };
  const src = { eMat: E.mat, eMatIn: E.matIn, eProd: E.prod, eShip: E.ship, eUsage: E.usage, eStaff: E.staff };
  E.saved = {};
  Object.entries(src).forEach(([tb, arr]) => {
    (arr || []).forEach(r => { E.saved[tb + ":" + keyOf[tb](r)] = { ...r }; });
  });
  // 자동 반영(src='auto') 행도 DB에 저장된 값이다 — 실사로 승격해 고칠 때 원래 값을 보여주려면 필요
  (E.autoMat || []).forEach(r => {
    const k = "eMat:m" + r.material_id;
    if (!E.saved[k]) E.saved[k] = { real_qty: r.real_qty, prev_qty: r.prev_qty, in_qty: r.in_qty, order_qty: r.order_qty };
  });
  E.savedKeyOf = keyOf;
}
/** 이 입력칸의 '이전 저장값' (없으면 null) */
function savedValueOf(el) {
  const tb = el.closest("tbody"); if (!tb || !E.saved) return null;
  const tr = el.closest("tr[data-i]"); if (!tr) return null;
  const f = el.dataset.f; if (!f) return null;
  const arr = { eMat: E.mat, eMatIn: E.matIn, eProd: E.prod, eShip: E.ship, eUsage: E.usage, eStaff: E.staff }[tb.id];
  const kf = E.savedKeyOf && E.savedKeyOf[tb.id];
  if (!arr || !kf) return null;
  const row = arr[+tr.dataset.i]; if (!row) return null;
  const snap = E.saved[tb.id + ":" + kf(row)];
  if (!snap) return null;                       // 이번에 새로 추가한 행 = 이전 값 없음
  const v = snap[f];
  return v === "" || v == null ? null : v;
}
/* 동시 편집 — 같은 날짜를 보고 있는 사람 표시 (8초 폴링으로 실시간 갱신) */
function renderEntryViewers() {
  const v = E.viewers || [];
  $("entryState").innerHTML = (E.stateBase || "")
    + (v.length
      ? `<br><span style="color:#B45309; font-weight:700">⚠ ${v.map(esc).join(", ")}님이 지금 이 날짜를 보고 있습니다</span>`
        + `<br><span class="auto" style="font-size:11px">동시에 저장하면 늦게 저장한 내용만 남습니다 — 저장할 때 다시 확인합니다</span>`
      : "");
}
/* 다른 사용자가 이 날짜를 저장하면 알림 — [화면 갱신]으로 최신 내용 다시 불러오기 */
function showDayUpdated(ver, who) {
  if (E.notifiedVer === ver) return;      // 같은 버전으로 반복 알림하지 않음
  E.notifiedVer = ver;
  $("dayUpdHint").innerHTML =
    `<b>${esc(E.date)}</b> 기록을 ${who ? `<b>${esc(who)}</b>님이` : "다른 사용자가"} 방금 저장했습니다.<br>`
    + `지금 보고 있는 화면은 저장 전 내용이라, 이대로 저장하면 그 내용을 덮어쓰게 됩니다.<br>`
    + `<span style="color:var(--crit); font-weight:700">[화면 갱신]을 누르면 저장하지 않은 내 수정은 사라집니다.</span>`;
  $("dayUpdOverlay").classList.add("on");
}
window.closeDayUpd = () => $("dayUpdOverlay").classList.remove("on");
$("dayUpdReload").onclick = () => { closeDayUpd(); loadDay(E.date); toast("최신 내용으로 갱신했습니다"); };
const NEWMAT_OPTS = `<option value="__new_raw__">➕ 새 원재료 등록…</option><option value="__new_sub__">➕ 새 부재료 등록…</option>`;
let pendingNewMat = null;   // 일일 입력에서 신규 자재 등록 시, 저장 후 그 행에 자동 선택
function selHtml(list, val, field, nameKey = "name", extra = "", cls = "") {
  return `<select class="mini-sel ${cls}" data-f="${field}"><option value="">— 선택 —</option>${extra}` +
    list.map(o => `<option value="${o.id}" ${o.id === val ? "selected" : ""}>${esc(o[nameKey])}</option>`).join("") + "</select>";
}
/* 자재 select — 원재료/부재료를 optgroup으로 분리해 찾기 쉽게 (data-f / data-uf 는 호출부 지정) */
function matOptGroups(val) {
  const grp = (label, arr) => arr.length
    ? `<optgroup label="${label}">${arr.map(o => `<option value="${o.id}" ${o.id === val ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</optgroup>` : "";
  return grp("원재료", M.raw) + grp("부재료", M.sub);
}
function matSel(val, dataAttr, extra = "", cls = "nm") {
  return `<select class="mini-sel ${cls}" ${dataAttr}><option value="">— 자재 —</option>${extra}${matOptGroups(val)}</select>`;
}
function renderAll() { renderProd(); renderShip(); renderMatIn(); renderMat(); renderStaff(); renderUsage(); renderNeed(); renderPhotos(); }

/* 제품별 자재 사용 (material_usage) — 금액(단가×사용량)은 admin 전용 표시.
   원재료/부재료 섹션으로 분리 — 각 섹션에서 바로 추가 가능. block 값은 데이터에만 보존. */
// 제품이 가진 배합 블록 목록 (반죽·토핑 우선, 그 외 '')
function productDisplayBlocks(pid) {
  const s = new Set();
  (BOMALL?.[pid] || []).forEach(b => s.add(b.block || ""));
  E.usage.filter(u => u.product_id === pid).forEach(u => s.add(u.block || ""));
  const order = ["반죽", "토핑", ""];
  return [...s].sort((a, b) => order.indexOf(a) - order.indexOf(b));
}
// 생산실적 배합 칸과 동기화할 대표 블록 (반죽 우선)
function primaryBatchBlock(pid) {
  const s = new Set((BOMALL?.[pid] || []).map(b => b.block || ""));
  if (s.has("반죽")) return "반죽";
  if (s.has("") && s.size === 1) return "";
  return [...s].find(b => b) ?? "";
}
// 이 블록의 표시 배율: 적용값 > (반죽=생산실적 배합) > 사용량 역산 > 1
function blockRatio(pid, block) {
  if (E.uratio[pid + "|" + block] != null) return E.uratio[pid + "|" + block];
  if (block === primaryBatchBlock(pid)) {
    const pr = E.prod.find(r => r.product_id === pid);
    if (pr && Number(pr.batches) > 0) return Number(pr.batches);
  }
  const y = productById(pid)?.batch_yield || 0;
  for (const b of (BOMALL?.[pid] || []).filter(x => (x.block || "") === block && Number(x.batch_qty) > 0)) {
    const u = E.usage.find(u => u.product_id === pid && u.material_id === b.material_id && (u.block || "") === block);
    if (u && Number(u.qty) > 0) {
      const m = materialById(b.material_id);
      let per = Number(b.batch_qty);
      if (b.unit === "g" && m && m.unit === "kg") per /= 1000;
      if (per > 0) return Math.round(Number(u.qty) / per * 10) / 10;
    }
  }
  return 1;
}
function blockStepperHtml(pid, block) {
  const cur = blockRatio(pid, block);
  const applied = E.uratio[pid + "|" + block] != null;
  const src = (E.uSrc || {})[pid + "|" + block];
  const srcChip = src
    ? `<span class="chip warn" style="margin-left:4px" title="'${esc(productById(src.srcPid)?.name || "?")}'의 ${src.srcBlock || "기본"} 배합을 가져와 사용 중 — ±0.5도 그 배합 기준">📥 ${esc((productById(src.srcPid)?.name || "?").slice(0, 12))}</span>` : "";
  return `<span class="chip ${applied ? "ok" : "cat"} num" style="margin-left:6px" title="이 배합의 배율">${NF(cur)}배합${applied ? " ✓" : ""}</span>${srcChip}
    <button class="btn sm num" data-ustep="${pid}|${block}|-">−0.5</button>
    <button class="btn sm num" data-ustep="${pid}|${block}|+">+0.5</button>
    <button class="btn sm" data-ustep="${pid}|${block}|d">기본</button>`;
}
/* 📥 다른 제품 배합 가져오기 — 타 제품의 반죽/토핑 배합비를 이 제품의 사용량으로 (간헐 사용 케이스) */
function recipeImportSel(pid) {
  if (!BOMALL) return "";
  const opts = [];
  Object.entries(BOMALL).forEach(([spidS, brows]) => {
    const spid = +spidS;
    if (spid === pid) return;   // 자기 배합은 스테퍼(±0.5)가 담당
    const p = productById(spid);
    if (!p || p.status === "단종") return;
    [...new Set(brows.map(b => b.block || ""))].forEach(bk => {
      opts.push(`<option value="${spid}|${bk}">${esc(p.name)} — ${bk || "기본"}</option>`);
    });
  });
  if (!opts.length) return "";
  const active = Object.keys(E.uSrc || {}).some(k => k.startsWith(pid + "|"));
  return `<select class="mini-sel" data-uimport="${pid}" style="max-width:200px; font-size:11.5px;"
    title="간혹 다른 제품의 반죽·토핑을 만들 때 — 그 제품 배합비를 이 제품 사용량으로 가져옵니다">
    <option value="">📥 다른 제품 배합 가져오기</option>
    ${active ? '<option value="__revert__">↩ 가져온 배합 되돌리기</option>' : ""}
    ${opts.join("")}</select>`;
}
function usageGroupHtml(pid, title, sub, buttons, addBar) {
  const admin = canM("mat");   // 자재 금액 열람
  const nCols = admin ? 5 : 4;
  let gAmt = 0, gMiss = 0;
  const items = E.usage.map((u, ui) => ({ u, ui })).filter(x => x.u.product_id === pid);
  const kindOf = u => { const m = materialById(u.material_id); return m ? m.kind : (u.sec || "raw"); };
  const rowHtml = ({ u, ui }) => {
      const m = materialById(u.material_id) || {};
      const amt = Number(u.qty) > 0 && m.unit_price > 0 ? Number(u.qty) * m.unit_price : null;
      if (amt != null) gAmt += amt;
      else if (Number(u.qty) > 0 && u.material_id) gMiss++;
      const kc = m.kind ? `<span class="kchip kchip-${m.kind}" title="${m.kind === "raw" ? "원재료" : "부재료"}">${m.kind === "raw" ? "원재료" : "부재료"}</span>` : "";
      // 납품처 지정 자재 표시 (배합비에 거래처가 지정된 자재 — 복수 가능)
      const bomP = pid != null ? (BOMALL?.[pid] || []).find(x =>
        x.material_id === u.material_id && (x.block || "") === (u.block || "") && bomPartnerIds(x).length) : null;
      const pchipNames = bomP ? bomPartnerIds(bomP)
        .map(id => (M.partner.find(p => p.id === id) || {}).name).filter(Boolean) : [];
      const pchip = pchipNames.length ? `<span class="chip cat" style="font-size:10px" title="이 자재는 지정 납품처용 — 계획 거래처 분배의 그 거래처 몫으로 계산됩니다">🏢 ${esc(pchipNames.join(", ").slice(0, 20))}</span>` : "";
      // 툴팁: 개수 자재는 나눗셈 근거, 금액 셀은 곱셈 근거를 그대로 보여줌
      const prodRow = pid != null ? E.prod.find(x => x.product_id === pid) : null;
      const qtyTip = isCountMat(m) && prodRow
        ? `개수 자재 자동 계산: 생산(분배) 수량 ÷ 개입수 ${NF(m.pack_count)} (올림)` : "";
      return `<tr data-ui="${ui}">
        <td style="display:flex; align-items:center; gap:4px;">${kc}${matSel(u.material_id, 'data-uf="material_id"', NEWMAT_OPTS)}${pchip}</td>
        <td class="r"><input class="mini-input num" data-uf="qty" value="${u.qty ?? ""}" ${qtyTip ? `title="${qtyTip}"` : ""}></td>
        <td class="auto">${esc(m.unit || "")}</td>
        ${admin ? `<td class="r auto" ${amt != null ? `title="사용량 ${NF(u.qty)} × 단가 ${NF(m.unit_price)}원"` : ""}>${amt != null ? NF(Math.round(amt)) : (Number(u.qty) > 0 ? '<span class="auto">단가 미입력</span>' : "—")}</td>` : ""}
        <td><button class="btn ghost sm" data-udel>삭제</button></td></tr>`;
    };
  const pidKey = pid ?? 0;   // 기타 사용(제품 없음) 그룹은 0으로 표기 — 핸들러에서 null로 복원
  // 하단 검색+추가 (블록·구분별)
  const searchRow = block => addBar ? `<tr><td colspan="${nCols}" style="padding:5px 10px 9px;">
      <input class="mini-input" list="qaMatRaw" data-usearch="${pidKey}|${block}|raw" placeholder="🔍 원재료" style="width:150px; text-align:left">
      <button class="btn ghost sm" data-uadd="${pidKey}|${block}|raw">＋원재료</button>
      <span style="display:inline-block; width:14px"></span>
      <input class="mini-input" list="qaMatSub" data-usearch="${pidKey}|${block}|sub" placeholder="🔍 부재료" style="width:150px; text-align:left">
      <button class="btn ghost sm" data-uadd="${pidKey}|${block}|sub">＋부재료</button></td></tr>` : "";
  const blocks = pid != null ? productDisplayBlocks(pid) : [];
  const multi = blocks.some(b => b === "반죽" || b === "토핑");
  let bodyHtml;
  if (pid != null && multi) {
    // 배합 블록(반죽/토핑)별 섹션 — 각 블록에 배합수 스테퍼, 자재는 원(원재료)/부(부재료) 칩으로 구분
    const BL = { "반죽": "🍞 반죽 배합", "토핑": "🍪 토핑 배합", "": "📦 기타 · 포장 (배합 무관)" };
    bodyHtml = blocks.map(bk => {
      const brows = items.filter(x => (x.u.block || "") === bk)
        .sort((a, b2) => (kindOf(a.u) === "sub") - (kindOf(b2.u) === "sub"));
      const rowsH = brows.map(rowHtml).join("");
      const stepper = (bk === "반죽" || bk === "토핑") ? blockStepperHtml(pid, bk) : "";
      const head = `<tr><td colspan="${nCols}" style="background:var(--bg); font-size:11.5px; font-weight:800; color:var(--muted); padding:6px 10px;">${BL[bk] || "기타"} ${stepper}</td></tr>`;
      const emptyH = rowsH ? "" : `<tr><td colspan="${nCols}" class="auto" style="font-size:12px; padding:3px 10px;">${bk === "" ? "포장·기타 자재를 아래에서 추가하세요" : "±0.5로 배합 수를 누르면 배합비대로 자동 입력됩니다"}</td></tr>`;
      return head + rowsH + emptyH + searchRow(bk);
    }).join("");
  } else {
    // 단일(배합 블록 없음)·기타 사용: 원재료/부재료 섹션
    const SEC = { raw: "🌾 원재료", sub: "📦 부재료" };
    const secHtml = kind => {
      const rows = items.filter(x => kindOf(x.u) === kind).map(rowHtml).join("");
      const label = kind === "raw" ? "원재료" : "부재료";
      const head = `<tr><td colspan="${nCols}" style="background:var(--bg); font-size:11.5px; font-weight:800; color:var(--muted); padding:5px 10px;">${SEC[kind]}</td></tr>`;
      const emptyMsg = pid == null ? `사용한 ${label}를 아래에서 추가하세요`
        : (kind === "raw" ? "배합 수를 입력하면 배합비대로 자동 입력됩니다" : "포장지·박스 등 부재료 사용을 아래에서 추가하세요");
      const empty = rows ? "" : `<tr><td colspan="${nCols}" class="auto" style="font-size:12px; padding:4px 10px;">${emptyMsg}</td></tr>`;
      const foot = addBar ? `<tr><td colspan="${nCols}" style="padding:5px 10px 9px;">
        <input class="mini-input" list="${kind === "raw" ? "qaMatRaw" : "qaMatSub"}" data-usearch="${pidKey}||${kind}"
          placeholder="🔍 ${label} 검색 후 Enter" style="width:220px; text-align:left">
        <button class="btn ghost sm" data-uadd="${pidKey}||${kind}" style="margin-left:6px;">＋ ${label} 추가</button></td></tr>` : "";
      return head + rows + empty + foot;
    };
    bodyHtml = secHtml("raw") + secHtml("sub");
  }
  const foot = admin && gAmt > 0
    ? `<tr style="font-weight:700; background:var(--bg);"><td>자재비 합계</td><td></td><td></td>
       <td class="r num">${NF(Math.round(gAmt))}원${gMiss ? ` <span class="auto" style="font-weight:400">(단가 미입력 ${gMiss}종 제외)</span>` : ""}</td><td></td></tr>` : "";
  const impSel = (pid != null && addBar) ? recipeImportSel(pid) : "";
  return `<div class="ugroup">
    <div class="ughead"><span class="uno">${title}</span>
      <span class="auto num">${sub}</span>
      <span class="spacer"></span>${impSel}${buttons}</div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>자재명</th><th class="r">사용량</th><th>단위</th>${admin ? '<th class="r">금액(원)</th>' : ""}<th></th></tr></thead>
      <tbody class="num">${bodyHtml}${foot}</tbody></table></div>
  </div>`;
}

function renderUsage() {
  const box = $("eUsage");
  if (!E.date) { box.innerHTML = '<div class="auto">달력에서 날짜를 선택하세요</div>'; return; }
  $("qaMatRaw").innerHTML = M.raw.map(o => `<option value="${esc(o.name)}">`).join("");   // 섹션별 검색용
  $("qaMatSub").innerHTML = M.sub.map(o => `<option value="${esc(o.name)}">`).join("");
  // 생산실적과 연동: 그룹 = 생산실적의 제품 (순서 동일)
  const mainPids = [];
  E.prod.forEach(r => { if (r.product_id && !mainPids.includes(r.product_id)) mainPids.push(r.product_id); });
  const orphanPids = [];
  (E.usage || []).forEach(u => {
    if (u.product_id && !mainPids.includes(u.product_id) && !orphanPids.includes(u.product_id))
      orphanPids.push(u.product_id);
  });
  let h = mainPids.map((pid, gi) => {
    const p = productById(pid);
    const prodRow = E.prod.find(r => r.product_id === pid);
    // 배합 스테퍼는 반죽/토핑 블록별로 usageGroupHtml 안에서 렌더 (그룹 헤더는 제목만)
    return usageGroupHtml(pid,
      `${gi + 1}. ${esc(p ? p.name : "(삭제된 제품)")}`,
      prodRow && prodRow.prod_qty ? "생산 " + NF(Number(prodRow.prod_qty)) + "개" : "생산수량 미입력",
      "", true);
  }).join("");
  if (!mainPids.length) h += '<div class="auto" style="margin-bottom:8px;">생산실적에 제품을 추가하면 제품별 그룹이 생깁니다 — 생산과 무관한 사용은 아래 \'기타 사용\'에 기록하세요</div>';
  if (orphanPids.length) {
    h += `<div style="font-size:11.5px; font-weight:700; color:var(--warn); margin:12px 0 8px;">
      ⚠ 아래는 생산실적에 없는 제품의 사용 기록입니다 (엑셀 임포트분 또는 생산 행 삭제로 남은 기록) —
      [생산실적에 추가]로 연결하거나 [기록 삭제]로 정리하세요</div>`;
    h += orphanPids.map(pid => {
      const p = productById(pid);
      return usageGroupHtml(pid,
        `${esc(p ? p.name : "(삭제된 제품)")}`, "생산실적 없음",
        `<button class="btn sm" data-uprod="${pid}">생산실적에 추가</button>
         <button class="btn sm" style="color:var(--crit)" data-uclear="${pid}">기록 삭제</button>`);
    }).join("");
  }
  // 기타 사용 (생산 외) — 제품 없이 쓴 자재. 생산실적과 무관하게 항상 입력 가능
  h += usageGroupHtml(null, "기타 사용 (생산 외)",
    "빵 생산과 무관한 사용 — 테스트·청소·타용도 등", "", true);
  if (canM("mat")) {
    let tot = 0;
    E.usage.forEach(u => {
      const m = materialById(u.material_id);
      if (m && m.unit_price > 0 && Number(u.qty) > 0) tot += Number(u.qty) * m.unit_price;
    });
    if (tot > 0) h += `<div class="num" style="text-align:right; font-weight:800; font-size:13px; margin-top:6px;">
      이날 자재비 총합계 (단가 입력분): ${NF(Math.round(tot))}원</div>`;
  }
  box.innerHTML = h;
}
$("eUsage").addEventListener("input", e => {
  const tr = e.target.closest("tr[data-ui]"); if (!tr) return;
  const f = e.target.dataset.uf; if (!f) return;
  const u = E.usage[+tr.dataset.ui];
  if (f === "material_id" && String(e.target.value).startsWith("__new_")) {
    pendingNewMat = { list: "usage", index: +tr.dataset.ui };
    const kind = e.target.value === "__new_raw__" ? "raw" : "sub";
    e.target.value = u.material_id || "";
    openMaster(kind, null);
    return;
  }
  u[f] = e.target.tagName === "SELECT"
    ? (e.target.value ? +e.target.value : null)
    : e.target.value;
  if (e.target.tagName === "SELECT") { renderUsage(); return; }
  if (f === "qty" && canM("mat") && tr.cells[3]) {   // 금액 제자리 갱신 (포커스 유지)
    const m = materialById(u.material_id) || {};
    tr.cells[3].textContent = Number(u.qty) > 0 && m.unit_price > 0
      ? NF(Math.round(Number(u.qty) * m.unit_price))
      : (Number(u.qty) > 0 ? "단가 미입력" : "—");
    tr.cells[3].title = Number(u.qty) > 0 && m.unit_price > 0
      ? `사용량 ${NF(u.qty)} × 단가 ${NF(m.unit_price)}원` : "";
  }
});
/* 하단 검색으로 행 추가 — 해당 블록(반죽/토핑/'')·구분(원/부) 안에서만 검색 */
function usageAddByName(pid, name, kind, block) {
  const list = kind === "raw" ? M.raw : kind === "sub" ? M.sub : M.raw.concat(M.sub);
  const label = kind === "raw" ? "원재료" : kind === "sub" ? "부재료" : "자재";
  let hit = list.find(o => o.name === name);
  if (!hit) {
    const cands = list.filter(o => o.name.toLowerCase().includes(name.toLowerCase()));
    if (!cands.length) return toast(`${label}에서 '${name}' 검색 결과 없음`);
    if (cands.length > 1) return toast(`'${name}' 검색 결과 ${cands.length}건 — 목록에서 정확한 이름을 선택하세요`);
    hit = cands[0];
  }
  E.usage.push({ product_id: pid, material_id: hit.id, qty: "", block: block || "", sec: kind });
  renderUsage();
  toast(`'${hit.name}' 행 추가됨${block ? ` (${block})` : ""}`);
}
// 📥 다른 제품 배합 가져오기 — 선택 시 그 배합비의 자재·수량을 이 제품의 해당 블록에 채움
$("eUsage").addEventListener("change", async e => {
  const imp = e.target.closest("[data-uimport]");
  if (!imp || !imp.value) return;
  E.uSrc = E.uSrc || {};
  const pid = +imp.dataset.uimport;
  const val = imp.value;
  imp.value = "";
  if (val === "__revert__") {   // 가져온 배합 전부 해제 (그 블록 행 삭제)
    Object.keys(E.uSrc).filter(k => k.startsWith(pid + "|")).forEach(k => {
      const bk = k.split("|")[1];
      E.usage = E.usage.filter(u => !(u.product_id === pid && (u.block || "") === bk));
      delete E.uSrc[k];
      delete E.uratio[k];
    });
    renderUsage();
    toast("↩ 가져온 배합을 해제했습니다 — 배합 수(±0.5)를 누르면 이 제품 배합비로 채워집니다");
    return;
  }
  const [spidS, srcBlock] = val.split("|");
  const spid = +spidS;
  const destBlock = srcBlock;   // 가져온 배합은 같은 블록 이름(반죽→반죽, 토핑→토핑)으로 들어감
  const srcName = productById(spid)?.name || "?";
  const exist = E.usage.filter(u => u.product_id === pid && (u.block || "") === destBlock).length;
  if (exist && !confirm(`이 제품의 '${destBlock || "기본"}' 자재 ${exist}건을\n'${srcName}'의 ${srcBlock || "기본"} 배합으로 교체할까요?`)) return;
  E.usage = E.usage.filter(u => !(u.product_id === pid && (u.block || "") === destBlock));
  E.uSrc[pid + "|" + destBlock] = { srcPid: spid, srcBlock };
  await setBatchRatio(pid, 1, destBlock);   // 1배합으로 채움 — 이후 ±0.5 스테퍼도 이 배합 기준
});
$("eUsage").addEventListener("keydown", e => {
  const inp = e.target.closest("[data-usearch]");
  if (inp && e.key === "Enter") {
    e.preventDefault();
    const v = inp.value.trim();
    const [pidS, block, kind] = inp.dataset.usearch.split("|");
    if (v) { usageAddByName(+pidS || null, v, kind, block); }   // 0 = 기타 사용(제품 없음)
  }
});
$("eUsage").addEventListener("click", e => {
  const del = e.target.closest("[data-udel]");
  if (del) { E.usage.splice(+del.closest("tr[data-ui]").dataset.ui, 1); renderUsage(); return; }
  const add = e.target.closest("[data-uadd]");
  if (add) {   // 하단 [＋ 추가]: 검색어 있으면 그 자재로, 없으면 그 블록·구분에 빈 행
    const [pidS, block, kind] = add.dataset.uadd.split("|");
    const pid = +pidS || null;   // 0 = 기타 사용(제품 없음)
    const inp = $("eUsage").querySelector(`[data-usearch="${pidS}|${block}|${kind}"]`);
    const v = inp && inp.value.trim();
    if (v) usageAddByName(pid, v, kind, block);
    else { E.usage.push({ product_id: pid, material_id: null, qty: "", block: block || "", sec: kind }); renderUsage(); }
    return;
  }
  const toProd = e.target.closest("[data-uprod]");
  if (toProd) {
    const pid = +toProd.dataset.uprod;
    if (!E.prod.some(r => r.product_id === pid)) {
      E.prod.push({ product_id: pid, line_id: null, plan_qty: "", prod_qty: "", defect_qty: "", lotSplits: [], expiry: "" });
      renderProd(); renderUsage();
      toast("생산실적에 추가됨 — 생산수량을 입력하세요");
    }
    return;
  }
  const clear = e.target.closest("[data-uclear]");
  if (clear) {
    const pid = +clear.dataset.uclear;
    const p = productById(pid);
    const n = E.usage.filter(u => u.product_id === pid).length;
    if (!confirm(`'${p ? p.name : "?"}'의 사용 기록 ${n}건을 삭제할까요?\n저장 시 DB에 반영됩니다.`)) return;
    E.usage = E.usage.filter(u => u.product_id !== pid);
    renderUsage();
    return;
  }
  const step = e.target.closest("[data-ustep]");
  if (step) {
    const [pidS, block, op] = step.dataset.ustep.split("|");
    const pid = +pidS;
    const cur = blockRatio(pid, block);
    setBatchRatio(pid, op === "d" ? 1 : op === "+" ? cur + 0.5 : cur - 0.5, block);
    return;
  }
});
// 생산행에서 제품이 빠지면(행 삭제·제품 변경) 그 제품의 자재 사용 기록도 함께 정리
function cleanupUsageFor(pid) {
  if (!pid || E.prod.some(r => r.product_id === pid)) return;   // 같은 제품의 다른 생산행이 있으면 유지
  const n = E.usage.filter(u => u.product_id === pid).length;
  if (!n) return;
  E.usage = E.usage.filter(u => u.product_id !== pid);
  Object.keys(E.uratio).forEach(k => { if (k.startsWith(pid + "|")) delete E.uratio[k]; });
  if (E.uSrc) Object.keys(E.uSrc).forEach(k => { if (k.startsWith(pid + "|")) delete E.uSrc[k]; });
  renderUsage();
  toast(`'${productById(pid)?.name || "?"}' 자재 사용 ${n}건도 함께 제거됨 (저장 시 반영)`);
}
// 생산행에 제품을 선택하면 배합비 전체(반죽·토핑·포장 부재료)를 자동으로 채움.
// 이미 사용 기록이 있는 블록은 건드리지 않음 — 배합 수를 나중에 입력하면 대표 블록만 그 배율로 갱신
async function autoFillUsage(pid) {
  if (!pid) return;
  await ensureBomAll();
  const blocks = [...new Set((BOMALL[pid] || []).map(b => b.block || ""))];
  if (!blocks.length) return;   // 배합비 미등록 — 자동 채움 없음
  const pr = E.prod.find(r => r.product_id === pid);
  const primary = primaryBatchBlock(pid);
  const ratio = pr && Number(pr.batches) > 0 ? Number(pr.batches) : 1;
  let filled = false;
  for (const bk of blocks) {
    if (E.usage.some(u => u.product_id === pid && (u.block || "") === bk)) continue;
    const r = bk === primary ? ratio : 1;
    if (await applyBatchUsage(pid, r, bk, true)) {
      E.uratio[pid + "|" + bk] = r;
      filled = true;
    }
  }
  if (filled) {
    renderUsage();
    toast(`'${productById(pid)?.name || "?"}' 배합비 자재(원·부재료) 자동 입력됨`);
  }
}
// 배합 배율 스테퍼(±0.5/기본값) → 그 블록(반죽/토핑)의 배합비 × 배율로 자재 사용량 입력.
// 반죽(대표 블록)만 생산실적 배합 칸·계획과 동기화.
async function setBatchRatio(pid, ratio, block, quiet) {
  ratio = Math.max(0.5, Math.round(ratio * 10) / 10);
  E.uratio[pid + "|" + block] = ratio;
  const ok = await applyBatchUsage(pid, ratio, block, quiet);
  if (!ok) { delete E.uratio[pid + "|" + block]; renderUsage(); return; }
  // 가져온(타 제품) 배합의 배율은 이 제품의 생산실적 배합 칸과 무관 — 동기화 제외
  if (block === primaryBatchBlock(pid) && !(E.uSrc || {})[pid + "|" + block]) {
    const pr = E.prod.find(r => r.product_id === pid);
    if (pr && String(pr.batches) !== String(ratio)) {
      const py = productById(pid)?.batch_yield || 0;
      const prev = Number(pr.batches) || 0;
      if (py > 0 && (!Number(pr.plan_qty) || Number(pr.plan_qty) === Math.round(prev * py)))
        pr.plan_qty = Math.round(ratio * py);
      pr.batches = ratio;
      renderProd();
    }
  }
}
// '개수' 단위 자재 판정 — 개입수(pack_count)가 있으면 생산수량 ÷ 개입수로 소모 (kg/g 배합량 방식과 구분)
const COUNT_UNITS = new Set(["개", "ea", "EA", "매", "장", "롤", "박스", "묶음", "봉", "set", "세트", "팩"]);
function isCountMat(m) { return !!m && COUNT_UNITS.has(String(m.unit || "").trim()) && Number(m.pack_count) > 0; }
// LOT 분할에서 구간별로 지정한 포장 배정 — {포장 자재 id: Σ올림(구간수량 ÷ 개입수)}
// 하나라도 지정돼 있으면 그 포장들의 소모는 '배정 기준'으로 계산 (생산 전체 기준 이중 계산 방지)
function lotPackAssign(pid) {
  const pr = E.prod.find(r => r.product_id === pid);
  const agg = {};
  const add = (m, qty) => {
    if (!isCountMat(m)) return;
    agg[m.id] = (agg[m.id] || 0) + Math.ceil(qty / Number(m.pack_count));
  };
  (pr && pr.lotSplits || []).forEach(s => {
    if (!(Number(s.qty) > 0)) return;
    if (s.pack_set) {   // 세트: 그 세트의 모든 개수 부재료 함께 소모 (다대다 — 구성원 목록으로 조회)
      packSetMembers(s.pack_set).forEach(mm => add(materialById(mm.id), Number(s.qty)));
    } else if (s.pack_mid) {
      add(materialById(+s.pack_mid), Number(s.qty));
    }
  });
  return agg;
}
// 생산수량이 바뀌면 그 제품의 개수 자재 사용량을 다시 계산 (배합량 자재는 그대로)
// 우선순위: LOT 분할 포장 배정 > 납품처 지정 분배 몫 > 생산수량 전체
function updatePackUsage(pid) {
  const pr = E.prod.find(r => r.product_id === pid);
  const prodQty = pr ? Number(pr.prod_qty) || 0 : 0;
  const splits = (pr && pr.prodSplits || []).filter(s => Number(s.qty) > 0);
  const packAssign = lotPackAssign(pid);
  const hasAssign = Object.keys(packAssign).length > 0;
  let changed = false;
  E.usage.forEach(u => {
    if (u.product_id !== pid) return;
    const m = materialById(u.material_id);
    if (!isCountMat(m)) return;
    const b = (BOMALL?.[pid] || []).find(x =>
      x.material_id === u.material_id && (x.block || "") === (u.block || ""));
    const bpids = b ? bomPartnerIds(b) : [];
    let q;
    if (hasAssign && Number(m.pack_count) > 1) {
      // 포장을 구간별로 지정한 경우: 지정된 포장은 배정 합, 지정 안 된 포장(BOX류)은 0
      q = packAssign[u.material_id] || 0;
    } else if (bpids.length && splits.length) {
      const mine = splits.filter(x => bpids.includes(x.partner_id))
        .reduce((a, s) => a + Number(s.qty), 0);
      q = mine > 0 ? Math.ceil(mine / Number(m.pack_count)) : 0;
    } else {
      q = prodQty > 0 ? Math.ceil(prodQty / Number(m.pack_count)) : 0;
    }
    if (Number(u.qty) !== q) { u.qty = q; changed = true; }
  });
  if (changed) renderUsage();
}
const applyPartnerPack = updatePackUsage;   // 계획 분배 저장 시 납품처 부재료 재계산
// 특정 블록(반죽/토핑/'')의 배합비 자재를 배합수만큼 채움 (다른 블록은 건드리지 않음)
// E.uSrc에 출처가 지정된 블록은 그 제품의 배합비로 채움 (📥 다른 제품 배합 가져오기)
async function applyBatchUsage(pid, ratio, block, quiet) {
  await ensureBomAll();
  const src = (E.uSrc || {})[pid + "|" + block];
  const srcPid = src ? src.srcPid : pid;
  const srcBlock = src ? src.srcBlock : block;
  const list = (BOMALL[srcPid] || []).filter(b => (b.block || "") === srcBlock);
  if (!list.length) { if (!quiet) toast(`이 제품의 ${block || ""} 배합비가 없습니다 — 기준정보 > 배합비에서 등록하세요`); return false; }
  const y = productById(srcPid)?.batch_yield || 0;   // 배합량 폴백 환산은 '출처' 제품 수율 기준
  const pr = E.prod.find(r => r.product_id === pid);
  const prodQty = pr && Number(pr.prod_qty) > 0 ? Number(pr.prod_qty) : Math.round(y * ratio);
  const splits = (pr && pr.prodSplits || []).filter(s => Number(s.qty) > 0);
  const splitTotal = splits.reduce((a, s) => a + Number(s.qty), 0);
  const agg = {};
  list.forEach(b => {
    const m = materialById(b.material_id);
    const bpids = bomPartnerIds(b);   // 납품처 지정 (복수) — 분배와 연동
    let qty;
    if (isCountMat(m)) {                               // 개수 자재: 수량 ÷ 개입수 (올림)
      const packAssign = lotPackAssign(pid);
      if (Object.keys(packAssign).length && Number(m.pack_count) > 1) {
        qty = packAssign[b.material_id] || 0;          // LOT 분할 포장 배정 우선
      } else if (bpids.length && splits.length) {      // 지정 납품처들의 분배 합 기준
        const mine = splits.filter(s => bpids.includes(s.partner_id))
          .reduce((a, s) => a + Number(s.qty), 0);
        if (!(mine > 0)) return;                       // 해당 거래처 생산 없음 → 행 제외
        qty = Math.ceil(mine / Number(m.pack_count));
      } else
      qty = prodQty > 0 ? Math.ceil(prodQty / Number(m.pack_count)) : 0;
    } else {
      qty = Number(b.batch_qty) > 0 ? Number(b.batch_qty) * ratio
        : b.qty_per_unit * y * ratio;                 // batch_qty 없는 구/실측 행 폴백
      if (b.unit === "g" && m && m.unit === "kg") qty /= 1000;
      if (bpids.length && splits.length && splitTotal > 0) {   // 배합량 자재 = 생산 중 그 거래처 몫 비례 (보유분 제외)
        const mine = splits.filter(s => bpids.includes(s.partner_id))
          .reduce((a, s) => a + Number(s.qty), 0);
        if (!(mine > 0)) return;
        qty = qty * mine / (prodQty > 0 ? Math.max(prodQty, splitTotal) : splitTotal);
      }
    }
    agg[b.material_id] = (agg[b.material_id] || 0) + qty;
  });
  let n = 0;
  Object.entries(agg).forEach(([midStr, qty]) => {
    const mid = +midStr;
    qty = Math.round(qty * 1000) / 1000;
    n++;
    const ex = E.usage.find(u => u.product_id === pid && u.material_id === mid && (u.block || "") === block);
    if (ex) ex.qty = qty;
    else E.usage.push({ product_id: pid, material_id: mid, qty, block });
  });
  renderUsage();
  if (!quiet) toast(src
    ? `📥 '${productById(srcPid)?.name || "?"}' ${srcBlock || "기본"} 배합 ${ratio}배합 입력됨 — ${n}종`
    : `${block || "배합"} ${ratio}배합 입력됨 — ${n}종`);
  return true;
}

// 배합 행의 납품처 목록 (복수 "1,3" — 구 단일 partner_id 폴백)
function bomPartnerIds(b) {
  if (b.partner_ids) return String(b.partner_ids).split(",").map(Number).filter(Boolean);
  return b.partner_id ? [+b.partner_id] : [];
}
let BOMALL = null;
async function ensureBomAll() {
  if (!BOMALL) {
    BOMALL = {};
    (await api("/api/bom")).forEach(r =>
      (BOMALL[r.product_id] = BOMALL[r.product_id] || []).push(r));
  }
  return BOMALL;
}
// (제거됨) '생산수량 기준' 자동 채움 — 사용량은 배합 수 기준 전량으로만 계산 (2026-07-07 사용자 확정)

// 생산실적 소비기한 버튼 — 분할(수량별 소비기한) 설정 요약 표시
function prodExpiryBtn(r, i) {
  const sp = (r.lotSplits || []).filter(s => s.expiry && Number(s.qty) > 0);
  if (!r.product_id) return '<span class="auto" style="font-size:11.5px">제품 먼저</span>';
  if (!sp.length)
    return `<button class="btn ghost sm" data-lotsplit="${i}">＋ 소비기한 설정</button>`;
  const label = sp.length === 1
    ? sp[0].expiry
    : `${sp.length}구간 · ~${sp.map(s => s.expiry).sort()[0]}`;
  return `<button class="btn sm" data-lotsplit="${i}" style="border-color:var(--ok); color:var(--ok-ink,#0a7d3c)">📅 ${label}</button>`;
}
// 생산실적의 라인 선택 — 물리(대표) 라인만. 공정 행으로 저장된 옛 값은 대표 라인으로 자동 매핑
function prodLineSel(val) {
  const phys = M.line.filter(l => !l.parent_id && l.status !== "중지");
  const cur = M.line.find(x => x.id === val);
  const v = cur && cur.parent_id ? +cur.parent_id : val;
  return `<select class="mini-sel" data-f="line_id"><option value="">— 선택 —</option>` +
    phys.map(o => {
      const hasKids = M.line.some(c => +c.parent_id === o.id);
      const label = hasKids ? o.name : (o.process ? `${o.name} / ${o.process}` : o.name);
      return `<option value="${o.id}" ${o.id === v ? "selected" : ""}>${esc(label)}</option>`;
    }).join("") + "</select>";
}
// 생산 셀 — 입력·거래처 분배 버튼·요약을 세로 정렬
// 생산수량은 항상 직접 입력 가능. 분배 합계 ≤ 생산이며 나머지는 '보유'(미출고 재고)로 표시
function prodSplitSummary(r) {
  const sp = (r.prodSplits || []).filter(s => Number(s.qty) > 0);
  if (!sp.length) return "";
  const splitSum = sp.reduce((a, s) => a + Number(s.qty), 0);
  const prodQty = Number(String(r.prod_qty).replace(/,/g, "")) || 0;
  const remain = prodQty - splitSum;
  const parts = sp.map(s => {
    const p = M.partner.find(x => x.id === s.partner_id);
    return `${esc(p ? p.name : "미지정")} ${NF(s.qty)}`;
  });
  if (remain > 0) parts.push(`<b>보유 ${NF(remain)}</b>`);
  if (remain < 0) parts.push(`<span style="color:var(--crit)">⚠ 분배가 생산보다 ${NF(-remain)} 많음</span>`);
  return parts.join(" · ");
}
function prodSplitCell(r, i) {
  const sp = (r.prodSplits || []).filter(s => Number(s.qty) > 0);
  const sum = prodSplitSummary(r);
  return `<div style="display:flex; flex-direction:column; align-items:flex-end; gap:2px;">
    <input class="mini-input num" data-f="prod_qty" value="${r.prod_qty}">
    ${r.product_id ? `<button class="btn ghost sm" data-prodsplit="${i}"
      style="font-size:10.5px; padding:1px 8px; white-space:nowrap; ${sp.length ? "border:1px solid var(--warn); color:#98670F; background:var(--warn-soft); border-radius:6px;" : ""}"
      title="생산 수량을 거래처별로 나눕니다 (남는 수량 = 보유 재고) — 납품처 지정 부재료가 각 분배량 기준으로 계산됩니다">${sp.length ? "🏷 분배 " + sp.length + "곳" : "＋ 거래처"}</button>` : ""}
    <div class="auto num psp-sum" style="font-size:10px; line-height:1.35; white-space:normal; max-width:120px; text-align:right; ${sum ? "" : "display:none;"}">${sum}</div>
  </div>`;
}
function renderProd() {
  $("eProd").innerHTML = E.prod.map((r, i) => {
    const good = (Number(r.prod_qty) || 0) - (Number(r.defect_qty) || 0);
    renderUsageSoon();
    const py = productById(r.product_id)?.batch_yield || 0;
    return `<tr data-i="${i}">
      <td>${selHtml(M.product.filter(p => p.status !== "단종"), r.product_id, "product_id", "name", "", "nm")}</td>
      <td>${prodLineSel(r.line_id)}</td>
      <td class="r"><input class="mini-input num" data-f="batches" value="${r.batches}" style="width:56px"
        title="${py ? "1배합 = " + NF(Math.round(py)) + "개 — 입력 시 계획 자동" : "제품에 1배합당 생산수량 등록 시 계획 자동"}"></td>
      <td class="r"><input class="mini-input num" data-f="plan_qty" value="${r.plan_qty}"></td>
      <td class="r">${prodSplitCell(r, i)}</td>
      <td class="r"><input class="mini-input num" data-f="defect_qty" value="${r.defect_qty}">
        <button class="btn ghost sm dfr-btn ${r.defect_reason ? "set" : ""}" data-dreason="${i}"
          style="${Number(String(r.defect_qty).replace(/,/g, "")) > 0 ? "" : "display:none"}"
          title="${esc(r.defect_reason || "불량 사유 입력")}">${r.defect_reason ? "🏷 " + esc(r.defect_reason.length > 8 ? r.defect_reason.slice(0, 8) + "…" : r.defect_reason) : "＋ 사유"}</button></td>
      <td class="r auto" title="달성률 = 생산 ${NF(Number(r.prod_qty) || 0)} ÷ 계획 ${NF(Number(r.plan_qty) || 0)}">${PCT(Number(r.prod_qty) || 0, Number(r.plan_qty) || 0)}</td>
      <td class="r auto" title="양품 = 생산 ${NF(Number(r.prod_qty) || 0)} − 불량 ${NF(Number(r.defect_qty) || 0)}">${r.prod_qty ? NF(good) : "—"}</td>
      <td>${prodExpiryBtn(r, i)}</td>
      <td><button class="btn ghost sm" data-del>삭제</button></td></tr>`;
  }).join("") || `<tr><td colspan="10" class="auto">+ 생산 행 추가를 누르세요</td></tr>`;
  if (typeof renderNeed === "function") renderNeed();   // 생산 행 추가/삭제 시 예상 소요 갱신
}
/* 생산 LOT 소비기한 분할 모달 */
const LSP = { idx: -1, rows: [] };
$("eProd").addEventListener("click", e => {
  const dr = e.target.closest("[data-dreason]");
  if (dr) { openDreason(+dr.dataset.dreason); return; }
  const ps = e.target.closest("[data-prodsplit]");
  if (ps) { openPlanSplit(+ps.dataset.prodsplit); return; }
  const b = e.target.closest("[data-lotsplit]"); if (!b) return;
  openLotSplit(+b.dataset.lotsplit);
});

/* 생산 거래처 분배 모달 — 분배 합계 ≤ 생산 수량, 남는 수량 = 보유(재고).
   생산 수량을 비우면 분배 합계가 생산이 되고, 거래처 1곳 + 수량 비움 = 생산 전량 자동 */
const PSP = { idx: -1, rows: [] };
function openPlanSplit(i) {
  const r = E.prod[i];
  if (!r || !r.product_id) return toast("제품을 먼저 선택하세요");
  PSP.idx = i;
  PSP.rows = (r.prodSplits || []).map(s => ({ partner_id: s.partner_id, qty: s.qty }));
  if (!PSP.rows.length) PSP.rows.push({ partner_id: null, qty: "" });
  const p = productById(r.product_id);
  $("planSplitTarget").textContent = p ? p.name : "?";
  $("planSplitProd").value = r.prod_qty || "";
  renderPlanSplitRows();
  $("planSplitOverlay").classList.add("on");
}
window.closePlanSplit = () => $("planSplitOverlay").classList.remove("on");
window.clearPlanSplit = () => {   // 분배 해제 — 생산은 직접 입력 방식으로 복귀
  if (PSP.idx >= 0 && E.prod[PSP.idx]) E.prod[PSP.idx].prodSplits = [];
  closePlanSplit();
  renderProd();
  toast("거래처 분배를 해제했습니다 — 생산수량을 직접 입력하세요");
};
function renderPlanSplitRows() {
  const partners = M.partner.filter(isSeller);
  $("planSplitBody").innerHTML = PSP.rows.map((s, i) => `<tr data-psi="${i}">
    <td><select class="mini-sel" data-psf="partner_id" style="min-width:150px">
      <option value="">— 거래처 —</option>
      ${partners.map(p => `<option value="${p.id}" ${p.id === s.partner_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
    </select></td>
    <td class="r"><input class="mini-input num" data-psf="qty" value="${s.qty ?? ""}" style="width:100px" inputmode="decimal"></td>
    <td><button class="btn ghost sm" data-psdel>삭제</button></td></tr>`).join("");
  updatePlanSplitSum();
}
function updatePlanSplitSum() {
  const sum = PSP.rows.reduce((a, s) => a + (Number(String(s.qty).replace(/,/g, "")) || 0), 0);
  const prod = Number(String($("planSplitProd").value).replace(/,/g, "")) || 0;
  const remain = prod - sum;
  $("planSplitSum").innerHTML = !prod
    ? `분배 합계 <b>${NF(sum)}</b>개 → 생산수량으로 입력됩니다`
    : remain >= 0
      ? `분배 <b>${NF(sum)}</b> / 생산 ${NF(prod)} · 보유 <b>${NF(remain)}</b>개`
      : `<span style="color:var(--crit)">⚠ 분배 ${NF(sum)}개가 생산 ${NF(prod)}개보다 많습니다</span>`;
}
$("planSplitProd").addEventListener("input", updatePlanSplitSum);
$("planSplitBody").addEventListener("input", e => {
  const tr = e.target.closest("tr[data-psi]"); if (!tr) return;
  const f = e.target.dataset.psf; if (!f) return;
  const s = PSP.rows[+tr.dataset.psi];
  s[f] = f === "partner_id" ? (e.target.value ? +e.target.value : null) : e.target.value;
  if (f === "qty") updatePlanSplitSum();
});
$("planSplitBody").addEventListener("click", e => {
  const d = e.target.closest("[data-psdel]"); if (!d) return;
  PSP.rows.splice(+d.closest("tr[data-psi]").dataset.psi, 1);
  if (!PSP.rows.length) PSP.rows.push({ partner_id: null, qty: "" });
  renderPlanSplitRows();
});
$("planSplitAdd").onclick = () => { PSP.rows.push({ partner_id: null, qty: "" }); renderPlanSplitRows(); };
$("planSplitSave").onclick = () => {
  const r = E.prod[PSP.idx]; if (!r) return closePlanSplit();
  const prodv = Number(String($("planSplitProd").value).replace(/,/g, "")) || 0;
  let rows = PSP.rows.filter(s => s.partner_id);
  if (!rows.length) return toast("거래처를 선택하세요 (분배를 안 쓰려면 [분배 해제])");
  // 거래처 1곳 + 수량 비움 = 생산수량 전량 자동
  if (rows.length === 1 && !(Number(String(rows[0].qty).replace(/,/g, "")) > 0))
    rows[0].qty = prodv;
  rows = rows.map(s => ({ partner_id: s.partner_id, qty: Number(String(s.qty).replace(/,/g, "")) || 0 }))
    .filter(s => s.qty > 0);
  if (!rows.length) return toast("수량을 입력하세요");
  const sum = rows.reduce((a, s) => a + s.qty, 0);
  if (prodv > 0 && sum > prodv)
    return toast(`⚠ 분배 합계 ${NF(sum)}개가 생산 ${NF(prodv)}개보다 많습니다`);
  r.prodSplits = rows;
  r.prod_qty = prodv > 0 ? prodv : sum;   // 생산 미입력 시 분배 합계 = 생산
  const remain = r.prod_qty - sum;
  closePlanSplit();
  renderProd();
  renderUsageSoon();                // 자재 사용 그룹 헤더 '생산 N개' 동기화
  applyPartnerPack(r.product_id);   // 납품처 지정 부재료 수량 재계산
  toast(`생산 ${NF(r.prod_qty)}개 — 거래처 ${rows.length}곳 분배${remain > 0 ? ` · 보유 ${NF(remain)}개` : ""} (저장 시 반영)`);
};
async function openLotSplit(i) {
  const r = E.prod[i];
  if (!r || !r.product_id) return toast("제품을 먼저 선택하세요");
  LSP.idx = i;
  LSP.rows = (r.lotSplits || []).map(s => ({ qty: s.qty, expiry: s.expiry,
    pack_mid: s.pack_mid || null, pack_set: s.pack_set || "", partner_id: s.partner_id || null }));
  if (!LSP.rows.length) LSP.rows.push({ qty: "", expiry: "", pack_mid: null });
  const p = productById(r.product_id);
  const prodQty = Number(String(r.prod_qty).replace(/,/g, "")) || 0;
  // 이 제품의 포장 후보 (배합비 + 오늘 자재 사용의 개수 부재료) — 구간마다 사용자가 어떤 포장인지 선택
  LSP.packs = [];
  try {
    await ensureBomAll();
    const seen = new Set();
    const addPack = mid => {
      const m = materialById(mid);
      if (isCountMat(m) && Number(m.pack_count) > 1 && !seen.has(m.id)) {
        seen.add(m.id);
        LSP.packs.push({ mid: m.id, name: m.name, pack: Number(m.pack_count) });
      }
    };
    (BOMALL[r.product_id] || []).forEach(b => addPack(b.material_id));
    E.usage.filter(u => u.product_id === r.product_id).forEach(u => addPack(u.material_id));
    LSP.packs.sort((a, b) => b.pack - a.pack);
    // 포장 선택 옵션 = 세트 묶음 + 어느 세트에도 안 든 개별 자재
    // 다대다: 한 자재가 여러 세트에 속하면 그 세트들이 각각 옵션으로 나온다
    const bySet = {};
    LSP.packOpts = [];
    LSP.packs.forEach(pk => {
      const sets = packSetsOf(pk.mid);
      if (sets.length) sets.forEach(s => (bySet[s] = bySet[s] || []).push(pk));
      else LSP.packOpts.push({ key: "mid:" + pk.mid, label: pk.name, pack: pk.pack, mids: [pk.mid] });
    });
    Object.entries(bySet).forEach(([name, members]) => {
      // 세트 전체 구성원 기준으로 표시 (이 제품 배합에 없는 구성원도 세트엔 포함)
      const all = packSetMembers(name);
      LSP.packOpts.push({ key: "set:" + name, label: "📦 " + name + " 세트 (" + (all.length || members.length) + "종)",
        pack: members[0].pack, mids: (all.length ? all.map(m => m.id) : members.map(m => m.mid)), isSet: true });
    });
    LSP.packOpts.sort((a, b) => b.pack - a.pack);
  } catch (e) { /* 환산 표시만 생략 */ }
  // 기존 재고 (이날 생산분 제외) — 소비기한별로 몇 개 남아있는지 참고용으로 표시
  LSP.exist = [];
  try {
    const ld = await api(`/api/lots/${r.product_id}?date=${E.date}`);
    E.shipLots[r.product_id] = ld.lots || [];
    const agg = {};
    (ld.lots || []).filter(l => l.made !== E.date).forEach(l => {
      const k = l.expiry || "";
      agg[k] = (agg[k] || 0) + Number(l.qty);
    });
    LSP.exist = Object.entries(agg).map(([expiry, qty]) => ({ expiry, qty }))
      .sort((a, b) => (a.expiry === "") - (b.expiry === "") || a.expiry.localeCompare(b.expiry));
  } catch (e) { /* 재고 조회 실패 시 참고 표시만 생략 */ }
  // 거래처 분배가 있으면 팝업에도 보여주고, 그 수량대로 구간을 만들 수 있게
  const sp = (r.prodSplits || []).filter(s => Number(s.qty) > 0);
  const remain = prodQty - sp.reduce((a, s) => a + Number(s.qty), 0);
  const spTxt = sp.map(s => {
    const pa = M.partner.find(x => x.id === s.partner_id);
    return `${esc(pa ? pa.name : "미지정")} ${NF(s.qty)}`;
  }).join(" · ");
  $("lotSplitTarget").innerHTML = `${esc(p ? p.name : "?")} · ${E.date} 생산 ${NF(prodQty)}개`
    + (sp.length ? `<br><span class="num" style="font-size:12px;">🏷 거래처 분배: ${spTxt}${remain > 0 ? ` · 보유 ${NF(remain)}` : ""}</span>
      <button class="btn ghost sm" id="lotSplitFromSplit" style="margin-left:6px; font-size:11px;"
        title="거래처 분배 수량 그대로 소비기한 구간을 만듭니다">분배대로 구간 나누기</button>` : "");
  // 기존 재고 요약 — 소비기한을 정할 때 몇 개짜리 재고가 남아있는지 참고
  const existTot = LSP.exist.reduce((a, x) => a + x.qty, 0);
  $("lotSplitExist").innerHTML = existTot > 0
    ? `📦 기존 재고 <b>${NF(Math.round(existTot))}개</b> — ` + LSP.exist.map(x =>
        `<span style="white-space:nowrap">${x.expiry ? "~" + x.expiry.slice(5) : "기한미상"} <b>${NF(Math.round(x.qty))}</b></span>`).join(" · ")
      + ` <span class="auto">(이 분할은 오늘 생산분 ${NF(prodQty)}개에만 적용 — 기존 재고 기한은 LOT 관리에서 변경)</span>`
    : `📦 기존 재고 없음 — 오늘 생산분이 첫 재고입니다`;
  renderLotSplitRows();
  const fs = $("lotSplitFromSplit");
  if (fs) fs.onclick = () => {
    LSP.rows = sp.map(s => ({ qty: s.qty, expiry: "", partner_id: s.partner_id || null }));
    if (remain > 0) LSP.rows.push({ qty: remain, expiry: "", partner_id: null });
    renderLotSplitRows();
    toast("분배 수량대로 구간을 만들었습니다 (납품처 자동 지정) — 각 구간의 소비기한을 선택하세요");
  };
  $("lotSplitOverlay").classList.add("on");
}
window.closeLotSplit = () => $("lotSplitOverlay").classList.remove("on");
// 구간 수량의 포장 환산 — 포장을 선택했으면 그 포장 기준만, 아니면 후보 전체 표시 (선택 유도)
// 선택한 포장의 개입수로 나누어떨어지지 않으면 빨간 경고 (적용도 차단됨)
function lsPackTxt(qty, row) {
  const q = Number(String(qty ?? "").replace(/,/g, "")) || 0;
  if (!(q > 0) || !(LSP.packOpts || []).length) return "";
  const cur = row && row.pack_set ? "set:" + row.pack_set : (row && row.pack_mid ? "mid:" + row.pack_mid : "");
  const o = (LSP.packOpts || []).find(x => x.key === cur);
  if (o) {   // 선택된 포장(세트/개별)의 박스 환산
    const box = Math.floor(q / o.pack), rest = q % o.pack;
    return (rest
      ? `<span style="color:var(--crit); font-weight:700">⚠ ${NF(o.pack)}개입에 맞지 않음 (${NF(box)}박스 +${NF(rest)}개)</span>`
      : `${NF(o.pack)}개입 <b>${NF(box)}</b>박스 <span style="color:var(--ok)">✓ 딱 맞음</span>`)
      + (o.isSet ? ` <span class="auto">· 세트 ${o.mids.length}종 함께 소모</span>` : "");
  }
  return `<span class="auto">포장 선택 전 (${LSP.packOpts.length}종 후보)</span>`;
}
// 그 소비기한의 기존 재고 안내문 (구간 행 옆 참고 표시)
function lsExistTxt(expiry) {
  if (!expiry) return "";
  const hit = (LSP.exist || []).find(x => x.expiry === expiry);
  return hit ? `기존 ${NF(Math.round(hit.qty))}개 보유` : "새 기한 (기존 재고 없음)";
}
function renderLotSplitRows() {
  $("lotSplitBody").innerHTML = LSP.rows.map((s, i) => `<tr data-si="${i}">
    <td title="이 구간이 어떤 포장으로 나가는지 선택 — 환산과 부재료(BOX) 사용량 계산 근거가 됩니다">
      ${(LSP.packOpts || []).length ? `<select class="mini-sel" data-sf="pack_key" style="max-width:190px; font-size:11.5px;">
        <option value="">— 포장 선택 —</option>
        ${LSP.packOpts.map(o => { const cur = s.pack_set ? "set:" + s.pack_set : (s.pack_mid ? "mid:" + s.pack_mid : "");
          return `<option value="${esc(o.key)}" ${o.key === cur ? "selected" : ""}>${NF(o.pack)}개입 · ${esc(o.label.length > 20 ? o.label.slice(0, 20) + "…" : o.label)}</option>`; }).join("")}
      </select>` : '<span class="auto" style="font-size:11px">개입수 등록된 포장 없음</span>'}
      <div class="auto num" style="font-size:11px; margin-top:2px;"><span data-lspk="${i}">${lsPackTxt(s.qty, s)}</span></div></td>
    <td><select class="mini-sel" data-sf="partner_id" style="max-width:130px; font-size:11.5px;"
        title="이 구간의 납품처 — 출고 시 이 LOT을 고르면 거래처가 자동 선택됩니다">
      <option value="">— 미지정 —</option>
      ${M.partner.filter(isSeller).map(p => `<option value="${p.id}" ${+s.partner_id === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
    </select></td>
    <td class="r"><input class="mini-input num" data-sf="qty" value="${s.qty ?? ""}" style="width:90px" inputmode="decimal"></td>
    <td><input class="mini-input datepick" type="text" readonly data-sf="expiry" value="${esc(s.expiry || "")}" placeholder="📅 소비기한" style="width:150px"></td>
    <td class="auto" style="font-size:11px; white-space:nowrap;"
      title="같은 소비기한의 기존 재고 — 이 구간을 저장하면 합쳐서 관리됩니다"><span data-lsex="${i}">${lsExistTxt(s.expiry)}</span></td>
    <td><button class="btn ghost sm" data-sdel>삭제</button></td></tr>`).join("");
  updateLotSplitSum();
}
function updateLotSplitSum() {
  const prod = Number(E.prod[LSP.idx]?.prod_qty) || 0;
  const sum = LSP.rows.reduce((a, s) => a + (Number(s.qty) || 0), 0);
  const rest = prod - sum;
  $("lotSplitSum").innerHTML = `지정 ${NF(sum)} / 생산 ${NF(prod)}` +
    (rest > 0.0005 ? ` · <span style="color:var(--muted)">남은 ${NF(rest)}개는 제품 소비일 자동</span>`
      : rest < -0.0005 ? ` · <span style="color:var(--crit); font-weight:700">${NF(-rest)}개 초과!</span>` : ` · <span style="color:var(--ok)">딱 맞음</span>`);
}
$("lotSplitBody").addEventListener("input", e => {
  const tr = e.target.closest("tr[data-si]"); if (!tr) return;
  const f = e.target.dataset.sf; if (!f) return;
  const i = +tr.dataset.si;
  if (f === "qty") {
    // 생산수량 초과 방지: 다른 구간 합 + 이 값이 생산수량을 넘으면 최대치로 제한
    const prod = Number(E.prod[LSP.idx]?.prod_qty) || 0;
    const others = LSP.rows.reduce((a, s, j) => j === i ? a : a + (Number(s.qty) || 0), 0);
    let v = Number(String(e.target.value).replace(/,/g, "")) || 0;
    const max = Math.max(0, prod - others);
    if (v > max) { v = max; e.target.value = String(max); toast(`생산수량(${NF(prod)}개) 내에서만 지정할 수 있습니다`); }
    LSP.rows[i].qty = e.target.value;
    updateLotSplitSum();   // 합계만 제자리 갱신 (표 재렌더 X → 포커스 유지)
    const pk = tr.querySelector(`[data-lspk="${i}"]`);
    if (pk) pk.innerHTML = lsPackTxt(e.target.value, LSP.rows[i]);   // 포장 환산 제자리 갱신
  } else if (f === "pack_key") {   // 값 = 'mid:123' 또는 'set:이름' 또는 ''
    const v = e.target.value;
    LSP.rows[i].pack_mid = v.startsWith("mid:") ? +v.slice(4) : null;
    LSP.rows[i].pack_set = v.startsWith("set:") ? v.slice(4) : "";
    const pk = tr.querySelector(`[data-lspk="${i}"]`);
    if (pk) pk.innerHTML = lsPackTxt(LSP.rows[i].qty, LSP.rows[i]);
  } else if (f === "partner_id") {
    LSP.rows[i].partner_id = e.target.value ? +e.target.value : null;
  } else {
    LSP.rows[i][f] = e.target.value;
    if (f === "expiry") {   // 그 기한의 기존 재고 안내 갱신
      const sp = tr.querySelector(`[data-lsex="${i}"]`);
      if (sp) sp.textContent = lsExistTxt(e.target.value);
    }
  }
});
$("lotSplitBody").addEventListener("click", e => {
  const d = e.target.closest("[data-sdel]"); if (!d) return;
  LSP.rows.splice(+d.closest("tr[data-si]").dataset.si, 1);
  if (!LSP.rows.length) LSP.rows.push({ qty: "", expiry: "", pack_mid: null });
  renderLotSplitRows();
});
$("lotSplitAdd").onclick = () => { LSP.rows.push({ qty: "", expiry: "", pack_mid: null }); renderLotSplitRows(); };
$("lotSplitSave").onclick = () => {
  const pr = E.prod[LSP.idx];
  const prod = Number(pr?.prod_qty) || 0;
  const valid = LSP.rows.filter(s => s.expiry && Number(s.qty) > 0);
  const sum = valid.reduce((a, s) => a + Number(s.qty), 0);
  if (sum - prod > 0.5) return toast(`지정 수량(${NF(sum)})이 생산수량(${NF(prod)})을 초과합니다`);
  // 포장(세트/개별)을 선택한 구간은 수량이 개입수로 나누어떨어져야 적용 가능
  for (const s of valid) {
    const cur = s.pack_set ? "set:" + s.pack_set : (s.pack_mid ? "mid:" + s.pack_mid : "");
    if (!cur) continue;
    const o = (LSP.packOpts || []).find(x => x.key === cur);
    if (o && Number(s.qty) % o.pack !== 0)
      return toast(`⚠ ${NF(s.qty)}개는 ${NF(o.pack)}개입에 맞지 않습니다 — ${NF(Math.floor(s.qty / o.pack) * o.pack)} 또는 ${NF(Math.ceil(s.qty / o.pack) * o.pack)}개로 맞춰주세요`);
  }
  pr.lotSplits = valid.map(s => ({ qty: Number(s.qty), expiry: s.expiry, pack_mid: s.pack_mid || null, pack_set: s.pack_set || "", partner_id: s.partner_id || null }));
  pr.expiry = "";   // 분할을 쓰면 단일 기한은 비움
  closeLotSplit();
  renderProd();
  updatePackUsage(pr.product_id);   // 포장 선택에 따라 부재료(BOX) 사용량 재계산
  toast(valid.length ? `소비기한 ${valid.length}구간 설정됨 — 저장 시 반영` : "소비기한 분할 해제됨");
};
// 출고·분배 대상 거래처 = 자재 공급처/용역업체가 아닌 모든 유형 (직접 입력 유형 포함 — 예: 기부)
function isSeller(p) { return p.status !== "중지" && p.type !== "자재 공급처" && p.type !== "용역업체"; }
// 제품의 이날 가용 재고 = 현재고 + 이날 이미 저장된 출고분 (이날 편집 중 출고는 이 안에서만 가능)
function shipAvail(pid) {
  if (!pid) return Infinity;
  const p = productById(pid);
  return (p ? Number(p.stock) || 0 : 0) + (E.shipBase?.[pid] || 0);
}
function shipSumByProduct() {
  const s = {};
  E.ship.forEach(r => { if (r.product_id) s[r.product_id] = (s[r.product_id] || 0) + (Number(r.qty) || 0); });
  return s;
}
// 행이 가리키는 LOT 찾기 (생산일+소비기한+번호)
function shipLotOf(r) {
  return (E.shipLots[r.product_id] || []).find(l =>
    (l.made || "") === (r.prod_date || "") && (l.expiry || "") === (r.lotExpiry || "")
    && (l.no || 0) === (r.lotNo || 0));
}
// 선택한 LOT의 재고 초과분 (0 이하 = 정상). LOT 정보 없으면 0 — 총재고 검증은 별도
function shipLotOver(r) {
  if (!r.product_id || !(r.prod_date || r.lotExpiry)) return 0;
  const lot = shipLotOf(r);
  if (!lot) return 0;
  return (Number(String(r.qty).replace(/,/g, "")) || 0) - lot.qty;
}
function shipWarnText(r, over, avail) {
  if (over) return `⚠ 재고 ${NF(avail)} 초과`;
  const lo = shipLotOver(r);
  return lo > 0.5 ? `⚠ 선택 LOT 재고 초과 — 초과분은 다른 LOT에서 자동(FIFO) 차감` : "";
}
function renderShip() {
  const sum = shipSumByProduct();
  // 출고 제품 목록 = 재고가 있거나 이날 생산 입력이 있는 제품만 (전 제품이 다 뜨지 않게)
  const prodToday = new Set(E.prod.filter(x => x.product_id &&
    Number(String(x.prod_qty).replace(/,/g, "")) > 0).map(x => x.product_id));
  const shipProducts = r => M.product.filter(p => p.status !== "단종" &&
    (p.id === r.product_id || prodToday.has(p.id) || shipAvail(p.id) > 0.0005));
  $("eShip").innerHTML = E.ship.map((r, i) => {
    const avail = shipAvail(r.product_id);
    const over = r.product_id && sum[r.product_id] - avail > 0.5;
    const warnTxt = r.product_id ? shipWarnText(r, over, avail) : "";
    return `<tr data-i="${i}">
    <td>${selHtml(shipProducts(r), r.product_id, "product_id", "name", "", "nm")}</td>
    <td>${shipLotSel(r)}</td>
    <td>${selHtml(M.partner.filter(isSeller), r.partner_id, "partner_id")}</td>
    <td class="r"><input class="mini-input num${over ? " ship-over" : ""}" data-f="qty" value="${r.qty || ""}"
      title="${r.product_id ? "이날 가용 재고 " + NF(avail) + "개" : ""}"></td>
    <td>${r.product_id ? `<span class="ship-warn num" style="color:${over ? "var(--crit)" : "#B45309"}; font-size:11px; ${warnTxt ? "" : "display:none"}">${warnTxt}</span> `
      + `<button class="btn ghost sm" data-shipall="${i}" title="선택한 LOT(또는 제품)의 재고 전량을 출고량에 입력">전량</button> ` : ""}<button class="btn ghost sm" data-del>삭제</button></td></tr>`;
  }).join("")
    || `<tr><td colspan="5" class="auto">+ 출고 행 추가를 누르세요</td></tr>`;
}
// 출고량 타이핑 시 재고 초과 표시만 제자리 갱신 (표 재렌더 X → 포커스 유지)
function updateShipWarn() {
  const sum = shipSumByProduct();
  document.querySelectorAll("#eShip tr[data-i]").forEach(tr => {
    const r = E.ship[+tr.dataset.i]; if (!r) return;
    const avail = shipAvail(r.product_id);
    const over = r.product_id && sum[r.product_id] - avail > 0.5;
    const inp = tr.querySelector('[data-f="qty"]');
    if (inp) inp.classList.toggle("ship-over", !!over);
    const w = tr.querySelector(".ship-warn");
    if (w) {
      const t = shipWarnText(r, over, avail);
      w.style.display = t ? "" : "none";
      w.style.color = over ? "var(--crit)" : "#B45309";
      w.textContent = t;
    }
  });
}
// [전량] 버튼 — 선택 LOT(또는 제품 전체)의 재고를 출고량에 한번에 입력
$("eShip").addEventListener("click", e => {
  const b = e.target.closest("[data-shipall]"); if (!b) return;
  const r = E.ship[+b.dataset.shipall];
  if (!r || !r.product_id) return toast("제품을 먼저 선택하세요");
  const lots = E.shipLots[r.product_id] || [];
  let qty;
  if (r.prod_date || r.lotExpiry) {   // 특정 LOT 선택 → 그 LOT 재고
    const lot = shipLotOf(r);
    qty = lot ? lot.qty : shipAvail(r.product_id);
  } else {                            // 자동(FIFO) → 제품 전체 가용 재고
    qty = shipAvail(r.product_id);
  }
  r.qty = Math.round((Number(qty) || 0) * 1000) / 1000;
  renderShip();
  toast(`전량 ${NF(r.qty)}개 입력됨`);
});
/* 출고 생산일자(LOT) 선택: 그날 남아있는 생산일자별 재고를 불러와 표시 */
const _lotFetching = {};
function shipLotSel(r) {
  if (!r.product_id) return '<span class="auto" style="font-size:12px">제품 먼저 선택</span>';
  const lots = E.shipLots[r.product_id];
  if (!lots) { fetchShipLots(r.product_id); return '<span class="auto" style="font-size:12px">재고 확인 중…</span>'; }
  // 같은 (생산일,소비기한) LOT이 여럿이면 서버가 no(1,2,3…)를 부여 — 옵션 값에 포함해 구분·선택 가능하게
  const key = l => (l.made || "") + "|" + (l.expiry || "") + "|" + (l.no || 0);
  const cur = (r.prod_date || "") + "|" + (r.lotExpiry || "") + "|" + (r.lotNo || 0);
  const known = !r.prod_date || lots.some(l => key(l) === cur);
  const pName = pid => { const pa = M.partner.find(x => x.id === pid); return pa ? pa.name : ""; };
  const label = l => `${l.made || "생산일 미상 (이월)"}${l.no ? " #" + l.no : ""} · 재고 ${NF(l.qty)}${l.expiry ? " · ~" + l.expiry.slice(5) : ""}${l.partner_id ? " · " + pName(l.partner_id) : ""}`;
  return `<select class="mini-sel" data-f="prod_date" style="max-width:270px">
    <option value="||0">자동 — 소비기한 임박부터 (FIFO)</option>
    ${lots.map(l => `<option value="${key(l)}" ${key(l) === cur ? "selected" : ""}>${label(l)}</option>`).join("")}
    ${!known ? `<option value="${cur}" selected>⚠ 선택한 재고 LOT 없음</option>` : ""}</select>`;
}
async function fetchShipLots(pid) {
  if (!E.date || _lotFetching[pid]) return;
  _lotFetching[pid] = true;
  try {
    const d = await api(`/api/lots/${pid}?date=${E.date}`);
    E.shipLots[pid] = d.lots || [];
  } catch (e) {
    E.shipLots[pid] = [];
  } finally {
    delete _lotFetching[pid];
    renderShip();
  }
}
/* 입고 카드 합계 — 실사 카드의 '입고'는 여기서 계산 (입고 카드에 그 자재가 없으면 과거 저장분 폴백) */
function matInTotal(mid) {
  return (E.matIn || []).filter(x => x.material_id === mid)
    .reduce((s, x) => s + (Number(x.qty) || 0), 0);
}
function effIn(r) {
  return (E.matIn || []).some(x => x.material_id === r.material_id)
    ? matInTotal(r.material_id) : (Number(r.in_qty) || 0);
}
function renderMatIn() {
  const all = M.raw.concat(M.sub);
  $("eMatIn").innerHTML = E.matIn.map((r, i) => {
    const m = materialById(r.material_id) || {};
    return `<tr data-i="${i}">
      <td>${matSel(r.material_id, 'data-f="material_id"', NEWMAT_OPTS)}</td>
      <td class="auto">${esc(m.unit || "")}</td>
      <td class="r"><input class="mini-input num" data-f="qty" value="${r.qty ?? ""}"></td>
      <td><input class="mini-input datepick" type="text" readonly data-f="expiry" value="${esc(r.expiry || "")}" placeholder="📅 유통기한" style="width:135px"></td>
      <td><input class="mini-input w num" style="text-align:left" data-f="note" value="${esc(r.note || "")}" placeholder="메모 (공급처 등)"></td>
      <td><button class="btn ghost sm" data-del>삭제</button></td></tr>`;
  }).join("") || `<tr><td colspan="6" class="auto">+ 입고 행 추가 — 재고 실사 카드에 발주량·발주일을 적어두면 입고 전까지 여기 자동으로 나타납니다</td></tr>`;
}
// 이날 기록된 제품별 사용 합 (실사 사용량과의 차이 = 로스/조정)
function usageSumOf(mid) {
  return E.usage.filter(u => u.material_id === mid)
    .reduce((s, u) => s + (Number(String(u.qty).replace(/,/g, "")) || 0), 0);
}
// 실사 사용량 대비 기록 사용 합의 차이 표시 (+ = 기록보다 더 씀(로스) / − = 덜 씀)
function lossHtml(mid, used) {
  const rec = usageSumOf(mid);
  if (!(rec > 0)) return "";
  const diff = Math.round((used - rec) * 1000) / 1000;
  if (Math.abs(diff) < 0.0005) return "";
  return `<div class="auto" style="font-size:10.5px; color:${diff > 0 ? "#B45309" : "var(--ok)"}"
    title="실사 사용량 ${NF(used)} − 기록된 제품별 사용 합 ${NF(rec)} — 국자 계량 오차·흘림 등 자연 로스는 이 값으로 흡수하면 되고, 입고를 가짜로 넣을 필요 없습니다">로스 ${diff > 0 ? "+" : ""}${NF(diff)}</div>`;
}
function renderMat() {
  const all = M.raw.concat(M.sub);
  // 실사 행이 있는 자재는 자동 반영 행을 숨김 (실사 우선 — 저장 시에도 실사 기준으로 계산됨)
  const manualIds = new Set(E.mat.map(r => r.material_id).filter(Boolean));
  const rows = E.mat.map((r, i) => {
    const m = materialById(r.material_id) || {};
    const prev = r.prev_qty !== "" && r.prev_qty != null ? Number(r.prev_qty) : (E.prevStock[r.material_id] ?? 0);
    const inq = effIn(r);
    const used = prev + inq - (Number(r.real_qty) || 0);
    const replacing = (E.autoMat || []).some(a => a.material_id === r.material_id);
    // select가 width:100%라 칩을 그냥 이어붙이면 옆 칸(단위)을 침범한다 — .namecell flex로 나란히
    return `<tr data-i="${i}">
      <td><div class="namecell">
        ${matSel(r.material_id, 'data-f="material_id"', NEWMAT_OPTS)}${replacing
        ? '<span class="chip cat" style="flex:none; white-space:nowrap" title="이 자재는 자동 반영(제품별 사용 합계) 대신 이 실사 값이 우선 적용됩니다 — 자동 반영 행은 숨겨집니다">실사 우선</span>' : ""}
      </div></td>
      <td class="auto">${esc(m.unit || "")}</td>
      <td class="r auto">${NF(prev)}</td>
      <td class="r auto" ${inq > 0 ? 'style="color:var(--ok); font-weight:700"' : ""} title="입고는 '원부자재 입고' 카드에서 입력">${inq ? "+" + NF(inq) : "0"}</td>
      <td class="r"><input class="mini-input num" data-f="real_qty" value="${r.real_qty}"></td>
      <td class="r">${r.real_qty !== "" && r.material_id
        ? `<button class="uselink num" data-use="${r.material_id}">${NF(used)}</button>${lossHtml(r.material_id, used)}` : '<span class="auto">—</span>'}</td>
      <td class="r"><input class="mini-input num" data-f="order_qty" value="${r.order_qty || ""}" style="width:64px"></td>
      <td><input class="mini-input datepick" type="text" readonly style="text-align:left; width:130px" data-f="order_date" value="${esc(r.order_date)}" placeholder="📅 발주/입고예정"></td>
      <td><button class="btn ghost sm" data-del>삭제</button></td></tr>`;
  }).join("");
  const autoRows = (E.autoMat || []).filter(r => !manualIds.has(r.material_id)).map(r => `<tr style="color:var(--muted)">
    <td>${esc(r.name)} <span class="chip cat">자동 반영</span></td>
    <td>${esc(r.unit)}</td><td class="r">${NF(r.prev_qty)}</td>
    <td class="r" ${r.in_qty > 0 ? 'style="color:var(--ok); font-weight:700"' : ""}>${r.in_qty ? "+" + NF(r.in_qty) : "0"}</td>
    <td class="r"><button class="uselink num" data-fixreal="${r.material_id}"
      title="실재고가 맞지 않으면 클릭해서 직접 입력 — 자동 계산 대신 이 값이 적용됩니다">${NF(r.real_qty)} ✏️</button></td>
    <td class="r"><button class="uselink num" data-use="${r.material_id}">${NF(r.used_qty)}</button></td>
    <td colspan="3" class="hint" style="white-space:normal">전일재고 + 입고 − 제품별 사용 합계로 자동 계산 — 실재고를 클릭하면 실사로 고칠 수 있습니다</td></tr>`).join("");
  $("eMat").innerHTML = (rows + autoRows)
    || `<tr><td colspan="9" class="auto">+ 자재 행 추가 또는 직전 기록일 자재 불러오기 · 실사가 없으면 입고·제품별 사용으로 자동 계산됩니다</td></tr>`;
}
// 자동 반영 행 전용 동작 — 이 행들은 tr[data-i]가 없어 wireEntryTable 핸들러가 잡지 못한다
$("eMat").addEventListener("click", e => {
  if (e.target.closest("tr[data-i]")) return;      // 실사 행은 wireEntryTable이 처리
  const fix = e.target.closest("[data-fixreal]");
  if (fix) {
    // 실재고가 안 맞으면 클릭 → 실사 행으로 승격해 직접 고칠 수 있게 (실사가 자동 계산보다 우선)
    const mid = +fix.dataset.fixreal;
    const a = (E.autoMat || []).find(x => x.material_id === mid);
    if (!a) return;
    E.mat.push({ material_id: mid, prev_qty: a.prev_qty, in_qty: a.in_qty,
                 real_qty: a.real_qty, order_date: "", order_qty: "" });
    renderMat();
    const inp = $("eMat").querySelector(`tr[data-i="${E.mat.length - 1}"] [data-f="real_qty"]`);
    if (inp) { inp.focus(); inp.select(); }
    toast(`'${a.name}' 실사 행으로 옮겼습니다 — 실재고를 고치고 [재고·입고 저장]을 누르세요`);
    return;
  }
  const u = e.target.closest("[data-use]");        // 자동 반영 행의 사용량 클릭 → 사용처 분석
  if (u) openUse(+u.dataset.use, E.date);
});
function staffRate(r) {   // 가동률 = 실가동 ÷ 목표가동(그날 입력, 없으면 라인 정상가동시간)
  const line = M.line.find(l => l.id === r.line_id);
  const std = Number(r.target_hours) > 0 ? Number(r.target_hours) : (line ? line.std_hours : 0);
  return std > 0 && r.work_hours ? PCT(r.work_hours, std) : "—";
}
function renderStaff() {
  const admin = canM("labor");   // 시급 입력칸 노출 여부
  $("eStaff").innerHTML = E.staff.map((r, i) => {
    const line = M.line.find(l => l.id === r.line_id);
    const rate = staffRate(r);
    const ids = (r.members || []).map(m => m.id);
    // 정직원(등록된 직원) 칩 — 이름 + 개인 투입시간
    const chips = (r.members || []).map(m => {
      const st = M.staff.find(s => s.id === m.id);
      return st ? `<span class="member-chip">${esc(st.name)}
        <input class="mini-input num" data-mh="${m.id}" value="${m.h ?? ""}" placeholder="h"
          title="이 인원의 투입 시간 (노무비 = 시급 × 시간)" style="width:38px; padding:1px 3px; font-size:11px;">h
        <button data-rm="${m.id}">✕</button></span>` : "";
    }).join("");
    // 용역 칩 — 업체·성별 + 각자 투입시간 + 개인별 시급 묶음 (시급은 admin만)
    const agencyPartners = M.partner.filter(p => p.status !== "중지" && p.type === "용역업체");
    const agChips = (r.agency || []).map((a, ai) => `<span class="member-chip agstack" style="background:var(--bg); color:var(--muted)">
        <span style="white-space:nowrap">
          <select class="mini-sel" data-apt="${ai}" title="용역 업체 (거래처에 '용역업체' 유형으로 등록)" style="max-width:88px; font-size:10.5px; padding:1px 2px;">
            <option value="">업체—</option>
            ${agencyPartners.map(p => `<option value="${p.id}" ${p.id === a.pid ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
          </select>
          <select class="mini-sel" data-agd="${ai}" title="성별" style="max-width:46px; font-size:10.5px; padding:1px 2px;">
            <option value="">—</option>
            <option value="남" ${a.g === "남" ? "selected" : ""}>남</option>
            <option value="여" ${a.g === "여" ? "selected" : ""}>여</option>
          </select>
          <button data-arm="${ai}">✕</button></span>
        <span style="white-space:nowrap">용역
          <input class="mini-input num" data-ah="${ai}" value="${a.h ?? ""}" placeholder="h"
            title="이 용역 인원의 투입 시간" style="width:38px; padding:1px 3px; font-size:11px;">h
          ${admin ? `<span style="font-size:10.5px;">시급
          <input class="mini-input num" data-aw="${ai}" value="${a.w ?? ""}" placeholder="원"
            title="이 용역 인원의 시급 (노무비 = 시간 × 시급)" style="width:58px; padding:1px 3px; font-size:11px;"></span>` : ""}</span>
      </span>`).join("");
    // ＋ 인원 추가: 맨 위에 '＋ 용역(이름없음)', 그 아래 등록된 직원
    const addSel = `<select class="mini-sel" data-addmember style="max-width:140px"><option value="">＋ 인원 추가</option>` +
      `<option value="__agency__">＋ 용역 (이름 없음)</option>` +
      `<optgroup label="정직원 · 등록 직원">` +
      M.staff.filter(s => s.status !== "퇴사" && !ids.includes(s.id))
        .map(s => `<option value="${s.id}">${esc(s.name)}${s.kind === "용역" ? " (용역)" : ""}</option>`).join("") + "</optgroup></select>";
    const named = (r.members || []).length, agency = (r.agency || []).length;
    const total = named + agency || (Number(r.headcount) || 0);
    return `<tr data-i="${i}">
      <td>${selHtml(M.line, r.line_id, "line_id", "disp")}</td>
      <td style="white-space:normal; min-width:340px; max-width:560px; width:520px; line-height:2;">${chips}${agChips}${addSel}</td>
      <td class="r auto" title="정직원 ${named} + 용역 ${agency}">${NF(total)}</td>
      <td class="r"><input class="mini-input num" data-f="target_hours" value="${r.target_hours || ""}" style="width:56px"
        placeholder="${line && line.std_hours ? line.std_hours : ""}" title="그날의 목표가동 시간 — 비우면 라인 정상가동시간(${line && line.std_hours ? line.std_hours + "h" : "미설정"}) 사용"></td>
      <td class="r"><input class="mini-input num" data-f="work_hours" value="${r.work_hours || ""}" style="width:56px"></td>
      <td class="r auto">${rate}</td>
      <td><button class="stopbtn ${r.stop_reason ? "hastext" : ""}" data-stop>${r.stop_reason ? esc(r.stop_reason.slice(0, 14)) + (r.stop_reason.length > 14 ? "…" : "") : "＋ 정지사유"}</button></td>
      <td><button class="btn ghost sm" data-del>삭제</button></td></tr>`;
  }).join("") || `<tr><td colspan="8" class="auto">+ 라인 행 추가를 누르세요</td></tr>`;
}

// addProd/addShip/addMat/addMatIn 버튼은 아래 wireQuickAdd가 연결 (검색어 있으면 그 항목으로, 없으면 빈 행)
$("addStaff").onclick = () => { mustDate() && (E.staff.push({ line_id: null, headcount: "", agency: [], agency_wage: "", target_hours: "", work_hours: "", stop_reason: "", members: [] }), renderStaff()); };
// 칩의 개인별 투입 시간 입력 (data-mh=정직원 / data-ah=용역 — 재렌더 없이 값만 갱신해 포커스 유지)
$("eStaff").addEventListener("input", e => {
  const tr = e.target.closest("tr[data-i]"); if (!tr) return;
  const row = E.staff[+tr.dataset.i];
  const mh = e.target.dataset.mh, ah = e.target.dataset.ah, aw = e.target.dataset.aw;
  const agd = e.target.dataset.agd, apt = e.target.dataset.apt;
  if (mh) { const m = (row.members || []).find(x => x.id === +mh); if (m) m.h = e.target.value; }
  else if (ah != null) { if (row.agency && row.agency[+ah]) row.agency[+ah].h = e.target.value; }
  else if (aw != null) { if (row.agency && row.agency[+aw]) row.agency[+aw].w = e.target.value; }
  else if (agd != null) { if (row.agency && row.agency[+agd]) row.agency[+agd].g = e.target.value; }
  else if (apt != null) { if (row.agency && row.agency[+apt]) row.agency[+apt].pid = e.target.value ? +e.target.value : null; }
});
function mustDate() { if (!E.date) { toast("달력에서 날짜를 먼저 선택하세요"); return false; } return true; }

/* 🔍 검색해서 행 추가 — 데이터 많은 카드 공통.
   이름 입력 후 Enter 또는 [+ 행 추가] 클릭 = 그 항목으로 행 추가. 검색칸이 비어 있으면 [+ 행 추가]는 빈 행. */
function wireQuickAdd(inputId, listId, getItems, onPick, addBtnId, addBlank) {
  const inp = $(inputId);
  const refresh = () => { $(listId).innerHTML = getItems().map(o => `<option value="${esc(o.name)}">`).join(""); };
  inp.addEventListener("focus", refresh);
  function pick() {
    const t = inp.value.trim();
    if (!t || !mustDate()) return;
    const items = getItems();
    let hit = items.find(o => o.name === t);
    if (!hit) {
      const cands = items.filter(o => o.name.toLowerCase().includes(t.toLowerCase()));
      if (!cands.length) return toast(`'${t}' 검색 결과 없음`);
      if (cands.length > 1) return toast(`'${t}' 검색 결과 ${cands.length}건 — 목록에서 정확한 이름을 선택하세요`);
      hit = cands[0];
    }
    inp.value = "";
    onPick(hit);
    toast(`'${hit.name}' 행 추가됨`);
  }
  // 타이핑 중 자동 등록 없음 — Enter 또는 [+ 행 추가]로만 확정
  inp.addEventListener("keydown", e => { if (e.key === "Enter") pick(); });
  $(addBtnId).onclick = () => {
    if (!mustDate()) return;
    if (inp.value.trim()) { pick(); return; }   // 검색어가 있으면 그 항목으로 행 추가
    addBlank();
  };
}
wireQuickAdd("qaProd", "qaProducts", () => M.product.filter(p => p.status !== "단종"), hit => {
  if (E.prod.some(r => r.product_id === hit.id)) return toast(`'${hit.name}'은 이미 생산실적에 있습니다`);
  E.prod.push({ product_id: hit.id, line_id: hit.line_id || null, batches: "", plan_qty: "", prod_qty: "", defect_qty: "", lotSplits: [], expiry: "" });
  renderProd(); renderUsage();
  autoFillUsage(hit.id);   // 배합비 원·부재료 자동 채움
}, "addProd", () => { E.prod.push({ product_id: null, line_id: null, batches: "", plan_qty: "", prod_qty: "", defect_qty: "", lotSplits: [], expiry: "" }); renderProd(); });
wireQuickAdd("qaShip", "qaProducts", () => M.product.filter(p => p.status !== "단종"), hit => {
  E.ship.push({ product_id: hit.id, partner_id: null, qty: "", prod_date: "", lotExpiry: "", lotNo: 0 });
  renderShip();
}, "addShip", () => { E.ship.push({ product_id: null, partner_id: null, qty: "", prod_date: "", lotExpiry: "", lotNo: 0 }); renderShip(); });
wireQuickAdd("qaMat", "qaMaterials", () => M.raw.concat(M.sub), hit => {
  if (E.mat.some(r => r.material_id === hit.id)) return toast(`'${hit.name}'은 이미 실사 목록에 있습니다`);
  E.mat.push({ material_id: hit.id, prev_qty: E.prevStock[hit.id] ?? 0, in_qty: "", real_qty: "", order_date: "", order_qty: "" });
  renderMat();
}, "addMat", () => { E.mat.push({ material_id: null, prev_qty: "", in_qty: "", real_qty: "", order_date: "", order_qty: "" }); renderMat(); });
wireQuickAdd("qaMatIn", "qaMaterials", () => M.raw.concat(M.sub), hit => {
  E.matIn.push({ material_id: hit.id, qty: "", expiry: "", note: "" });
  renderMatIn(); renderMat();
}, "addMatIn", () => { E.matIn.push({ material_id: null, qty: "", expiry: "", note: "" }); renderMatIn(); });
// 기록조회·분석의 제품 검색창도 클릭 시 전체 제품 드롭다운 (datalist 공유, 포커스 때 최신화)
["lkSearch", "anaRotFilter"].forEach(id => {
  $(id).addEventListener("focus", () => {
    $("qaProducts").innerHTML = M.product.map(o => `<option value="${esc(o.name)}">`).join("");
  });
});

function wireEntryTable(tbodyId, arr, rerender, liveUpdate) {
  $(tbodyId).addEventListener("input", e => {
    const tr = e.target.closest("tr[data-i]"); if (!tr) return;
    const f = e.target.dataset.f; if (!f) return;
    const row = arr()[+tr.dataset.i];
    // 자재 선택에서 '➕ 새 자재 등록' → 등록 팝업, 저장 시 이 행에 자동 선택
    if (f === "material_id" && String(e.target.value).startsWith("__new_")) {
      pendingNewMat = { list: tbodyId, index: +tr.dataset.i };
      const kind = e.target.value === "__new_raw__" ? "raw" : "sub";
      e.target.value = row.material_id || "";
      openMaster(kind, null);
      return;
    }
    const prodOldPid = (f === "product_id" && tbodyId === "eProd") ? row.product_id : null;
    row[f] = e.target.tagName === "SELECT"
      ? (f === "prod_date" ? e.target.value : (e.target.value ? +e.target.value : null))
      : e.target.value;
    if (f === "material_id" && row.material_id != null) row.prev_qty = E.prevStock[row.material_id] ?? 0;
    if (f === "product_id" && tbodyId === "eProd" && prodOldPid !== row.product_id) {
      // 제품 변경 = 이전 제품의 자재 사용 정리 + 새 제품 배합비 자동 채움
      cleanupUsageFor(prodOldPid);
      autoFillUsage(row.product_id);
    }
    if (f === "product_id" && tbodyId === "eShip") { row.prod_date = ""; row.lotExpiry = ""; row.lotNo = 0; }   // 제품 바뀌면 LOT 초기화
    if (f === "prod_date" && tbodyId === "eShip") {   // 옵션값 "생산일|소비기한|번호" 파싱
      const [made, exp, no] = String(e.target.value).split("|");
      row.prod_date = made || "";
      row.lotExpiry = exp || "";
      row.lotNo = +no || 0;
      // 이 LOT에 지정된 납품처가 있으면 거래처를 자동 선택 (사용자가 이미 고른 거래처는 유지)
      const lot = shipLotOf(row);
      if (lot && lot.partner_id && !row.partner_id) row.partner_id = lot.partner_id;
    }
    // SELECT 변경만 전체 재렌더 — 숫자 타이핑은 계산 셀만 제자리 갱신 (포커스 유지)
    if (e.target.tagName === "SELECT") { rerender(); return; }
    if (liveUpdate) liveUpdate(tr, row);
  });
  $(tbodyId).addEventListener("click", e => {
    const tr = e.target.closest("tr[data-i]"); if (!tr) return;
    const i = +tr.dataset.i;
    if (e.target.closest("[data-del]")) {
      const delRow = arr()[i];
      arr().splice(i, 1); rerender();
      // 생산행 삭제 → 그 제품의 자재 사용 기록도 함께 정리 (같은 제품 행이 남아 있으면 유지)
      if (tbodyId === "eProd" && delRow && delRow.product_id) cleanupUsageFor(delRow.product_id);
    }
    if (e.target.closest("[data-use]")) openUse(+e.target.dataset.use, E.date);
    if (e.target.closest("[data-stop]")) openStop(i);
    if (e.target.closest("[data-rm]")) {
      const row = arr()[i];
      row.members = (row.members || []).filter(m => m.id !== +e.target.dataset.rm);
      row.headcount = row.members.length; rerender();
    }
    if (e.target.closest("[data-arm]")) {   // 용역 칩 삭제
      const row = arr()[i];
      row.agency = (row.agency || []).filter((_, ai) => ai !== +e.target.closest("[data-arm]").dataset.arm);
      rerender();
    }
  });
  $(tbodyId).addEventListener("change", e => {
    if (e.target.matches("[data-addmember]") && e.target.value) {
      const tr = e.target.closest("tr[data-i]");
      const row = arr()[+tr.dataset.i];
      // 새 인원의 시간 기본값 = 라인 실가동 시간 (비어있으면 직접 입력)
      if (e.target.value === "__agency__") {   // 이름 없는 용역 한 명 추가 (여러 번 = 여러 명)
        const last = (row.agency || [])[row.agency ? row.agency.length - 1 : -1];
        row.agency = (row.agency || []).concat({ h: row.work_hours || "",
          w: (last && last.w) || row.agency_wage || "",     // 시급·업체·성별 기본값 = 직전 용역
          g: (last && last.g) || "", pid: (last && last.pid) || null });
      } else {
        row.members = (row.members || []).concat({ id: +e.target.value, h: row.work_hours || "" });
        row.headcount = row.members.length;
      }
      rerender();
    }
  });
}
let _batchApplyTimer = null;
wireEntryTable("eProd", () => E.prod, renderProd, (tr, row) => {
  // 배합 입력 → 계획 자동 (배합 × 제품 1배합당 생산수량)
  const py = productById(row.product_id)?.batch_yield || 0;
  const b = Number(row.batches) || 0;
  if (b > 0 && py > 0 && document.activeElement === tr.querySelector('[data-f="batches"]')) {
    row.plan_qty = Math.round(b * py);
    tr.querySelector('[data-f="plan_qty"]').value = row.plan_qty;
    // 배합 수 입력 → 대표(반죽) 블록 자재를 배합비 × 배율로 자동 채움 (입력 멈추면 적용)
    // BOMALL 로딩 후 대표 블록을 판정해야 정확 (미로딩 상태에서 판정하면 포장 블록이 빠지는 간헐 버그)
    clearTimeout(_batchApplyTimer);
    _batchApplyTimer = setTimeout(async () => {
      await ensureBomAll();
      await setBatchRatio(row.product_id, b, primaryBatchBlock(row.product_id), true);
      autoFillUsage(row.product_id);   // 나머지 블록(포장 부재료 등)도 빠짐없이 채움
    }, 500);
  }
  const plan = Number(row.plan_qty) || 0, prod = Number(row.prod_qty) || 0;
  const good = prod - (Number(row.defect_qty) || 0);
  tr.cells[6].textContent = PCT(prod, plan);
  tr.cells[6].title = `달성률 = 생산 ${NF(prod)} ÷ 계획 ${NF(plan)}`;
  tr.cells[7].textContent = row.prod_qty ? NF(good) : "—";
  tr.cells[7].title = `양품 = 생산 ${NF(prod)} − 불량 ${NF(Number(row.defect_qty) || 0)}`;
  // 불량 사유 버튼: 불량 > 0 일 때만 표시 (제자리 토글 — 포커스 유지)
  const dfr = tr.querySelector(".dfr-btn");
  if (dfr) dfr.style.display = (Number(String(row.defect_qty).replace(/,/g, "")) > 0) ? "" : "none";
  if (tr.querySelector('[data-f="prod_qty"]') === document.activeElement) {
    updatePackUsage(row.product_id);   // 생산수량 → 개수 자재(포장재) 사용량 자동 재계산
    const psum = tr.querySelector(".psp-sum");   // 분배 요약의 '보유 N' 제자리 갱신
    if (psum) { const s = prodSplitSummary(row); psum.innerHTML = s; psum.style.display = s ? "" : "none"; }
  }
  renderUsageSoon();   // 제품별 자재 사용 그룹 헤더의 '생산 N개' 동기화
  renderNeed();        // 계획/생산 바뀌면 예상 자재 소요·부족 갱신
});
wireEntryTable("eShip", () => E.ship, renderShip, () => updateShipWarn());
wireEntryTable("eMatIn", () => E.matIn, () => { renderMatIn(); renderMat(); },
  () => renderMat());   // 입고량 타이핑 → 실사 카드의 입고/사용량 즉시 갱신 (다른 표라 포커스 유지)
wireEntryTable("eMat", () => E.mat, renderMat, (tr, row) => {
  const prev = row.prev_qty !== "" && row.prev_qty != null
    ? Number(row.prev_qty) : (E.prevStock[row.material_id] ?? 0);
  const used = prev + effIn(row) - (Number(row.real_qty) || 0);
  tr.cells[5].innerHTML = row.real_qty !== "" && row.material_id
    ? `<button class="uselink num" data-use="${row.material_id}">${NF(used)}</button>${lossHtml(row.material_id, used)}`
    : '<span class="auto">—</span>';
});
wireEntryTable("eStaff", () => E.staff, renderStaff, (tr, row) => {
  const named = (row.members || []).length, agency = (row.agency || []).length;
  tr.cells[2].textContent = NF(named + agency || (Number(row.headcount) || 0));   // 인원수 = 정직원 + 용역
  tr.cells[5].textContent = staffRate(row);   // 가동률 = 실가동 ÷ (목표가동 || 라인 정상가동)
});

// 저장 전 숫자값 검증 — 숫자로 해석 안 되는 값(미완성 수식 '45*' 등)이 있으면 저장 차단
function badNumIn(rows, fields, label) {
  for (const r of rows) for (const f of fields) {
    const v = r[f];
    if (v !== "" && v != null && isNaN(Number(String(v).replace(/,/g, ""))))
      return `${label}에 숫자가 아닌 값이 있습니다: "${v}"`;
  }
  return null;
}
// 음수 수량 차단 — 수량 필드에 음수가 있으면 저장 차단 (음수 출고 = 재고 조작이 되므로 서버도 400)
function negNumIn(rows, fields, label) {
  for (const r of rows) for (const f of fields) {
    const v = Number(String(r[f] ?? "").replace(/,/g, ""));
    if (!isNaN(v) && v < 0) return `${label}에 음수 수량이 있습니다: ${v}`;
  }
  return null;
}
// 저장 분리: 생산 탭과 재고·입고 탭은 각자 자기 섹션만 저장 — 담당자가 달라도 서로 안 덮어씀
// 동시 편집 충돌: 내가 연 이후 다른 사용자가 저장했으면 409 → 덮어쓸지 확인
// 일일 입력 body 섹션 → 필요한 담당 (서버 DUTY_SECTION과 동일)
const DUTY_SECTION = { production: "production", shipment: "shipment", usage: "usage",
  staffing: "staffing", materials: "stock", mat_in: "stock" };
const PROD_DUTIES = ["production", "shipment", "usage", "staffing"];   // '생산 입력' 탭이 저장하는 섹션들
let SAVING = false;   // 내 저장 중에는 '다른 사용자가 저장함' 알림을 띄우지 않는다
async function saveDayBody(body, label, force) {
  // 같은 날짜를 누가 보고 있으면 저장 전에 확인 — 저장하면 그 사람 화면에 갱신 알림이 뜬다
  if (!force && (E.viewers || []).length
      && !confirm(`⚠ ${E.viewers.join(", ")}님이 지금 ${E.date} 날짜를 보고 있습니다.\n\n`
        + `지금 저장하면 그분들 화면에 '업데이트됨' 알림이 뜹니다.\n`
        + `(그분이 아직 저장하지 않았다면 그분의 수정 중인 내용은 사라질 수 있습니다)\n\n저장할까요?`)) {
    return;
  }
  // 담당이 아닌 섹션은 아예 보내지 않는다 — 담당을 일부만 가진 계정도 자기 항목은 저장되게
  // (서버도 403으로 막지만, 여기서 걸러야 '출고만 담당'인 사람이 저장 자체를 못 하는 일이 없다)
  if (ROLE !== "admin") {
    const dropped = Object.keys(body)
      .filter(k => DUTY_SECTION[k] && !MYDUTY.has(DUTY_SECTION[k]));
    dropped.forEach(k => delete body[k]);
    if (dropped.length) {
      const names = [...new Set(dropped.map(k => DUTY_LABELS[DUTY_SECTION[k]]))].join(", ");
      toast(`담당이 아닌 항목은 제외하고 저장합니다 — ${names}`);
    }
  }
  body.base_version = E.version ?? null;
  if (force) body.force = true;
  SAVING = true;
  try {
    const r = await fetch("/api/day/" + E.date, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 409) {
      if (confirm("⚠ 다른 사용자가 이 날짜를 방금 저장했습니다.\n\n[확인] = 내 내용으로 덮어쓰기 (그 사람의 변경은 사라질 수 있음)\n[취소] = 저장하지 않고 최신 내용을 다시 불러오기")) {
        return await saveDayBody(body, label, true);
      }
      toast("저장을 취소하고 최신 내용을 불러왔습니다");
      await loadDay(E.date);
      return;
    }
    if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
    if (!r.ok) {
      let msg = await r.text();
      try { msg = JSON.parse(msg).detail || msg; } catch (e) { /* raw */ }
      toast(String(msg).slice(0, 180));
      throw new Error("save failed");
    }
    toast(`${E.date} ${label} 저장 완료 — 재고·현황에 반영됨`);
    entryCal.render();
    await loadDay(E.date);   // E.version 갱신 — 내 저장으로 내 알림이 뜨지 않게 SAVING 안에서
    // 저장 즉시 다른 화면(기준정보·분석)의 재고가 갱신되도록 캐시 무효화
    ANA.raw = null;
    for (const k of ["product", "raw", "sub"]) await reloadMaster(k);
    loadLowStock();   // 실사·발주 입력이 반영되도록 사이드바 알림 갱신
  } finally {
    SAVING = false;
  }
}
$("btnSaveDay").onclick = async () => {           // 생산 입력 탭
  if (!mustDate()) return;
  const bad = badNumIn(E.prod, ["batches", "plan_qty", "prod_qty", "defect_qty"], "생산실적")
    || badNumIn(E.ship, ["qty"], "완제품 출고")
    || badNumIn(E.usage, ["qty"], "자재 사용")
    || badNumIn(E.staff, ["agency_wage", "target_hours", "work_hours"], "인원·가동")
    || badNumIn(E.staff.flatMap(r => (r.members || []).concat(r.agency || [])), ["h"], "인원 투입시간")
    || badNumIn(E.staff.flatMap(r => r.agency || []), ["w"], "용역 시급")
    || negNumIn(E.staff.flatMap(r => r.agency || []), ["h", "w"], "용역 시간·시급")
    || negNumIn(E.prod, ["batches", "plan_qty", "prod_qty", "defect_qty"], "생산실적")
    || negNumIn(E.ship, ["qty"], "완제품 출고")
    || negNumIn(E.usage, ["qty"], "자재 사용");
  if (bad) return toast("⚠ " + bad + " — 고친 뒤 저장하세요");
  // 불량 > 생산 차단 (양품이 음수가 됨)
  for (const r of E.prod) {
    if (!r.product_id) continue;
    const prod = Number(String(r.prod_qty).replace(/,/g, "")) || 0;
    const defect = Number(String(r.defect_qty).replace(/,/g, "")) || 0;
    if (defect - prod > 0.5) {
      const p = productById(r.product_id);
      return toast(`⚠ '${p ? p.name : "?"}' 불량 ${NF(defect)}개가 생산 ${NF(prod)}개보다 많습니다`);
    }
  }
  // 출고 재고 초과 차단
  const ssum = shipSumByProduct();
  for (const pid in ssum) {
    const avail = shipAvail(+pid);
    if (ssum[pid] - avail > 0.5) {
      const p = productById(+pid);
      return toast(`⚠ '${p ? p.name : pid}' 출고량 ${NF(ssum[pid])}개가 가용 재고 ${NF(avail)}개를 초과합니다`);
    }
  }
  const body = {
    memo: $("eMemo").value,
    production: E.prod.filter(r => r.product_id).map(r => ({ ...r,
      lot_splits: r.lotSplits || [], prod_splits: r.prodSplits || [] })),
    shipment: E.ship.filter(r => r.product_id && r.qty).map(r => ({ ...r, expiry: r.lotExpiry || "", lot_no: r.lotNo || 0 })),
    staffing: E.staff.filter(r => r.line_id).map(r => ({ ...r,
      headcount: (r.members || []).length,
      // 용역 = 개인별 [시간, 시급, 성별, 업체] — 서버가 상세 저장 + 집계(가중평균 시급) 계산
      agency: (r.agency || []).map(a => ({
        h: Number(String(a.h ?? "").replace(/,/g, "")) || 0,
        w: Number(String(a.w ?? "").replace(/,/g, "")) || 0,
        g: a.g || "", pid: a.pid || null })),
      agency_count: (r.agency || []).length,
      agency_hours: (r.agency || []).reduce((s, a) => s + (Number(a.h) || 0), 0),
      agency_wage: r.agency_wage || 0 })),
    usage: E.usage.filter(u => u.material_id && u.qty),   // 제품 없는 기타 사용도 저장
  };
  showSaveSum(body, "생산 입력");   // 저장 전 요약 확인 → 확인 시 저장
};
$("btnSaveStock").onclick = async () => {         // 재고 · 입고 탭
  if (!mustDate()) return;
  const bad = badNumIn(E.mat, ["prev_qty", "in_qty", "real_qty", "order_qty"], "재고 실사")
    || badNumIn(E.matIn, ["qty"], "원부자재 입고")
    || negNumIn(E.matIn, ["qty"], "원부자재 입고");   // 실사(E.mat)는 정정 신호일 수 있어 음수 허용
  if (bad) return toast("⚠ " + bad + " — 고친 뒤 저장하세요");
  const body = {
    // 실재고가 빈 실사 행은 제외 — 0으로 저장돼 재고가 통째로 사용 처리되는 사고 방지
    materials: E.mat.filter(r => r.material_id
        && ((r.real_qty !== "" && r.real_qty != null) || r.order_date || r.order_qty))
      .map(r => ({
        ...r, prev_qty: r.prev_qty !== "" && r.prev_qty != null ? r.prev_qty : (E.prevStock[r.material_id] ?? 0) })),
    mat_in: E.matIn.filter(r => r.material_id && r.qty),
  };
  const skipped = E.mat.filter(r => r.material_id).length - body.materials.length;
  if (skipped > 0) toast(`실재고가 빈 실사 행 ${skipped}건은 저장에서 제외됩니다`);
  await saveDayBody(body, "재고·입고");
};

/* 정지사유 모달 */
let stopIdx = -1;
function openStop(i) { stopIdx = i; $("stopText").value = E.staff[i].stop_reason || ""; $("stopOverlay").classList.add("on"); $("stopText").focus(); }
window.closeStop = () => $("stopOverlay").classList.remove("on");
window.saveStop = () => { if (stopIdx >= 0) { E.staff[stopIdx].stop_reason = $("stopText").value.trim(); renderStaff(); } closeStop(); };

/** 오늘 생산에 **쓰기 전** 가용 재고 = 전일재고 + 오늘 입고.
    자재의 '현재고'(최근 실재고)는 오늘 사용분이 이미 차감된 값이라 소요 비교에 쓰면 이중 차감된다. */
function availBeforeToday(mid) {
  const row = (E.mat || []).find(r => r.material_id === mid);          // 실사 행
  if (row) {
    const prev = row.prev_qty !== "" && row.prev_qty != null
      ? Number(row.prev_qty) : (E.prevStock[mid] ?? 0);
    return (Number(prev) || 0) + effIn(row);
  }
  // 자동 반영 행 — 입고는 effIn으로 (입고 카드에 방금 적은 분도 즉시 반영되게)
  const auto = (E.autoMat || []).find(r => r.material_id === mid);
  if (auto) return (Number(auto.prev_qty) || 0) + effIn(auto);
  return (Number(E.prevStock[mid]) || 0) + matInTotal(mid);            // 오늘 기록이 아직 없는 자재
}
/* ══ 예상 자재 소요·부족 (생산 계획 × 배합비 vs 오늘 쓰기 전 재고) ══ */
function renderNeed() {
  const box = $("eNeed"); if (!box) return;
  if (!BOMALL) { box.innerHTML = '<div class="auto">배합비 로딩 중…</div>'; return; }
  const need = {};   // material_id → 필요량 (자재 단위 기준)
  let anyQty = false;
  E.prod.forEach(r => {
    const pid = r.product_id; if (!pid) return;
    const q = Number(String(r.plan_qty).replace(/,/g, "")) || Number(String(r.prod_qty).replace(/,/g, "")) || 0;
    if (q <= 0) return;
    anyQty = true;
    (BOMALL[pid] || []).forEach(b => {
      const m = materialById(b.material_id); if (!m) return;
      let amt;
      if (isCountMat(m)) {
        amt = m.pack_count > 0 ? q / m.pack_count : 0;   // 개수 자재: 생산수량 ÷ 개입수
      } else {
        amt = (Number(b.qty_per_unit) || 0) * q;         // 1개당(b.unit) × 생산수량
        const bu = (b.unit || "g").toLowerCase(), mu = (m.unit || "").toLowerCase();
        if (bu !== mu) {
          if (bu === "g" && mu === "kg") amt /= 1000;
          else if (bu === "kg" && mu === "g") amt *= 1000;
        }
      }
      if (amt > 0) need[b.material_id] = (need[b.material_id] || 0) + amt;
    });
  });
  const list = Object.entries(need).map(([mid, q]) => {
    const m = materialById(+mid) || {};
    // 비교 기준 = **오늘 생산에 쓰기 전** 가용 재고 (전일재고 + 오늘 입고).
    // m.stock(최근 실재고)은 오늘 사용분이 이미 빠진 값이라, 그걸 쓰면 오늘 사용량을 두 번 빼게 된다.
    const stock = availBeforeToday(+mid);
    return { name: m.name || mid, unit: m.unit || "", need: q, stock, short: q - stock };
  });
  if (!list.length) {
    box.innerHTML = anyQty
      ? '<div class="auto">이 제품들의 배합비가 없어 소요를 계산할 수 없습니다 (기준정보 › 배합비에서 등록)</div>'
      : '<div class="auto">생산 계획(또는 생산수량)을 입력하면 필요한 자재량과 부족분이 표시됩니다</div>';
    $("needSub").textContent = "생산 계획(또는 생산수량) × 배합비 기준";
    return;
  }
  // 부족한 자재만 보여준다 — 필요량 열은 제거(계획/배합 기준 차이로 오해를 부름).
  // 필요량은 부족 판정에만 쓰고, 궁금하면 툴팁으로 확인.
  const shortList = list.filter(x => x.short > 0.0005).sort((a, b) => b.short - a.short);
  const okN = list.length - shortList.length;
  // 재고가 음수인 자재는 '부족'이 아니라 재고 정리가 안 된 것 — 따로 표시해야 발주량을 오판하지 않는다
  const negN = shortList.filter(x => x.stock < 0).length;
  const base = "계획수량 × 배합비 vs 전일재고 + 오늘 입고";
  $("needSub").textContent = shortList.length
    ? `⚠ 부족 ${shortList.length}종 — 발주 검토` + (negN ? ` · 재고 미정리 ${negN}종` : "") + ` · ${base}`
    : `가용 재고로 충분 (${list.length}종) · ${base}`;
  if (!shortList.length) {
    box.innerHTML = `<div class="auto" style="padding:10px 2px">✅ 이 생산 계획에 필요한 자재 ${list.length}종 모두 가용 재고로 충분합니다</div>`;
    return;
  }
  box.innerHTML = `<div class="need-head"><span>부족 자재</span><span>가용 재고</span><span>부족 수량</span></div>` +
    shortList.map(x => {
      const u = esc(x.unit);
      const neg = x.stock < 0;
      return `<div class="need-row short">
        <span class="nn">⚠️ ${esc(x.name)}</span>
        <span class="nq num" ${neg ? 'style="color:var(--crit)"' : ""}
          title="${neg ? "재고가 음수입니다 — 실사가 없어 사용량만 쌓인 상태입니다. 재고·입고에서 실재고를 입력하세요"
            : "오늘 쓰기 전 재고 = 전일재고 + 오늘 입고"}">${NF(x.stock)} ${u}${neg ? " ⚠" : ""}</span>
        <span class="nshort num" title="필요 ${NF(x.need)} ${u} − 가용 ${NF(x.stock)} ${u}">${NF(x.short)} ${u}</span></div>`;
    }).join("")
    + (okN ? `<div class="auto" style="padding:8px 8px 2px; font-size:11.5px">그 외 ${okN}종은 가용 재고로 충분합니다</div>` : "")
    + `<div class="auto" style="padding:2px 8px; font-size:11.5px">가용 재고 = 전일재고 + 오늘 입고 (오늘 사용분은 빼지 않은 값)</div>`
    + (negN ? `<div class="auto" style="padding:2px 8px; font-size:11.5px; color:var(--crit)">※ 가용 재고가 음수인 ${negN}종은 실사 기록이 없어 사용량만 쌓인 것입니다 — 재고·입고에서 실재고를 입력하세요</div>` : "");
}

/* ══ 어제처럼 — 직전 생산일 구성 불러오기 (생산/인원 각각) ══ */
$("btnCopyPrevProd").onclick = async () => {
  if (!mustDate()) return;
  if (!E.prevProdDate) return toast("불러올 이전 생산 기록이 없습니다");
  const hasNow = E.prod.some(r => r.product_id);
  if (hasNow && !confirm(`${E.prevProdDate} 생산 구성으로 현재 생산실적을 교체할까요?\n(제품·라인·배합·계획만 불러오고 생산수량은 비웁니다 · 인원은 그대로)`)) return;
  const d = await api("/api/day/" + E.prevProdDate);
  E.prod = d.production.map(r => ({ product_id: r.product_id, line_id: r.line_id, batches: r.batches || "",
    plan_qty: r.plan_qty || "", prod_qty: "", defect_qty: "", defect_reason: "", lotSplits: [],
    prodSplits: [],   // 분배는 실제 생산 기준 — 어제 것 복사 안 함 (생산수량도 비우므로)
    expiry: "" }));
  renderProd(); renderUsage(); renderNeed();
  toast(`${E.prevProdDate} 생산 구성을 불러왔습니다 — 생산수량을 입력하세요`);
};
$("btnCopyPrevStaff").onclick = async () => {
  if (!mustDate()) return;
  if (!E.prevProdDate) return toast("불러올 이전 인원 기록이 없습니다");
  const hasNow = E.staff.some(r => r.line_id);
  if (hasNow && !confirm(`${E.prevProdDate} 인원·가동 구성으로 현재 인원을 교체할까요?\n(생산실적은 그대로 둡니다)`)) return;
  const d = await api("/api/day/" + E.prevProdDate);
  if (!d.staffing.length) return toast(`${E.prevProdDate}에는 인원 기록이 없습니다`);
  E.staff = d.staffing.map(r => ({ ...mapStaffRow(r), stop_reason: "" }));
  renderStaff();
  toast(`${E.prevProdDate} 인원 구성을 불러왔습니다`);
};

/* ══ 불량 사유 모달 ══ */
const DEFECT_PRESETS = ["태움(과열)", "설익음", "모양 불량", "크기 미달", "이물질", "포장 불량", "터짐/갈라짐"];
let dreasonIdx = -1;
function openDreason(i) {
  const r = E.prod[i]; if (!r) return;
  dreasonIdx = i;
  const p = productById(r.product_id);
  const dq = Number(String(r.defect_qty).replace(/,/g, "")) || 0;
  $("dreasonHint").textContent = `${p ? p.name : "제품"} · 불량 ${NF(dq)}개 — 사유를 선택하거나 직접 적어주세요.`;
  $("dreasonText").value = r.defect_reason || "";
  $("dreasonChips").innerHTML = DEFECT_PRESETS.map(t => `<button class="dchip" data-dp="${esc(t)}">${esc(t)}</button>`).join("");
  $("dreasonOverlay").classList.add("on");
  setTimeout(() => $("dreasonText").focus(), 30);
}
$("dreasonChips").addEventListener("click", e => {
  const b = e.target.closest("[data-dp]"); if (!b) return;
  const ta = $("dreasonText");
  ta.value = ta.value.trim() ? ta.value.trim() + ", " + b.dataset.dp : b.dataset.dp;
  ta.focus();
});
window.closeDreason = () => $("dreasonOverlay").classList.remove("on");
window.saveDreason = () => {
  if (dreasonIdx >= 0 && E.prod[dreasonIdx]) E.prod[dreasonIdx].defect_reason = $("dreasonText").value.trim();
  closeDreason();
  renderProd();
};

/* ══ 생산 현장 사진 ══ */
function renderPhotos() {
  const box = $("ePhotos"); if (!box) return;
  const canEdit = ROLE !== "guest";
  $("addPhotoBtn").style.display = canEdit ? "" : "none";
  box.innerHTML = (E.photos && E.photos.length)
    ? E.photos.map(p => `<div class="ph-item">
        <img src="/dayphoto/${encodeURIComponent(p.file)}" alt="생산 사진">
        ${canEdit ? `<button class="ph-del" data-delphoto="${p.id}" title="삭제">✕</button>` : ""}</div>`).join("")
    : '<div class="ph-empty">첨부된 사진이 없습니다.</div>';
}
$("ePhotos").addEventListener("click", async e => {
  const b = e.target.closest("[data-delphoto]"); if (!b) return;
  if (!confirm("이 사진을 삭제할까요?")) return;
  try { await api("/api/day/photo/" + b.dataset.delphoto, { method: "DELETE" }); } catch (err) { return; }
  E.photos = E.photos.filter(p => p.id !== +b.dataset.delphoto);
  renderPhotos();
  toast("사진이 삭제되었습니다");
});
$("addPhotoBtn").onclick = () => { if (!mustDate()) return; $("photoFile").click(); };
$("photoFile").addEventListener("change", async e => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  if (f.size > 8 * 1024 * 1024) return toast("이미지는 8MB 이하만 가능합니다");
  const dataUrl = await new Promise((res, rej) => {
    const fr = new FileReader(); fr.onload = () => res(fr.result); fr.onerror = rej; fr.readAsDataURL(f);
  });
  try {
    const r = await api(`/api/day/${E.date}/photo`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ data: dataUrl }) });
    E.photos.push({ id: r.id, file: r.file, note: "" });
    renderPhotos();
    toast("사진이 첨부되었습니다");
  } catch (err) { /* api()가 토스트 표시 */ }
});

/* ══ 저장 전 요약 확인 ══ */
function showSaveSum(body, label) {
  const prod = body.production || [], ship = body.shipment || [];
  const usage = body.usage || [], staff = body.staffing || [];
  const prodQty = prod.reduce((s, r) => s + (Number(r.prod_qty) || 0), 0);
  const shipQty = ship.reduce((s, r) => s + (Number(r.qty) || 0), 0);
  const defNoReason = prod.filter(r => (Number(r.defect_qty) || 0) > 0 && !(r.defect_reason || "").trim()).length;
  const memo = (body.memo || "").trim();
  const rowsHtml = [
    ["🏭 생산실적", `${prod.length}종 · ${NF(prodQty)}개`],
    ["🚚 완제품 출고", `${ship.length}건 · ${NF(shipQty)}개`],
    ["🧾 자재 사용", `${usage.length}건`],
    ["👷 인원·가동", `${staff.length}라인`],
    ["📝 특이사항", memo ? "입력됨" : "없음"],
  ];
  let html = rowsHtml.map(([k, v]) => `<div class="ssum-row"><span>${k}</span><span class="sv">${v}</span></div>`).join("");
  if (defNoReason) html += `<div class="ssum-row warn"><span>⚠️ 불량 사유 미입력</span><span class="sv">${defNoReason}건</span></div>`;
  $("saveSumHint").textContent = `${E.date} (${dowOf(E.date)}) ${label}을 저장합니다.`;
  $("saveSumBody").innerHTML = html;
  $("saveSumConfirm").onclick = async () => { closeSaveSum(); await saveDayBody(body, label); };
  $("saveSumOverlay").classList.add("on");
}
window.closeSaveSum = () => $("saveSumOverlay").classList.remove("on");

/* 사용처 분석 모달 */
async function openUse(materialId, date) {
  const d = await api(`/api/usage?material_id=${materialId}&date=${date}`);
  $("useTitle").textContent = `${d.material} — 사용처 분석 (${d.shown_date})`;
  $("useHint").textContent = d.shown_date !== d.date
    ? `${date}에는 사용처 데이터가 없어 가장 가까운 ${d.shown_date} 데이터를 표시합니다.`
    : "원료수불부 실측 데이터 기준 제품별 사용량입니다.";
  const total = d.rows.reduce((s, r) => s + r.qty, 0);
  $("useBody").innerHTML = (d.rows.map(r => `<tr><td><b>${esc(r.name)}</b></td>
      <td class="r">${r.prod_qty != null ? NF(r.prod_qty) + "개" : "—"}</td>
      <td class="r">${NF(r.qty)} ${esc(d.unit)}</td>
      <td class="r">${PCT(r.qty, total)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="auto">이 자재의 제품별 사용 기록이 없습니다</td></tr>`)
    + (d.rows.length ? `<tr style="font-weight:700"><td>합계</td><td></td><td class="r">${NF(total)} ${esc(d.unit)}</td><td class="r">100%</td></tr>` : "")
    + (d.actual_used != null && d.rows.length ? `<tr style="font-weight:700"><td>수불부 사용량</td><td></td>
        <td class="r">${NF(d.actual_used)} ${esc(d.unit)}</td>
        <td class="r" style="color:${Math.abs(d.actual_used - total) > total * 0.05 ? "var(--crit)" : "var(--muted)"}">
        차이 ${NF(d.actual_used - total)}</td></tr>` : "")
    + ((d.types || []).length ? `<tr><td colspan="4" style="background:var(--bg); font-size:11.5px; font-weight:800; color:var(--muted);">용도별 배합 사용량 (원재료 수불부 기록)</td></tr>`
        + d.types.map(t => `<tr><td class="auto">${esc(t.type)} 배합</td><td></td>
          <td class="r">${NF(t.qty)} ${esc(d.unit)}</td><td></td></tr>`).join("") : "");
  $("useOverlay").classList.add("on");
}
window.closeUse = () => $("useOverlay").classList.remove("on");

/* ══ 기준정보 관리 ═════════════════════ */
const MCOLS = {
  product: { label: "제품", cols: ["제품명", "카테고리", "규격", "단가(원)", "소비일", "안전재고", "현재고", "상태"],
    row: r => [`<button class="uselink" data-phist="${r.id}" style="display:inline-flex; align-items:center; gap:8px">${r.image ? `<img src="/image/${encodeURIComponent(r.image)}" style="width:30px; height:30px; object-fit:cover; border-radius:5px; border:1px solid var(--line)">` : ""}<b>${esc(r.name)}</b></button>`, esc(r.category || "—"), esc(r.spec || "—"), r.unit_price == null ? "—" : NF(r.unit_price), r.shelf_days || "—", NF(r.safety_stock), NF(r.stock), chip(r.status)],
    hint: "제품명 클릭 = 생산일자별 재고(LOT)·생산/출고 이력 · 현재고 = 기초재고 + 생산 − 출고 − 폐기 (자동 계산)" },
  raw: { label: "원재료", cols: ["자재명", "규격", "단위", "개입수", "단가(원)", "안전재고", "현재고", "재고일수", "최종 기록일", "상태"],
    row: r => [`<button class="uselink" data-mhist="${r.id}">${esc(r.name)}</button>`, esc(r.spec || "—"), esc(r.unit), r.pack_count ? NF(r.pack_count) : "—", r.unit_price == null ? "—" : NF(r.unit_price), NF(r.safety_stock), stockCell(r), stockDaysCell(r), esc(r.stock_date || "—"), chip(r.status)],
    hint: "자재명 클릭 = 입·출고 이력 · 현재고 = 마지막 기록일의 실재고 · 재고일수 = 현재고 ÷ 최근 30일 일평균 사용량 · 개입수 = 개수 자재의 1개당 포장수(소모 = 생산수량 ÷ 개입수)" },
  sub: { label: "부재료", cols: ["자재명", "단위", "개입수", "단가(원)", "안전재고", "현재고", "재고일수", "생산가능수량", "생산가능횟수", "최종 기록일", "상태"],
    row: r => {
      const canQty = r.prod_mult && r.stock != null ? r.stock * r.prod_mult : null;
      const canCnt = canQty != null && r.prod_per ? canQty / r.prod_per : null;
      return [`<button class="uselink" data-mhist="${r.id}">${esc(r.name)}</button>`, esc(r.unit), r.pack_count ? NF(r.pack_count) : "—", NF(r.unit_price), NF(r.safety_stock), stockCell(r), stockDaysCell(r),
        canQty != null ? NF(Math.round(canQty)) : '<span class="auto">환산 미설정</span>',
        canCnt != null ? `<span style="${canCnt < 3 ? "color:var(--warn); font-weight:700" : ""}">${NF(Math.round(canCnt * 10) / 10)}회</span>` : "—",
        esc(r.stock_date || "—"), chip(r.status)];
    },
    hint: "재고일수 = 현재고 ÷ 최근 30일 일평균 사용량 · 생산가능수량 = 현재고 × 단위당 수량 · 횟수 = 수량 ÷ 1회 소요량 (환산계수는 7/7 실사 엑셀에서 갱신됨)" },
  partner: { label: "거래처", cols: ["거래처명", "유형", "연락처", "담당자", "비고", "상태"],
    row: r => [B(r.name), `<span class="chip cat">${esc(r.type)}</span>`, esc(r.phone || "—"), esc(r.contact || "—"), esc(r.note || "—"), chip(r.status)],
    hint: "중지 상태 거래처는 일일 입력 드롭다운에서 숨겨집니다" },
  staff: { label: "인원", cols: ["이름", "구분", "직책", "담당 공정", "시급(원)", "입사일", "상태"],
    row: r => [B(r.name), esc(r.kind), esc(r.position || "—"), esc(r.process || "—"), r.wage == null ? "—" : NF(r.wage), esc(r.join_date || "—"), chip(r.status)],
    hint: "일일 입력의 투입 인원 선택 목록 · 노무비 계산 기준" },
  line: { label: "생산라인", cols: ["라인명", "공정", "정상가동(h/일)", "비고", "상태"],
    row: r => {
      // 소속 라인(parent_id) 기준 그룹 표시 — 대표 라인엔 공정 수, 소속 공정은 ↳ (이름 오타와 무관)
      const kids = (M.line || []).filter(l => +l.parent_id === r.id);
      const parent = r.parent_id ? (M.line || []).find(l => l.id === +r.parent_id) : null;
      const addBtn = ` <button class="btn ghost sm" data-addproc="${r.id}" style="font-size:11px; padding:1px 8px;"
        title="이 라인 아래에 공정을 추가합니다 (예: 배합 · 성형 · 포장)">＋ 공정</button>`;
      const nameCell = parent
        ? `<span class="auto" style="padding-left:14px" title="소속: ${esc(parent.name)}">↳ ${esc(r.name)}</span>`
        : (kids.length
          ? `${B(r.name)} <span class="chip cat" title="이 라인에 소속된 공정 ${kids.length}개 — 가동률·보고서는 한 라인으로 집계됩니다">공정 ${kids.length + 1}개 · 한 라인</span>`
          : B(r.name)) + addBtn;
      return [nameCell, esc(r.process || "—"), NF(r.std_hours), esc(r.note || "—"), chip(r.status)];
    },
    hint: "공정 행은 수정 팝업의 '소속 라인'으로 대표 라인에 연결 — 연결된 공정들은 가동률·보고서에서 한 물리 라인으로 집계됩니다" },
};
function B(s) { return `<b>${esc(s)}</b>`; }
function chip(s) {
  const bad = ["단종", "중지", "중단", "퇴사"].includes(s);
  return `<span class="chip ${bad ? "warn" : "ok"}">${esc(s || "—")}</span>`;
}
function stockCell(r) {
  const low = (r.safety_stock > 0 && r.stock < r.safety_stock) || (r.stock != null && r.stock <= 0);
  return `<span class="num" style="${low ? "color:var(--crit); font-weight:700" : ""}">${NF(r.stock)}</span>`;
}
function stockDaysCell(r) {   // 재고일수 = 현재고 ÷ 최근 30일 일평균 사용량 (사용 기록 없으면 —)
  if (!(r.avg_use > 0) || r.stock == null) return '<span class="auto">—</span>';
  const d = r.stock / r.avg_use;
  const disp = d >= 10 ? Math.round(d) : Math.round(d * 10) / 10;
  return `<span class="num" style="${d < 7 ? "color:var(--warn); font-weight:700" : ""}"
    title="일평균 사용 ${NF(Math.round(r.avg_use * 100) / 100)} ${esc(r.unit || "")} 기준">${NF(disp)}일</span>`;
}
let mTab = "product";
$("itemTabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-mt]"); if (!b) return;
  document.querySelectorAll("#itemTabs button").forEach(x => x.classList.toggle("on", x === b));
  mTab = b.dataset.mt;
  mMissing = "";   // 미등록 필터는 탭별 항목이 달라 전환 시 해제
  // 검색어도 해제 — 탭마다 항목이 달라, 남아 있으면 '등록된 항목이 없습니다'만 보인다
  mFilter = "";
  $("mFilter").value = "";
  BOM.q = ""; $("bomProdSearch").value = "";
  renderMasters();
});
let mFilter = "";
$("mFilter").addEventListener("input", e => { mFilter = e.target.value.trim().toLowerCase(); renderMasters(); });

/* ── 완성도 점검 + 빠른 편집 설정 ──
   HEALTH: 탭별 미등록 점검 항목 (미등록이면 관련 기능이 잠자는 필드들)
   QE_COLS: 빠른 편집 대상 — cfg.cols의 컬럼 index → 필드명 */
const MHEALTH = {
  product: [
    ["shelf_days", "소비일", "미등록 시 LOT 소비기한이 전부 '기한미상'"],
    ["safety_stock", "안전재고", "미등록 시 대시보드 완제품 부족 경보 없음"],
    ["unit_price", "단가", "미등록 시 생산·출고 금액 집계에서 빠짐"],
    ["bom", "배합비", "미등록 시 자재 자동차감·부족예측 안 됨"],
    ["image", "이미지", "목록·랭킹에 사진 대신 🍞 표시"],
  ],
  raw: [
    ["unit_price", "단가", "미등록 시 자재 사용금액 집계에서 빠짐"],
    ["safety_stock", "안전재고", "미등록 시 재고 0 이하가 돼야만 부족 경보"],
  ],
  sub: [
    ["unit_price", "단가", "미등록 시 자재 사용금액 집계에서 빠짐"],
    ["safety_stock", "안전재고", "미등록 시 재고 0 이하가 돼야만 부족 경보"],
  ],
  staff: [["wage", "시급", "미등록 시 노무비 계산이 0으로 나옴"]],
};
const QE_COLS = {
  product: { 3: "unit_price", 4: "shelf_days", 5: "safety_stock" },
  raw: { 4: "unit_price", 5: "safety_stock" },
  sub: { 3: "unit_price", 4: "safety_stock" },
  staff: { 3: "wage" },
};
let mQuick = false;      // 빠른 편집 모드
let mMissing = "";       // 완성도 배너에서 선택한 '미등록만 보기' 필드
const ACTIVE_OK = { product: "단종", raw: "중단", sub: "중단", staff: "퇴사" };   // 제외할 상태
function mActive(r) { return r.status !== ACTIVE_OK[mTab]; }
function mIsMissing(r, f) {
  if (f === "bom") return !(BOMALL && (BOMALL[r.id] || []).length);
  if (f === "image") return !r.image;
  return !(Number(r[f]) > 0);   // null(미입력)·0 모두 미등록 (권한 마스킹은 renderMHealth에서 ROLE로 제외)
}
function renderMHealth() {
  const checks = MHEALTH[mTab];
  const bar = $("mHealthBar");
  if (!checks) { bar.style.display = "none"; mMissing = ""; return; }
  if (checks.some(([f]) => f === "bom") && !BOMALL) {   // 배합비 점검엔 BOM 캐시 필요
    ensureBomAll().then(() => { if (mTab === "product") renderMHealth(); });
  }
  const act = (M[mTab] || []).filter(mActive);
  const items = checks.map(([f, label, why]) => {
    if (f === "bom" && !BOMALL) return null;                       // 로딩 중엔 생략
    if (f === "unit_price" && !canM(mTab === "product" ? "prod" : "mat")) return null;   // 권한 마스킹 필드는 열람 권한자만 점검
    if (f === "wage" && !canM("labor")) return null;
    const n = act.filter(r => mIsMissing(r, f)).length;
    return n > 0 ? { f, label, why, n } : null;
  }).filter(Boolean);
  bar.style.display = "";
  if (!items.length) {
    bar.innerHTML = `<div class="mh-wrap allok">✅ <b>기준정보 완성!</b> 이 탭의 점검 항목(${checks.map(c => c[1]).join("·")})이 모두 등록되어 있습니다.</div>`;
    mMissing = "";
    return;
  }
  bar.innerHTML = `<div class="mh-wrap">📋 <b>미등록 점검</b>
    ${items.map(i => `<button class="mh-chip ${mMissing === i.f ? "on" : ""}" data-mh="${i.f}" title="${esc(i.why)} — 클릭하면 미등록만 표시">${i.label} ${i.n}건</button>`).join("")}
    ${mMissing ? '<span class="auto" style="font-size:11.5px">미등록만 표시 중 — 칩을 다시 클릭하면 해제</span>' : '<span class="auto" style="font-size:11.5px">칩 클릭 = 미등록만 보기 · ⚡ 빠른 편집으로 표에서 바로 입력</span>'}</div>`;
}
$("mHealthBar").addEventListener("click", e => {
  const b = e.target.closest("[data-mh]"); if (!b) return;
  mMissing = mMissing === b.dataset.mh ? "" : b.dataset.mh;
  renderMasters();
});

function masterList() {   // 현재 탭의 표시 목록 (검색 + 미등록 필터 적용)
  const full = M[mTab] || [];
  let list = mFilter ? full.filter(r => String(r.name || "").toLowerCase().includes(mFilter)) : full;
  if (mMissing) list = list.filter(r => mActive(r) && mIsMissing(r, mMissing));
  if (mTab === "line") {   // 소속 공정이 대표 라인 바로 아래 오도록 정렬
    const sorted = [];
    list.filter(l => !l.parent_id).forEach(p => {
      sorted.push(p);
      list.filter(l => +l.parent_id === p.id).forEach(c => sorted.push(c));
    });
    list.forEach(l => { if (!sorted.includes(l)) sorted.push(l); });   // 부모가 필터에 걸러진 공정
    list = sorted;
  }
  return { full, list };
}
function renderMasters() {
  $("bomBar").style.display = mTab === "bom" ? "flex" : "none";
  $("bomAddSearch").style.display = mTab === "bom" ? "" : "none";
  if (mTab !== "bom") $("bomBlocks").style.display = "none";
  $("mFilterBar").style.display = (mTab === "bom" || mTab === "users" || mTab === "audit") ? "none" : "flex";
  if (mTab === "bom" || mTab === "users" || mTab === "audit") $("mHealthBar").style.display = "none";
  $("mAdd").style.display = mTab === "audit" ? "none" : "";
  updateTabCounts();
  if (mTab === "bom") { renderBomTab(); return; }
  if (mTab === "users") { renderUsersTab(); return; }
  if (mTab === "audit") { renderAuditTab(); return; }
  const cfg = MCOLS[mTab];
  renderMHealth();
  // 빠른 편집 버튼: 대상 탭 + 쓰기 권한일 때만
  const qeMap = QE_COLS[mTab];
  $("mQuickEdit").style.display = (qeMap && ROLE !== "guest") ? "" : "none";
  $("mImport").style.display = (qeMap && ROLE === "admin") ? "" : "none";
  $("mPackSet").style.display = (mTab === "sub" && ROLE === "admin") ? "" : "none";
  $("mQuickEdit").classList.toggle("on", mQuick);
  $("mQuickEdit").textContent = mQuick ? "⚡ 빠른 편집 종료" : "⚡ 빠른 편집";
  const { full, list } = masterList();
  // 검색창 클릭 시 이 탭의 등록 항목 전체가 드롭다운으로 — 입력하면 자동 필터
  $("mFilterList").innerHTML = full.map(r => `<option value="${esc(r.name)}">`).join("");
  $("mFilterCnt").textContent = (mFilter || mMissing) ? `${list.length}건 / 전체 ${full.length}건` : "";
  $("mHead").innerHTML = "<tr>" + cfg.cols.map(c => `<th>${c}</th>`).join("") + "<th></th></tr>";
  $("mBody").innerHTML = list.map(r => {
    const cells = cfg.row(r);
    if (mQuick && qeMap) {
      for (const [idx, f] of Object.entries(qeMap)) {
        if (r[f] === null && !(ROLE === "admin"
          || (f === "unit_price" && canM(mTab === "product" ? "prod" : "mat"))
          || (f === "wage" && canM("labor")))) continue;   // 권한 마스킹 필드는 열람 권한 없으면 입력 불가 — 권한자의 null은 '미입력'
        cells[idx] = `<input class="qe-input" data-qe="${f}" data-qid="${r.id}" inputmode="decimal"
          value="${Number(r[f]) > 0 ? r[f] : ""}" placeholder="${cfg.cols[idx]}">`;
      }
    }
    return `<tr>${cells.map(c => `<td>${c}</td>`).join("")}
     <td style="white-space:nowrap"><button class="btn ghost sm" data-edit="${r.id}">수정</button><button class="btn ghost sm" style="color:var(--crit)" data-delm="${r.id}">삭제</button></td></tr>`;
  }).join("")
    || `<tr><td colspan="${cfg.cols.length + 1}" class="auto">${mMissing ? "미등록 항목이 없습니다 👍" : "등록된 항목이 없습니다"}</td></tr>`;
  $("mHint").textContent = mQuick
    ? "⚡ 빠른 편집: 노란 칸에 값을 입력하면 즉시 저장됩니다 (Tab/Enter로 다음 칸 이동)"
    : cfg.hint;
  $("mAdd").textContent = "+ 새 " + cfg.label;
}
$("mQuickEdit").onclick = () => { mQuick = !mQuick; renderMasters(); };
// 빠른 편집 저장 — 입력 확정(change) 시 해당 필드만 PUT, 표 재렌더 없이 제자리 반영
$("mBody").addEventListener("change", async e => {
  const inp = e.target.closest(".qe-input"); if (!inp) return;
  const f = inp.dataset.qe, id = +inp.dataset.qid;
  const v = Number(String(inp.value).replace(/,/g, ""));
  if (inp.value !== "" && (isNaN(v) || v < 0)) { toast("숫자를 입력하세요"); inp.value = ""; return; }
  try {
    await api(`/api/masters/${mTab}/${id}`, { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [f]: v || 0 }) });
    const row = (M[mTab] || []).find(r => r.id === id);
    if (row) row[f] = v || 0;
    inp.classList.add("saved");
    setTimeout(() => inp.classList.remove("saved"), 1200);
    renderMHealth();   // 배너 건수만 갱신 (표는 그대로 — 포커스 유지)
  } catch (err) { /* api()가 오류 토스트 표시 */ }
});
/* CSV 일괄 가져오기 — 이름으로 매칭해 단가/소비일/안전재고/시급 갱신 (admin) */
$("mImport").onclick = () => $("mImportFile").click();
$("mImportFile").addEventListener("change", async e => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  const buf = await f.arrayBuffer();
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); }
  catch (err) { text = new TextDecoder("euc-kr").decode(buf); }   // 한국 엑셀 CSV(ANSI=CP949)
  importCsvApply(text.replace(/^﻿/, ""));
});
const IMPORT_COLS = { "단가": "unit_price", "소비일": "shelf_days", "안전재고": "safety_stock",
  "시급": "wage", "개입수": "pack_count" };
async function importCsvApply(text) {
  const lines = text.split(/\r?\n/).filter(l => l.trim());
  if (lines.length < 2) return toast("데이터가 없습니다 — 첫 줄은 헤더, 둘째 줄부터 데이터여야 합니다");
  const delim = lines[0].includes("\t") ? "\t" : ",";
  const parse = l => {   // 따옴표 지원 간단 CSV 파서
    const out = []; let cur = "", q = false;
    for (let i = 0; i < l.length; i++) {
      const c = l[i];
      if (q) { if (c === '"') { if (l[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
      else if (c === '"') q = true;
      else if (c === delim) { out.push(cur); cur = ""; }
      else cur += c;
    }
    out.push(cur);
    return out.map(s => s.trim());
  };
  const head = parse(lines[0]);
  const colMap = {};   // 열 index → 필드
  head.forEach((h, i) => {
    for (const [k, f] of Object.entries(IMPORT_COLS)) if (h.includes(k)) { colMap[i] = f; break; }
  });
  if (!Object.keys(colMap).length)
    return toast("헤더에서 값 컬럼을 못 찾았습니다 — '단가·소비일·안전재고·시급' 중 하나 이상이 헤더에 있어야 합니다");
  let nameIdx = head.findIndex(h => /제품명|자재명|이름|name/i.test(h));
  if (nameIdx < 0) nameIdx = 0;   // 명시 없으면 첫 컬럼 = 이름
  const rows2 = [];
  for (let i = 1; i < lines.length; i++) {
    const c = parse(lines[i]);
    const name = (c[nameIdx] || "").trim();
    if (!name) continue;
    const row = { name };
    let has = false;
    for (const [idx, f] of Object.entries(colMap)) {
      const v = (c[+idx] || "").replace(/,/g, "").trim();
      if (v !== "" && !isNaN(Number(v))) { row[f] = Number(v); has = true; }
    }
    if (has) rows2.push(row);
  }
  if (!rows2.length) return toast("적용할 행이 없습니다 (이름과 숫자 값이 있는 행 기준)");
  const fieldNames = [...new Set(Object.values(colMap))]
    .map(f => Object.keys(IMPORT_COLS).find(k => IMPORT_COLS[k] === f)).join(" · ");
  if (!confirm(`CSV ${rows2.length}건을 '${MCOLS[mTab].label}' 탭에 적용할까요?\n\n· 갱신 항목: ${fieldNames}\n· 매칭 기준: 이름 정확히 일치\n· 예시: ${rows2.slice(0, 3).map(r => r.name).join(", ")}${rows2.length > 3 ? " …" : ""}`)) return;
  const res = await api(`/api/masters/${mTab}/bulkset`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows: rows2 }) });
  await reloadMaster(mTab);
  renderMasters();
  toast(`📤 ${res.applied}건 적용 완료${res.missed_total ? ` · 이름 미매칭 ${res.missed_total}건` : ""}`);
  if (res.missed && res.missed.length)
    setTimeout(() => alert("이름이 일치하지 않아 건너뛴 항목:\n" + res.missed.join("\n")), 300);
}

/* ── 포장 세트 관리 — 탭 2개: 세트 목록(수정/삭제) / 세트 만들기 ──
   한 부재료가 여러 세트에 동시에 속할 수 있다 (pack_set_member 다대다) */
const PSET = { name: "", checked: new Set(), q: "", tab: "list", editing: "" };
$("mPackSet").onclick = async () => {
  await loadPackSets();
  psetTab("list");
  $("packSetOverlay").classList.add("on");
};
function psetTab(t) {
  PSET.tab = t;
  document.querySelectorAll("#packSetTabs button").forEach(b => b.classList.toggle("on", b.dataset.pstab === t));
  $("packSetListPane").style.display = t === "list" ? "" : "none";
  $("packSetEditPane").style.display = t === "edit" ? "" : "none";
  $("packSetSave").style.display = t === "edit" ? "" : "none";
  if (t === "list") renderPackSetSets(); else renderPackSetList();
}
$("packSetTabs").addEventListener("click", e => {
  const b = e.target.closest("[data-pstab]"); if (!b) return;
  if (b.dataset.pstab === "edit") psetNew();   // '＋ 세트 만들기'는 항상 빈 폼으로
  else psetTab("list");
});
/* 탭1 — 기존 세트 목록 (구성원 표시 + 수정·삭제) */
function renderPackSetSets() {
  $("packSetListCount").textContent = PACKSETS.length
    ? `등록된 세트 ${PACKSETS.length}개 — [수정]으로 구성원·이름을 바꾸고, [삭제]로 묶음만 해제합니다 (부재료는 지워지지 않습니다)`
    : "등록된 세트가 없습니다 — [＋ 세트 만들기]로 추가하세요";
  $("packSetSets").innerHTML = PACKSETS.map(s => `
    <div style="border:1px solid var(--line-soft); border-radius:8px; padding:8px 10px; margin-bottom:8px;">
      <div style="display:flex; align-items:center; gap:8px;">
        <b style="flex:1; font-size:13px;">📦 ${esc(s.name)}
          <span class="auto" style="font-weight:500">· ${s.members.length}종</span></b>
        <button class="btn ghost sm" data-psedit="${esc(s.name)}">수정</button>
        <button class="btn ghost sm" style="color:var(--crit)" data-psdel="${esc(s.name)}">삭제</button>
      </div>
      <div class="auto" style="font-size:11.5px; margin-top:4px; line-height:1.5;">
        ${s.members.map(m => `${esc(m.name)}${m.pack_count > 0 ? ` (${NF(m.pack_count)}개입)` : ""}`).join(" · ")}
      </div></div>`).join("") || '<div class="auto" style="padding:14px">세트 없음</div>';
}
$("packSetSets").addEventListener("click", async e => {
  const ed = e.target.closest("[data-psedit]");
  if (ed) { psetEdit(ed.dataset.psedit); return; }
  const dl = e.target.closest("[data-psdel]");
  if (!dl) return;
  const name = dl.dataset.psdel;
  if (!confirm(`'${name}' 세트를 삭제할까요?\n\n부재료 자체는 지워지지 않고 묶음만 해제됩니다.\n이 세트로 지정된 LOT 구간은 '포장 미지정'이 됩니다.`)) return;
  try {
    const r = await api("/api/packset/" + encodeURIComponent(name), { method: "DELETE" });
    toast(`'${name}' 세트 삭제됨 — ${r.released}종 해제` + (r.lots ? `, LOT 구간 ${r.lots}건 포장 해제` : ""));
    await loadPackSets(); await reloadMaster("sub");
    renderPackSetSets(); renderMasters();
  } catch (err) { /* api()가 토스트 */ }
});
/* 탭2 — 세트 만들기/수정 */
function psetNew() {
  PSET.name = ""; PSET.editing = ""; PSET.checked = new Set(); PSET.q = "";
  $("packSetName").value = ""; $("packSetSearch").value = "";
  $("packSetNameDl").innerHTML = PACKSETS.map(s => `<option value="${esc(s.name)}">`).join("");
  psetTab("edit");
}
function psetEdit(name) {
  PSET.name = name; PSET.editing = name; PSET.q = "";
  PSET.checked = new Set(packSetMembers(name).map(m => m.id));
  $("packSetName").value = name; $("packSetSearch").value = "";
  $("packSetNameDl").innerHTML = PACKSETS.map(s => `<option value="${esc(s.name)}">`).join("");
  psetTab("edit");
}
function renderPackSetList() {
  const q = PSET.q.toLowerCase();
  // 개수 부재료(개입수>0) 우선, 그 외 부재료도 선택 가능
  const list = M.sub.filter(m => m.status !== "중단" && (!q || m.name.toLowerCase().includes(q)))
    .sort((a, b) => (b.pack_count > 0) - (a.pack_count > 0) || a.name.localeCompare(b.name, "ko"));
  $("packSetCount").textContent = PSET.name
    ? `'${PSET.name}' 세트 — 선택 ${PSET.checked.size}종` + (q ? ` · 검색 ${list.length}건` : "")
    : `세트 이름을 먼저 입력하세요 · 검색 ${list.length}건`;
  $("packSetList").innerHTML = list.map(m => {
    const on = PSET.checked.has(m.id);
    // 다른 세트 소속은 '함께 속함' 안내일 뿐 — 중복 소속이 허용되므로 이동시키지 않는다
    const others = packSetsOf(m.id).filter(n => n !== PSET.name);
    return `<label style="display:flex; align-items:center; gap:8px; padding:7px 10px; border-bottom:1px solid var(--line-soft); cursor:pointer; ${on ? "background:var(--accent-soft)" : ""}">
      <input type="checkbox" data-psetmid="${m.id}" ${on ? "checked" : ""}>
      <span style="flex:1; font-size:13px;">${esc(m.name)}${m.pack_count > 0 ? ` <span class="auto">· ${NF(m.pack_count)}개입</span>` : ' <span class="auto" style="color:var(--warn)">· 개입수 없음</span>'}</span>
      ${others.length ? `<span class="chip cat" style="font-size:10px" title="다른 세트에도 속해 있습니다 — 중복 소속 가능하므로 그대로 유지됩니다">${others.map(esc).join(", ")}</span>` : ""}</label>`;
  }).join("") || '<div class="auto" style="padding:14px">검색 결과 없음</div>';
}
$("packSetName").addEventListener("input", e => {
  PSET.name = e.target.value.trim();
  // 새로 만들 때 기존 세트명을 고르면 그 구성원을 불러온다 (수정 중엔 이름만 바꾸는 것이므로 유지)
  if (!PSET.editing) {
    const members = packSetMembers(PSET.name);
    if (members.length) PSET.checked = new Set(members.map(m => m.id));
  }
  renderPackSetList();
});
$("packSetSearch").addEventListener("input", e => { PSET.q = e.target.value.trim(); renderPackSetList(); });
$("packSetList").addEventListener("change", e => {
  const cb = e.target.closest("[data-psetmid]"); if (!cb) return;
  const mid = +cb.dataset.psetmid;
  if (cb.checked) PSET.checked.add(mid); else PSET.checked.delete(mid);
  $("packSetCount").textContent = PSET.name ? `'${PSET.name}' 세트 — 선택 ${PSET.checked.size}종` : "세트 이름을 먼저 입력하세요";
});
$("packSetSave").onclick = async () => {
  if (!PSET.name) return toast("세트 이름을 입력하세요");
  if (PSET.checked.size < 1) return toast("세트에 넣을 부재료를 하나 이상 선택하세요");
  try {
    await api("/api/packset", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: PSET.name, mids: [...PSET.checked],
        rename: PSET.editing && PSET.editing !== PSET.name ? PSET.editing : "" }) });
  } catch (e) { return; }
  toast(`'${PSET.name}' 세트 — 부재료 ${PSET.checked.size}종 저장됨`);
  await loadPackSets(); await reloadMaster("sub");
  renderMasters();
  psetTab("list");        // 저장 후 목록으로 돌아가 결과를 바로 확인
};

// 기준정보 CSV — 현재 탭의 표시 목록(검색·미등록 필터 반영)을 그대로 내보냄
$("mCsv").onclick = () => {
  if (mTab === "bom" || mTab === "users") return toast("이 탭은 CSV를 지원하지 않습니다");
  const cfg = MCOLS[mTab];
  const { list } = masterList();
  if (!list.length) return toast("내보낼 데이터가 없습니다");
  const div = document.createElement("div");
  const strip = h => { div.innerHTML = h; return `"${div.textContent.replace(/\s+/g, " ").trim().replace(/"/g, '""')}"`; };
  const lines = [cfg.cols.map(c => `"${c}"`).join(",")]
    .concat(list.map(r => cfg.row(r).map(strip).join(",")));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  const fname = csvName("기준정보", cfg.label + "_" + todayISO());
  a.href = URL.createObjectURL(blob); a.download = fname; a.click();
  URL.revokeObjectURL(a.href);
  toast(`📄 ${fname} 저장됨`);
};
function updateTabCounts() {
  document.querySelectorAll("#itemTabs button").forEach(b => {
    const k = b.dataset.mt;
    if (k === "bom") { b.innerHTML = "배합비"; return; }
    if (k === "users") { b.innerHTML = "사용자"; return; }
    if (k === "audit") { b.innerHTML = "이력"; return; }
    b.innerHTML = `${MCOLS[k].label} <span class="num" style="font-weight:500;color:var(--faint)">${(M[k] || []).length}</span>`;
  });
}

/* 변경 이력 (admin) — 누가 언제 뭘 바꿨는지 최근 300건 */
const AUDIT_LABELS = { save_day: "일일 저장", backup: "백업", restore: "복원", bulk_import: "일괄 가져오기",
  integrity_fix: "체인 복구", user_role: "권한 변경", disposal: "폐기", product_image: "제품 이미지" };
async function renderAuditTab() {
  const list = await api("/api/audit?limit=300");
  $("mHead").innerHTML = `<tr><th>시각</th><th>사용자</th><th>동작</th><th>내용</th></tr>`;
  $("mBody").innerHTML = list.map(r => {
    const act = AUDIT_LABELS[r.action] || (String(r.action || "").startsWith("update_") ? "수정"
      : String(r.action || "").startsWith("create_") ? "등록"
      : String(r.action || "").startsWith("stock_adjust") ? "재고 보정" : esc(r.action || "—"));
    return `<tr><td class="num auto" style="white-space:nowrap">${esc((r.at || "").slice(5, 16))}</td>
      <td><b>${esc(r.username || "—")}</b></td>
      <td><span class="chip cat">${act}</span></td>
      <td class="auto" style="white-space:normal; word-break:break-all; max-width:520px; font-size:12px;">${esc(String(r.detail || "").slice(0, 200))}</td></tr>`;
  }).join("") || '<tr><td colspan="4" class="auto">이력이 없습니다</td></tr>';
  $("mHint").textContent = "최근 300건 · 과거 기록엔 사용자가 비어 있을 수 있습니다 (사용자 기록은 오늘부터 시작)";
}

/* 사용자 관리 (admin 전용) */
async function renderUsersTab() {
  const list = await api("/api/users");
  $("mHead").innerHTML = `<tr><th>아이디</th><th>권한</th>
    <th title="체크한 항목만 그 사용자가 일일 입력에서 저장할 수 있습니다 (여러 개 지정 가능)">담당 (복수 지정)</th>
    <th title="체크한 금액 항목만 그 사용자에게 보입니다 (admin은 항상 전체)">금액 열람</th><th>생성일</th><th></th></tr>`;
  const dutyCell = u => {
    const ds = dutySet(u.duty);
    const boxes = Object.entries(DUTY_LABELS).map(([k, lbl]) =>
      `<label style="display:inline-flex; align-items:center; gap:4px; margin-right:10px; font-size:12px; cursor:pointer; white-space:nowrap;">
        <input type="checkbox" data-uduty="${u.id}" data-dkey="${k}" data-uname="${esc(u.username)}" ${ds.has(k) ? "checked" : ""}
          style="width:15px; height:15px;">${lbl}</label>`).join("");
    const tag = ds.size === DUTY_KEYS.length ? '<span class="chip ok">전체</span>'
      : ds.size === 0 ? '<span class="chip warn">담당 없음 (입력 불가)</span>' : "";
    return boxes + (tag ? ` ${tag}` : "");
  };
  $("mBody").innerHTML = list.map(u => {
    const mp = new Set((u.money_perms || "").split(",").filter(Boolean));
    const moneyCell = u.role === "admin"
      ? '<span class="chip cat">전체</span>'
      : Object.entries(MONEY_LABELS).map(([k, lbl]) =>
          `<label style="display:inline-flex; align-items:center; gap:4px; margin-right:10px; font-size:12px; cursor:pointer; white-space:nowrap;">
            <input type="checkbox" data-umoney="${u.id}" data-mkey="${k}" data-uname="${esc(u.username)}" ${mp.has(k) ? "checked" : ""}
              style="width:15px; height:15px;">${lbl}</label>`).join("");
    return `<tr>
    <td><b>${esc(u.username)}</b>${u.username === USERNAME ? ' <span class="chip ok">본인</span>' : ""}</td>
    <td>${u.role === "admin"
      ? '<span class="chip cat">admin</span>'
      : `<select class="mini-sel" data-urole="${u.id}" data-uname="${esc(u.username)}" style="max-width:150px">
           <option value="op" ${u.role === "op" ? "selected" : ""}>op (입력 가능)</option>
           <option value="guest" ${u.role === "guest" ? "selected" : ""}>guest (보기 전용)</option>
         </select>`}</td>
    <td style="white-space:normal; max-width:300px;">${u.role === "admin"
      ? '<span class="chip cat">전체</span>'
      : dutyCell(u)}</td>
    <td style="white-space:normal; max-width:340px;">${moneyCell}</td>
    <td class="auto">${(u.created_at || "").slice(0, 10)}</td>
    <td>${u.username !== USERNAME ? `<button class="btn ghost sm" data-udelusr="${u.id}" data-uname="${esc(u.username)}">삭제</button>` : ""}</td></tr>`;
  }).join("");
  $("mHint").textContent = "권한: admin=전체 · op=입력 가능 · guest=보기 전용 | 담당: 체크한 항목만 일일 입력에서 저장 가능 — 여러 개 지정 가능, 전부 체크=전체, 하나도 없으면 저장 불가 (특이사항 메모는 담당이 하나라도 있으면 가능) | 금액: 기본은 전부 숨김 — 체크한 항목만 그 사용자에게 표시. 바꾸면 접속 중인 화면에도 20초 내 적용";
  $("mAdd").textContent = "+ 새 사용자";
}
$("mBody").addEventListener("change", async e => {
  // 금액 열람 체크박스: 그 행의 체크 상태를 모아 한 번에 저장
  const mc = e.target.closest("[data-umoney]");
  if (mc) {
    const uid = mc.dataset.umoney;
    const keys = [...document.querySelectorAll(`[data-umoney="${uid}"]`)]
      .filter(c => c.checked).map(c => c.dataset.mkey);
    try {
      await api("/api/users/" + uid, { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ money_perms: keys }) });
      toast(`'${mc.dataset.uname}' 금액 열람: ${keys.length ? keys.map(k => MONEY_LABELS[k]).join(", ") : "전부 숨김"} — 접속 중이면 20초 내 적용`);
    } catch (err) {
      renderUsersTab();   // 실패 시 원복
    }
    return;
  }
  // 담당 체크박스: 그 행의 체크 상태를 모아 한 번에 저장 (여러 담당 지정 가능)
  const dc = e.target.closest("[data-uduty]");
  if (dc) {
    const uid = dc.dataset.uduty;
    const keys = [...document.querySelectorAll(`[data-uduty="${uid}"]`)]
      .filter(c => c.checked).map(c => c.dataset.dkey);
    try {
      await api("/api/users/" + uid, { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duty: keys }) });
      const lbl = keys.length === DUTY_KEYS.length ? "전체"
        : keys.length ? keys.map(k => DUTY_LABELS[k]).join(", ") : "담당 없음 (입력 불가)";
      toast(`'${dc.dataset.uname}' 담당: ${lbl} — 접속 중이면 즉시 적용`);
      // 전체/담당없음 칩만 그 자리에서 갱신 — 표를 통째로 다시 그리면 연속 클릭이 끊긴다
      const cell = dc.closest("td");
      const old = cell.querySelector(".chip");
      if (old) old.remove();
      const tag = keys.length === DUTY_KEYS.length ? '<span class="chip ok">전체</span>'
        : keys.length === 0 ? '<span class="chip warn">담당 없음 (입력 불가)</span>' : "";
      if (tag) cell.insertAdjacentHTML("beforeend", " " + tag);
    } catch (err) {
      renderUsersTab();   // 실패 시 원복
    }
    return;
  }
  const rSel = e.target.closest("[data-urole]");
  if (!rSel) return;
  try {
    await api("/api/users/" + rSel.dataset.urole, { method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: rSel.value }) });
    toast(`'${rSel.dataset.uname}' → ${rSel.value}(으)로 변경됨 — 접속 중이면 즉시 적용`);
  } catch (err) {
    renderUsersTab();   // 실패 시 원복
  }
});
function openUserModal() {
  mstEdit = { type: "users", id: null };
  $("mstTitle").textContent = "새 사용자 등록";
  $("mstHint").textContent = "admin = 전체 · op = 시급 제외 전체 · guest = 보기 전용";
  $("mstForm").innerHTML = `
    <div class="fld"><label>아이디 *</label><input data-mf="username"></div>
    <div class="fld"><label>비밀번호 *</label><input data-mf="password" type="password"></div>
    <div class="fld"><label>권한</label><select data-mf="role">
      <option value="guest">guest (보기 전용)</option>
      <option value="op">op (시급·단가 제외 전체)</option>
      <option value="admin">admin (전체)</option></select></div>
    <div class="fld"><label>담당 (일일 입력 저장 범위 · 여러 개 선택 가능)</label>
      <div style="display:flex; flex-wrap:wrap; gap:4px 14px; padding:2px 0;">
        ${Object.entries(DUTY_LABELS).map(([k, lbl]) =>
          `<label style="display:inline-flex; align-items:center; gap:5px; font-size:12.5px; cursor:pointer; white-space:nowrap;">
            <input type="checkbox" data-newduty="${k}" checked style="width:15px; height:15px;">${lbl}</label>`).join("")}
      </div>
      <div class="hint" style="margin-top:2px;">전부 체크 = 전체 · 하나도 없으면 일일 입력 저장 불가</div></div>`;
  $("mstOverlay").classList.add("on");
}

/* ── 배합비 (BOM) ─────────────────────── */
const BOM = { pid: null, rows: [], view: "", w: {}, q: "" };   // q = 제품 검색어 (드롭다운 좁히기)
const BOM_UNITS = ["g", "kg", "ea", "매", "롤"];
async function loadBom(pid) {
  BOM.pid = pid;
  BOM.rows = (await api("/api/bom/" + pid)).map(r => ({
    material_id: r.material_id, qty_per_unit: r.qty_per_unit, unit: r.unit || "g",
    block: r.block || "", batch_qty: r.batch_qty || 0, block_yield: r.block_yield || 0,
    note: r.note || "" }));
  BOM.w = {};   // 블록별 분할 무게(1개당 g) — 전체무게 ÷ 수율로 역산해 초기 표시
  BOM.loadedFor = pid;
  renderBomRows();
}
function rowYield(r) {   // 그 행이 속한 배합 블록의 1배합당 생산수량 (없으면 제품 도우 수율)
  return Number(r.block_yield) || bomYield();
}
function blockTotal(b) {  // 블록 전체 무게 (자재 소요량 합, g)
  return BOM.rows.filter(r => r.block === b)
    .reduce((s, r) => s + (Number(r.batch_qty) || 0), 0);
}
/* 블록 계산 바: 전체 무게(자동 합계) ÷ 분할 무게(입력) = 1배합당 생산수량 — 공장 산정 공식 */
function renderBomBlocks() {
  const box = $("bomBlocks");
  const blocks = BOM.pid ? [...new Set(BOM.rows.map(r => r.block).filter(b => b))]
    .sort((a, b) => (a === "토핑") - (b === "토핑")) : [];
  if (!blocks.length) { box.style.display = "none"; return; }
  box.style.display = "flex";
  box.innerHTML = blocks.map(b => {
    const total = blockTotal(b);
    const y = Number(BOM.rows.find(r => r.block === b && Number(r.block_yield) > 0)?.block_yield) || 0;
    const w = BOM.w[b] ?? (y > 0 && total > 0 ? Math.round(total / y * 100) / 100 : "");
    return `<span class="chip cat">${b} 배합</span>
      <span class="num">전체 <b data-btotal="${b}">${NF(Math.round(total))}</b> g ÷ 분할 무게</span>
      <input class="mini-input num" data-bw="${b}" value="${w}" style="width:64px" title="1개당 ${b} 무게 (g)"> g
      <span class="num">= 1배합 ≈ <b data-byield="${b}">${y ? NF(Math.round(y)) : "—"}</b>개</span>`;
  }).join('<span style="width:16px"></span>');
}
/* 블록 재계산: 분할 무게가 있으면 수율 = 전체무게 ÷ 분할무게, 각 행 1개당도 갱신 (제자리 — 포커스 유지) */
function recalcBlock(b) {
  const total = blockTotal(b);
  const tEl = $("bomBlocks").querySelector(`[data-btotal="${b}"]`);
  if (tEl) tEl.textContent = NF(Math.round(total));
  const w = Number(BOM.w[b]) || 0;
  if (!(w > 0) || !(total > 0)) return;
  const y = total / w;
  BOM.rows.forEach(r => {
    if (r.block !== b) return;
    r.block_yield = y;
    if (Number(r.batch_qty) > 0) r.qty_per_unit = Number(r.batch_qty) / y;
  });
  const yEl = $("bomBlocks").querySelector(`[data-byield="${b}"]`);
  if (yEl) yEl.textContent = NF(Math.round(y));
  // 계산 셀은 data 속성으로 찾는다 — 모드(1배합당/1개당)·개수 자재(colspan)마다 열 위치가 달라
  // 인덱스로 쓰면 단위 select나 납품처 칸을 덮어쓴다
  document.querySelectorAll("#mBody tr[data-bi]").forEach(tr => {
    const r = BOM.rows[+tr.dataset.bi];
    if (!r || r.block !== b) return;
    const per = tr.querySelector("[data-bper]"), k = tr.querySelector("[data-bk]");
    if (per) per.textContent = r.qty_per_unit ? NF(Math.round(r.qty_per_unit * 10000) / 10000) : "—";
    if (k) k.textContent = perThousand(r);
  });
}
$("bomBlocks").addEventListener("input", e => {
  const b = e.target.dataset.bw;
  if (!b) return;
  BOM.w[b] = Number(String(e.target.value).replace(/,/g, "")) || 0;
  recalcBlock(b);
});
function bomYield() {
  const p = productById(BOM.pid);
  return p && p.batch_yield > 0 ? p.batch_yield : 0;
}
async function renderBomTab() {
  // 제품 = 제품탭 기준 드롭다운 — 반죽/토핑 배합이 있는 제품은 '제품 — 반죽', '제품 — 토핑' 항목으로 분리
  $("qaMaterials").innerHTML = M.raw.concat(M.sub).map(o => `<option value="${esc(o.name)}">`).join("");   // 검색 추가용
  await ensureBomAll();
  const has = new Set(Object.keys(BOMALL).map(Number));
  const all = M.product.filter(p => p.status !== "단종")
    .slice().sort((a, b) => a.name.localeCompare(b.name, "ko"));
  // 제품 검색 — 이름 일부로 목록을 좁힌다.
  // 선택 중인 제품이 검색에 안 걸려도 목록에서 사라지면 안 되므로 **맨 뒤 별도 그룹**으로 남긴다.
  // (앞에 두면 'Enter = 첫 결과'가 검색어와 무관한 그 제품을 잡고, 건수도 부풀려진다)
  const q = (BOM.q || "").toLowerCase();
  const prods = q ? all.filter(p => p.name.toLowerCase().includes(q)) : all;
  const withB = prods.filter(p => has.has(p.id));
  const without = prods.filter(p => !has.has(p.id));
  if (!BOM.pid && prods.length) BOM.pid = (withB[0] || prods[0]).id;
  const cur = productById(BOM.pid);
  const keepCur = q && cur && !prods.some(p => p.id === BOM.pid);
  const opt = p => `<option value="${p.id}" ${p.id === BOM.pid ? "selected" : ""}>${esc(p.name)}</option>`;
  $("bomProdSel").innerHTML =
    (withB.length ? `<optgroup label="✔ 배합비 등록 (${withB.length})">${withB.map(opt).join("")}</optgroup>` : "")
    + (without.length ? `<optgroup label="배합비 미등록 (${without.length})">${without.map(opt).join("")}</optgroup>` : "")
    + (keepCur ? `<optgroup label="— 현재 선택 (검색 결과 아님)"><option value="${cur.id}" data-cur selected>${esc(cur.name)}</option></optgroup>` : "")
    || `<option value="">검색 결과 없음</option>`;
  const allWith = all.filter(p => has.has(p.id)).length;
  $("bomCount").textContent = q
    ? `검색 ${prods.length}건 / 전체 ${all.length}제품 (배합비 등록 ${allWith})`
    : `배합비 등록 ${allWith} / 전체 ${all.length}제품`;
  // 복사 목록은 검색과 무관하게 배합비 있는 제품 전체 (검색으로 좁히면 복사할 대상을 못 찾는다)
  $("bomCopySel").innerHTML = `<option value="">다른 제품 배합비 복사…</option>` +
    all.filter(p => has.has(p.id) && p.id !== BOM.pid)
      .map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("mAdd").style.display = "none";        // 하단 추가 버튼·검색 제거 — 배합은 각 섹션 헤더의 검색/버튼으로 추가
  $("bomAddSearch").style.display = "none";
  // 배합비가 등록된 제품일 때만 '배합비 삭제' 노출 (admin)
  $("bomDelete").style.display = (ROLE === "admin" && BOM.pid && has.has(BOM.pid)) ? "" : "none";
  $("mHint").textContent = "1배합당 소요량(g) 기준으로 편집합니다 · 반죽/토핑 각 섹션 옆의 🔍 검색으로 자재를 추가합니다 · 저장은 제품 단위(두 배합 함께 보존)";
  if (BOM.pid && BOM.loadedFor !== BOM.pid) { loadBom(BOM.pid); return; }
  renderBomRows();
}
$("bomProdSel").addEventListener("change", e => {
  const pid = +e.target.value;
  if (!pid) return;
  BOM.pid = pid;
  // 제품을 고르면 검색은 역할이 끝났다 — 해제해 목록을 전체로 되돌린다.
  // (안 그러면 고른 제품이 '현재 선택 (검색 결과 아님)'으로 밀려 목록이 헷갈린다)
  BOM.q = ""; $("bomProdSearch").value = "";
  renderBomTab();
});
/* 제품 검색 — 옆 드롭다운 목록을 좁힌다 (Enter = 첫 결과 열기).
   자동완성 목록에서 제품명을 그대로 고르면 = 그 제품을 선택한 것으로 보고 바로 연다. */
$("bomProdSearch").addEventListener("input", e => {
  const v = e.target.value.trim();
  const hit = v ? M.product.find(p => p.status !== "단종" && p.name === v) : null;
  if (hit) {                       // 이름이 정확히 일치 → 선택으로 처리하고 검색 해제
    BOM.pid = hit.id;
    BOM.q = ""; e.target.value = "";
    renderBomTab();
    return;
  }
  BOM.q = v;
  renderBomTab();
});
$("bomProdSearch").addEventListener("focus", () => {
  $("qaProducts").innerHTML = M.product.filter(p => p.status !== "단종")
    .map(o => `<option value="${esc(o.name)}">`).join("");
});
$("bomProdSearch").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  // '현재 선택(검색 결과 아님)' 항목은 건너뛴다 — 검색어와 무관하므로
  const first = $("bomProdSel").querySelector("option[value]:not([value='']):not([data-cur])");
  if (!first) return toast("검색 결과가 없습니다");
  BOM.pid = +first.value;
  renderBomTab();
});
// 미등록 제품에 기존 제품의 배합비를 복사 (제품명이 달라 임포트 매칭이 안 된 경우의 수동 매칭)
$("bomCopyBtn").onclick = async () => {
  if (!BOM.pid) return toast("제품을 먼저 선택하세요");
  const src = +$("bomCopySel").value;
  if (!src) return toast("복사해올 제품을 먼저 선택하세요 ('다른 제품 배합비 복사…')");
  await ensureBomAll();
  const rows = BOMALL[src] || [];
  if (!rows.length) return toast("선택한 제품의 배합비가 없습니다");
  const srcName = (productById(src) || {}).name || "?";
  if (BOM.rows.length && !confirm(`편집 중인 배합 ${BOM.rows.length}행을 '${srcName}' 배합비로 교체할까요?`)) return;
  BOM.rows = rows.map(r => ({ material_id: r.material_id, qty_per_unit: r.qty_per_unit,
    unit: r.unit || "g", block: r.block || "", batch_qty: r.batch_qty || 0,
    block_yield: r.block_yield || 0, note: `${srcName} 배합 복사` }));
  renderBomRows();
  toast(`'${srcName}' 배합비 ${rows.length}종 복사됨 — [배합비 저장]으로 확정하세요`);
};
function perThousand(r) {
  if (!r.qty_per_unit) return "—";
  const v = Number(r.qty_per_unit) * 1000;
  if (r.unit === "g") return NF(v / 1000) + " kg";
  return NF(v) + " " + r.unit;
}
function renderBomRows() {
  const all = M.raw.concat(M.sub);
  const y = bomYield();
  renderBomBlocks();   // 전체무게 ÷ 분할무게 = 수율 계산 바
  const hasBlocks = BOM.rows.some(r => r.block);
  const chip = $("bomYieldChip");
  chip.style.display = BOM.pid && !hasBlocks ? "" : "none";   // 계산 바가 있으면 칩 생략
  chip.textContent = y
    ? `1배합 ≈ ${NF(Math.round(y))}개`
    : "⚠ 1배합당 생산수량 미등록 (제품 수정 또는 아래 분할 무게로 계산)";
  // 반죽/토핑은 **항상** 1배합당(g) 기준으로 편집한다.
  // 전체무게 합계·분할무게 수율 계산이 전부 batch_qty(1배합당) 기준이라, 여기서 1개당으로 받으면
  // 신규 등록 제품의 전체무게가 0으로 남는다. 1배합 생산수량은 분할 무게로 역산하면 되므로
  // '1배합당 생산수량 미등록'이어도 입력 기준은 바꾸지 않는다.
  const cols = 8;
  $("mHead").innerHTML = `<tr><th>자재명</th><th>구분</th><th class="r">1배합당 소요량 (g)</th>
       <th class="r">1개당 (g)</th><th class="r">1,000개당</th><th>납품처</th><th>비고</th><th></th></tr>`;
  const rowHtml = (r, i) => {
    const m = materialById(r.material_id) || {};
    const ry = rowYield(r);
    // 1배합당 값이 있으면 그대로, 없고 1개당만 있으면(옛 데이터·실측 계산) 역산해 보여준다
    const pb = Number(r.batch_qty) > 0 ? Math.round(Number(r.batch_qty) * 10) / 10
      : (r.qty_per_unit && ry > 0 ? Math.round(r.qty_per_unit * ry * 10) / 10 : "");
    const qtyCells = isCountMat(m)
      ? `<td class="r auto" colspan="3" style="text-align:left">📦 개수 자재 · 개입수 ${NF(m.pack_count)} → 소모 = 생산수량 ÷ 개입수 (자동)</td>`
      : `<td class="r"><input class="mini-input num w" data-bf="per_batch" value="${pb}"
           title="1배합에 들어가는 양 (g) — 수량을 안 적으면 0으로 저장됩니다"></td>
         <td class="r auto" data-bper title="1개당 = 1배합당 소요량 ÷ 1배합 생산수량${ry > 0 ? ` ${NF(Math.round(ry))}개` : " (분할 무게를 입력하면 계산됩니다)"}">${r.qty_per_unit ? NF(Math.round(r.qty_per_unit * 10000) / 10000) : "—"}</td>
         <td class="r auto" data-bk title="1,000개당 = 1개당 소요량 × 1,000 (kg 환산)">${perThousand(r)}</td>`;
    const pNames = bomPartnerIds(r).map(id => (M.partner.find(p => p.id === id) || {}).name).filter(Boolean);
    const extraCells = `<td><button class="note-cell ${pNames.length ? "" : "auto"}" data-bpartner="${i}" style="max-width:110px"
      title="이 자재를 쓰는 납품처 지정 (여러 곳 선택 가능) — 일일입력 계획 거래처 분배와 연동">${pNames.length ? esc(pNames.join(", ")) : "공통"}</button></td>`;
    return `<tr data-bi="${i}">
      <td>${matSel(r.material_id, 'data-bf="material_id"')}</td>
      <td>${m.kind ? `<span class="chip cat">${m.kind === "raw" ? "원재료" : "부재료"}</span>` : "—"}</td>
      ${qtyCells}${extraCells}
      <td>${r.note ? `<button class="note-cell" data-bnote="${i}" title="클릭하면 전체 보기/편집">${esc(r.note)}</button>`
        : `<button class="note-cell auto" data-bnote="${i}">＋ 비고</button>`}</td>
      <td><button class="btn ghost sm" data-bdel>삭제</button></td></tr>`;
  };
  // 표시할 배합 섹션: 반죽·토핑 항상 + 구분없음(실측 등) 행이 있으면
  const blocks = ["반죽", "토핑"];
  if (BOM.rows.some(r => !(r.block))) blocks.push("");
  const SEC = { "반죽": "🍞 반죽 배합", "토핑": "🍪 토핑 배합", "": "— 구분 없음 (실측 등)" };
  $("mBody").innerHTML = blocks.map(b => {
    const items = BOM.rows.map((r, i) => ({ r, i })).filter(x => (x.r.block || "") === b);
    const by = Number(BOM.rows.find(r => (r.block || "") === b && Number(r.block_yield) > 0)?.block_yield) || 0;
    const body = items.map(({ r, i }) => rowHtml(r, i)).join("")
      || `<tr><td colspan="${cols}" class="auto" style="padding:8px 10px;">${b ? `${b} 배합 자재가 없습니다 — 아래 [+ ${b} 자재 추가]로 만드세요` : "구분 없음 자재 없음"}</td></tr>`;
    // 추가 바는 그 배합 **아래**에 — 새 행이 목록 끝에 붙으므로 추가 지점과 같은 자리
    const bar = `<tr><td colspan="${cols}" style="background:var(--bg); padding:6px 10px;">
      <b style="font-size:12.5px;">${SEC[b]}</b>
      <span class="auto num" style="margin-left:6px; font-size:11.5px;">${items.length}종${by ? ` · 1배합 ≈ ${NF(Math.round(by))}개` : ""}</span>
      ${b ? `<span style="display:inline-flex; align-items:center; gap:6px; margin-left:10px; flex-wrap:wrap;">
        <input class="mini-input" data-addsearch="${b}" list="qaMaterials" placeholder="🔍 자재 검색 후 Enter = ${b}에 추가" style="text-align:left; width:230px;">
        <button class="btn ghost sm" data-addblock="${b}">+ ${b} 자재 추가</button>
        ${PACKSETS.length ? `<select class="mini-sel" data-addset="${b}" style="max-width:210px; font-size:11.5px;"
          title="포장 세트를 고르면 그 세트의 부재료를 한 번에 추가합니다">
          <option value="">📦 포장 세트로 한 번에 추가…</option>
          ${PACKSETS.map(s => `<option value="${esc(s.name)}">${esc(s.name)} (${s.members.length}종)</option>`).join("")}
        </select>` : ""}</span>` : ""}</td></tr>`;
    return body + bar;
  }).join("");
}
$("bomEstimate").onclick = async () => {
  if (!BOM.pid) return toast("제품을 먼저 선택하세요");
  const est = await api(`/api/bom/${BOM.pid}/estimate`);
  if (!est.length) return toast("이 제품의 실측 사용 데이터가 없습니다");
  BOM.rows = est.map(r => ({ material_id: r.material_id, qty_per_unit: r.qty_per_unit,
    unit: r.unit, block: "", batch_qty: 0, block_yield: 0, note: `실측 ${r.days}일 평균` }));
  renderBomRows();
  toast(`실측 기반 ${est.length}개 자재 계산됨 — 검토 후 저장하세요`);
};
$("bomSave").onclick = async () => {
  if (!BOM.pid) return toast("제품을 먼저 선택하세요");
  // 반죽 블록 수율을 제품 batch_yield로 함께 저장 (일일 입력 계획 자동계산이 이 값을 씀)
  const dough = BOM.rows.find(r => r.block === "반죽" && Number(r.block_yield) > 0);
  // 자재만 선택돼 있으면 저장 — 수량 미입력은 0으로 (행이 사라지지 않게)
  const num = v => { const n = Number(String(v ?? "").replace(/,/g, "")); return isFinite(n) ? n : 0; };
  await api("/api/bom/" + BOM.pid, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows: BOM.rows.filter(r => r.material_id).map(r => ({ ...r,
        qty_per_unit: num(r.qty_per_unit), batch_qty: num(r.batch_qty), block_yield: num(r.block_yield) })),
      batch_yield: dough ? Number(dough.block_yield) : null }) });
  toast("배합비 저장 완료");
  BOMALL = null;          // 일일 입력 '배합비 자동'이 새 배합을 쓰도록 캐시 무효화
  COSTS = null;           // 원가 분석도 새 배합 기준으로
  BOM.loadedFor = null;   // 저장본 재로드
  await reloadMaster("product");   // batch_yield 변경분을 제품 캐시에 반영
  renderBomTab();         // 등록/미등록 그룹·복사 목록도 갱신
};
$("bomDelete").onclick = async () => {
  if (!BOM.pid) return;
  const p = productById(BOM.pid);
  const n = ((BOMALL && BOMALL[BOM.pid]) || []).length;
  if (!confirm(`'${p ? p.name : ""}' 배합비를 통째로 삭제할까요?\n\n`
    + `• 배합 자재 ${n}종이 모두 지워집니다 (자재 자체는 안 지워짐)\n`
    + `• 제품의 1배합당 생산수량도 초기화됩니다\n\n이 작업은 되돌릴 수 없습니다.`)) return;
  try {
    const r = await api("/api/bom/" + BOM.pid, { method: "DELETE" });
    toast(`'${p ? p.name : ""}' 배합비 삭제됨 — ${r.removed}종 제거`);
    BOM.rows = []; BOM.loadedFor = null;
    BOMALL = null; COSTS = null;
    await reloadMaster("product");
    renderBomTab();
  } catch (e) { /* api()가 토스트 */ }
};
// 배합 자재 납품처 팝업 — 여러 곳 체크 선택
let bpIdx = -1;
function openBomPartner(i) {
  const row = BOM.rows[i]; if (!row) return;
  bpIdx = i;
  const m = materialById(row.material_id) || {};
  $("bpHint").textContent = `'${m.name || "자재"}'를 쓰는 납품처를 모두 체크하세요 — 아무것도 안 고르면 '공통'(전 거래처)입니다.`;
  const cur = new Set(bomPartnerIds(row));
  const sellers = M.partner.filter(isSeller);
  $("bpList").innerHTML = sellers.length
    ? sellers.map(p => `<label style="display:flex; align-items:center; gap:8px; font-size:13.5px; cursor:pointer;">
        <input type="checkbox" data-bpid="${p.id}" ${cur.has(p.id) ? "checked" : ""} style="width:17px; height:17px;">
        ${esc(p.name)}</label>`).join("")
    : '<div class="auto">등록된 판매처가 없습니다 — 기준정보 › 거래처에서 판매처를 등록하세요</div>';
  $("bpOverlay").classList.add("on");
}
window.closeBomPartner = () => { $("bpOverlay").classList.remove("on"); bpIdx = -1; };
$("bpSave").onclick = () => {
  if (bpIdx >= 0 && BOM.rows[bpIdx]) {
    const ids = [...document.querySelectorAll('#bpList [data-bpid]:checked')].map(c => +c.dataset.bpid);
    BOM.rows[bpIdx].partner_ids = ids.join(",");
    BOM.rows[bpIdx].partner_id = ids[0] || null;
  }
  closeBomPartner();
  renderBomRows();
};

// 비고 팝업 — 전체 보기/편집
let notePopIdx = -1;
function openBomNote(i) {
  const row = BOM.rows[i]; if (!row) return;
  notePopIdx = i;
  const m = materialById(row.material_id) || {};
  $("notePopHint").textContent = (m.name || "자재") + " 비고";
  $("notePopText").value = row.note || "";
  $("notePopOverlay").classList.add("on");
  setTimeout(() => $("notePopText").focus(), 30);
}
window.closeNotePop = () => { $("notePopOverlay").classList.remove("on"); notePopIdx = -1; };
$("notePopSave").onclick = () => {
  if (notePopIdx >= 0 && BOM.rows[notePopIdx]) BOM.rows[notePopIdx].note = $("notePopText").value.trim();
  closeNotePop();
  renderBomRows();
};
// 포장 세트로 한 번에 추가 — 세트 구성원(부재료)을 그 배합에 통째로 넣는다.
// (헤더 바에 있어 tr[data-bi]가 없으므로 별도 핸들러)
$("mBody").addEventListener("change", e => {
  const sel = e.target.closest("[data-addset]"); if (!sel) return;
  const name = sel.value;
  sel.value = "";                       // 다시 고를 수 있게 초기화
  if (!name) return;
  if (!BOM.pid) return toast("제품을 먼저 선택하세요");
  const blk = sel.dataset.addset;
  const members = packSetMembers(name);
  if (!members.length) return toast(`'${name}' 세트에 구성원이 없습니다`);
  const src = BOM.rows.find(x => (x.block || "") === blk && Number(x.block_yield) > 0);
  let added = 0, dup = 0;
  members.forEach(mm => {
    if (BOM.rows.some(r => r.material_id === mm.id && (r.block || "") === blk)) { dup++; return; }
    BOM.rows.push({ material_id: mm.id, qty_per_unit: "", unit: "g", block: blk, batch_qty: "",
      block_yield: src ? src.block_yield : (bomYield() || 0), note: name });
    added++;
  });
  renderBomRows();
  toast(added ? `'${name}' 세트 ${added}종을 ${blk}에 추가했습니다${dup ? ` (이미 있는 ${dup}종 제외)` : ""} — [배합비 저장]으로 확정하세요`
    : `'${name}' 세트 ${members.length}종이 이미 ${blk}에 있습니다`);
});
$("mBody").addEventListener("input", e => {
  if (mTab !== "bom") return;
  const tr = e.target.closest("tr[data-bi]"); if (!tr) return;
  const f = e.target.dataset.bf; if (!f) return;
  const row = BOM.rows[+tr.dataset.bi];
  row[f] = e.target.tagName === "SELECT" && (f === "material_id" || f === "partner_id")
    ? (e.target.value ? +e.target.value : null) : e.target.value;
  if (f === "block") {                         // 블록 변경 → 같은 블록의 수율 승계 후 1개당 재계산
    const src = BOM.rows.find(x => x !== row && x.block === row.block && Number(x.block_yield) > 0);
    row.block_yield = src ? src.block_yield : (Number(row.block_yield) || bomYield() || 0);
    const ry = rowYield(row);
    if (Number(row.batch_qty) > 0 && ry > 0) row.qty_per_unit = Number(row.batch_qty) / ry;
    renderBomRows();
    return;
  }
  if (e.target.tagName === "SELECT") { renderBomRows(); return; }
  if (f === "per_batch") {                     // 1배합당(엑셀 원본 단위) 입력 → 1개당 자동 환산
    // batch_qty는 수율과 무관하게 항상 저장한다 — 전체무게 합계가 이 값 기준이라
    // 수율 없는 신규 제품에서 여기 저장을 건너뛰면 전체무게가 0으로 남는다.
    row.batch_qty = Number(String(e.target.value).replace(/,/g, "")) || 0;
    row.unit = "g";
    const ry = rowYield(row);
    if (ry > 0) {                              // 수율을 알면 1개당까지 즉시 환산
      row.qty_per_unit = row.batch_qty / ry;
      if (!Number(row.block_yield)) row.block_yield = ry;
      const per = tr.querySelector("[data-bper]"), k = tr.querySelector("[data-bk]");
      if (per) per.textContent = row.qty_per_unit ? NF(Math.round(row.qty_per_unit * 10000) / 10000) : "—";
      if (k) k.textContent = perThousand(row);
    }
    if (row.block) recalcBlock(row.block);     // 전체무게 갱신 + 분할무게 기준 수율 재계산
    return;
  }
  if (f === "qty_per_unit") {                  // 1,000개당 제자리 갱신
    const k = tr.querySelector("[data-bk]");
    if (k) k.textContent = perThousand(row);
  }
});
$("mBody").addEventListener("click", e => {
  const mh = e.target.closest("[data-mhist]");
  if (mh) { openMatHistory(+mh.dataset.mhist); return; }
  const ph = e.target.closest("[data-phist]");
  if (ph) { openProdHistory(+ph.dataset.phist); return; }
  // 라인 행의 [＋ 공정] — 소속이 미리 선택된 등록 폼 (공정 이름만 입력하면 됨)
  const ap = e.target.closest("[data-addproc]");
  if (ap) {
    const parent = M.line.find(l => l.id === +ap.dataset.addproc);
    if (!parent) return;
    openMaster("line", null);
    $("mstTitle").textContent = `'${parent.name}' 공정 추가`;
    $("mstHint").textContent = "공정 이름만 입력하면 됩니다 (예: 배합 · 성형 · 포장) — 이 라인의 한 공정으로 묶여 집계됩니다.";
    const nameInp = document.querySelector('#mstForm [data-mf="name"]');
    if (nameInp) nameInp.value = parent.name;   // 표시 일관성: 공정 행 이름 = 라인명 (↳ 라인명으로 보임)
    const sel = document.querySelector('#mstForm [data-mf="parent_id"]');
    if (sel) sel.value = String(parent.id);
    const proc = document.querySelector('#mstForm [data-mf="process"]');
    if (proc) setTimeout(() => proc.focus(), 60);
    return;
  }
  const du = e.target.closest("[data-udelusr]");
  if (du) {
    if (!confirm(`사용자 '${du.dataset.uname}'을(를) 삭제할까요?`)) return;
    api("/api/users/" + du.dataset.udelusr, { method: "DELETE" })
      .then(() => { toast("삭제됨"); renderUsersTab(); }).catch(() => {});
    return;
  }
  if (mTab === "bom") {
    const ab = e.target.closest("[data-addblock]");
    if (ab) {
      if (!BOM.pid) return toast("제품을 먼저 선택하세요");
      const blk = ab.dataset.addblock;
      const s = ab.closest("td").querySelector(`[data-addsearch="${blk}"]`);
      bomAddByName((s && s.value.trim()) || null, blk);
      return;
    }
    const del = e.target.closest("[data-bdel]");
    if (del) {
      const tr = del.closest("tr[data-bi]");
      BOM.rows.splice(+tr.dataset.bi, 1);
      renderBomRows();
      return;
    }
    const nb = e.target.closest("[data-bnote]");
    if (nb) { openBomNote(+nb.dataset.bnote); return; }
    const bp = e.target.closest("[data-bpartner]");
    if (bp) { openBomPartner(+bp.dataset.bpartner); return; }
    return;
  }
  const del = e.target.closest("[data-delm]");
  if (del) {
    const row = (M[mTab] || []).find(r => r.id === +del.dataset.delm);
    if (!row) return;
    if (!confirm(`'${row.name}'을(를) 정말 삭제할까요?\n삭제 즉시 DB에 반영되며 되돌릴 수 없습니다.\n(생산·출고 등 기록이 있으면 삭제 대신 안내가 표시됩니다)`)) return;
    api(`/api/masters/${mTab}/${row.id}`, { method: "DELETE" }).then(async () => {
      toast(`'${row.name}' 삭제됨`);
      await reloadMaster(mTab);
      renderMasters();
    }).catch(() => {});
    return;
  }
  const b = e.target.closest("[data-edit]"); if (!b) return;
  openMaster(mTab, (M[mTab] || []).find(r => r.id === +b.dataset.edit));
});
$("mAdd").onclick = () => {
  if (mTab === "users") { openUserModal(); return; }
  if (mTab === "bom") {
    if (!BOM.pid) return toast("제품을 먼저 선택하세요");
    const s = $("bomAddSearch");
    if (s.value.trim()) { bomAddByName(s.value.trim()); s.value = ""; return; }
    bomAddByName(null);   // 빈 행
    return;
  }
  openMaster(mTab, null);
};
// 배합비에 자재 추가 — 검색 이름(name) 또는 null(빈 행), 지정한 배합(blk, 기본 반죽)으로 추가
function bomAddByName(name, blk = "반죽") {
  let hit = null;
  if (name) {
    const all = M.raw.concat(M.sub);
    hit = all.find(o => o.name === name);
    if (!hit) {
      const cands = all.filter(o => o.name.toLowerCase().includes(name.toLowerCase()));
      if (!cands.length) return toast(`'${name}' 검색 결과 없음`);
      if (cands.length > 1) return toast(`'${name}' 검색 결과 ${cands.length}건 — 목록에서 정확한 이름을 선택하세요`);
      hit = cands[0];
    }
  }
  const src = BOM.rows.find(x => (x.block || "") === blk && Number(x.block_yield) > 0);
  BOM.rows.push({ material_id: hit ? hit.id : null, qty_per_unit: "", unit: "g",
    block: blk, batch_qty: "", block_yield: src ? src.block_yield : (bomYield() || 0), note: "" });
  renderBomRows();
  if (hit) toast(`'${hit.name}' — ${blk} 배합에 추가됨 (소요량 입력 후 [배합비 저장])`);
}
// 각 배합 섹션 헤더의 검색창 — Enter로 그 배합에 자재 추가
$("mBody").addEventListener("keydown", e => {
  const s = e.target.closest("[data-addsearch]");
  if (!s || e.key !== "Enter") return;
  e.preventDefault();
  if (!BOM.pid) return toast("제품을 먼저 선택하세요");
  const v = s.value.trim();
  if (v) bomAddByName(v, s.dataset.addsearch);
});

/* 기준정보 등록/수정 모달 (탭별 폼) */
const MFORMS = {
  product: [["name", "제품명 *"], ["category", "카테고리"], ["spec", "규격 (예: 60g/EA)"],
    ["unit_price", "단가 (원)", "num"], ["shelf_days", "소비일 (일)", "num"], ["safety_stock", "안전재고", "num"],
    ["batch_yield", "1배합당 생산수량 (개)", "num"],
    ["initial_stock", "초기재고 (신규만)", "num"], ["stock_set", "현재고 (수정 시 기초재고 자동 조정)", "num"],
    ["status", "상태", "sel", ["판매중", "단종"]], ["note", "비고", "full"]],
  raw: [["name", "자재명 *"], ["kind", "구분", "sel", [["raw", "원재료"], ["sub", "부재료"]]],
    ["spec", "규격 (예: 20kg 포대)"], ["unit", "단위", "sel", ["kg", "g", "L", "개", "ea"]],
    ["pack_count", "개입수 (개수 단위일 때만 · 예: 16개입=16 → 소모=생산수량÷개입수)", "num"],
    ["pack_set", "포장 세트 (읽기 전용 — 부재료 탭의 [📦 포장 세트]에서 관리 · 여러 세트 동시 소속 가능)", "ro"],
    ["unit_price", "단가 (원)", "num"], ["safety_stock", "안전재고", "num"], ["initial_stock", "초기재고 (신규만)", "num"],
    ["stock_set", "현재고 (아래 기준일의 실사 기록으로 저장됩니다)", "num"], ["stock_date", "기준일 (이 날짜에 기록)", "date"],
    ["status", "상태", "sel", ["사용중", "중단"]], ["note", "비고", "full"]],
  sub: [["name", "자재명 *"], ["kind", "구분", "sel", [["sub", "부재료"], ["raw", "원재료"]]],
    ["spec", "규격 (예: 500ea/롤)"], ["unit", "단위", "sel", ["개", "ea", "롤", "박스", "묶음", "매"]],
    ["pack_count", "개입수 (개수 단위일 때만 · 예: 16개입=16 → 소모=생산수량÷개입수)", "num"],
    ["pack_set", "포장 세트 (읽기 전용 — 부재료 탭의 [📦 포장 세트]에서 관리 · 여러 세트 동시 소속 가능)", "ro"],
    ["unit_price", "단가 (원)", "num"], ["safety_stock", "안전재고", "num"], ["initial_stock", "초기재고 (신규만)", "num"],
    ["stock_set", "현재고 (아래 기준일의 실사 기록으로 저장됩니다)", "num"], ["stock_date", "기준일 (이 날짜에 기록)", "date"],
    ["prod_mult", "단위당 수량 (생산가능 환산)", "num"], ["prod_per", "1회 생산 소요량", "num"],
    ["status", "상태", "sel", ["사용중", "중단"]], ["note", "비고", "full"]],
  partner: [["name", "거래처명 *"], ["type", "유형 — 선택 또는 직접 입력 (예: 기부)", "combo", ["판매처", "자재 공급처", "용역업체"]], ["phone", "연락처"],
    ["contact", "담당자"], ["status", "상태", "sel", ["활성", "중지"]], ["note", "비고", "full"]],
  staff: [["name", "이름 *"], ["kind", "구분", "sel", ["정직원", "계약직", "용역", "일용직", "아르바이트", "파견"]],
    ["position", "직책", "combo", []], ["process", "담당 공정"],
    ["wage", "시급 (원)", "num"], ["join_date", "입사(계약)일", "date"], ["phone", "연락처"],
    ["status", "상태", "sel", ["재직", "계약중", "퇴사"]], ["note", "비고", "full"]],
  line: [["name", "라인명 *"], ["process", "공정"], ["std_hours", "정상가동시간 (h/일)", "num"],
    ["parent_id", "소속 라인 — 이 행이 어떤 물리 라인의 공정이면 그 대표 라인을 선택 (가동률·보고서가 한 라인으로 집계됨)", "sel", []],
    ["status", "상태", "sel", ["가동", "중지"]], ["note", "비고", "full"]],
};
let mstEdit = null;
function openMaster(type, row) {
  mstEdit = { type, id: row ? row.id : null };
  $("mstTitle").textContent = row ? MCOLS[type].label + " 정보 수정" : "새 " + MCOLS[type].label + " 등록";
  $("mstHint").textContent = row
    ? "수정 내용은 이후 입력분부터 적용됩니다. 과거 기록은 당시 값 그대로 보존됩니다."
    : "등록 즉시 일일 입력의 선택 목록에 나타납니다.";
  $("mstForm").innerHTML = MFORMS[type].map(([f, label, kind, opts]) => {
    if (row && f === "initial_stock") return "";
    if (!row && (f === "stock_set" || f === "stock_date")) return "";
    // 자재의 구분(원↔부)은 수정 시에만 — 신규는 탭이 곧 구분. 인원 등 다른 탭의 kind는 항상 표시
    if (!row && f === "kind" && (type === "raw" || type === "sub")) return "";
    if (f === "wage" && !canM("labor")) return "";     // 시급 — 노무비 권한
    if (f === "unit_price" && !canM(type === "product" ? "prod" : "mat")) return "";   // 단가 — 금액 권한
    const v = row
      ? (f === "stock_set" ? (row.stock != null ? Math.round(row.stock * 1000) / 1000 : "")
        : f === "stock_date" ? (row.stock_date || todayISO())
        : (row[f] ?? ""))
      : (kind === "combo" && opts && opts.length ? opts[0] : "");
    const cls = kind === "full" ? "fld full" : "fld";
    // 라인 폼 '소속 라인' — 옵션은 대표 라인들(소속 없는 라인)만, 자기 자신 제외 (동적)
    if (type === "line" && f === "parent_id")
      opts = [["", "— (독립 라인 / 대표)"]].concat(
        (M.line || []).filter(l => !l.parent_id && (!row || l.id !== row.id))
          .map(l => [l.id, l.name + (l.process ? " / " + l.process : "")]));
    // 인원 '직책' 콤보 옵션 = 기본 직책 + 이미 등록된 직책 (직접 입력도 가능)
    if (type === "staff" && f === "position")
      opts = [...new Set(["사장", "부장", "차장", "과장", "대리", "주임", "사원", "반장", "팀장", "이사"]
        .concat((M.staff || []).map(s => s.position).filter(Boolean)))];
    if (kind === "ro")      // 읽기 전용 표시 (실제 관리는 전용 화면에서)
      return `<div class="${cls}"><label>${label}</label>
        <input value="${esc(v || "—")}" disabled style="background:var(--bg); color:var(--muted)"></div>`;
    if (kind === "combo")   // 목록에서 고르거나 직접 입력 (datalist)
      return `<div class="${cls}"><label>${label}</label>
        <input data-mf="${f}" value="${esc(v)}" list="mstdl_${f}" placeholder="선택 또는 직접 입력">
        <datalist id="mstdl_${f}">${opts.map(o => `<option value="${esc(o)}">`).join("")}</datalist></div>`;
    if (kind === "sel")
      return `<div class="${cls}"><label>${label}</label><select data-mf="${f}">${opts.map(o => {
        const val = Array.isArray(o) ? o[0] : o, txt = Array.isArray(o) ? o[1] : o;
        return `<option value="${esc(val)}" ${String(val) === String(v) ? "selected" : ""}>${esc(txt)}</option>`;
      }).join("")}</select></div>`;
    if (kind === "date") {   // 달력 팝업 선택 (YYYY-MM-DD 외 옛 값은 빈칸으로 표시됨)
      const dv = esc(/^\d{4}-\d{2}-\d{2}$/.test(v) ? v : "");
      return `<div class="${cls}"><label>${label}</label>
        <input class="datepick" type="text" readonly placeholder="📅 날짜 선택" data-mf="${f}"
          value="${dv}" data-init="${dv}"></div>`;   // data-init: 기준일만 바꾼 것도 저장되게 (변경 판정용)
    }
    return `<div class="${cls}"><label>${label}</label>
      <input data-mf="${f}" value="${esc(v)}" ${f === "stock_set" ? `data-init="${esc(v)}"` : ""}
        ${kind === "num" ? 'inputmode="decimal"' : ""}></div>`;
  }).join("");
  // 제품 수정 시 — 거래처별 판매 단가 (기본 단가와 다른 곳만 입력, 비우면 기본 단가 적용)
  if (type === "product" && row && canM("prod")) renderProdPrices(row.id);
  $("mstOverlay").classList.add("on");
}
/* 거래처별 판매 단가 — 제품 수정 폼 하단에 붙는다. 출고 저장 시 이 단가가 스냅샷으로 기록된다. */
async function renderProdPrices(pid) {
  let d;
  try { d = await api("/api/prodprice/" + pid); } catch (e) { return; }
  if (!document.querySelector("#mstOverlay.on") || !mstEdit || mstEdit.id !== pid) return;   // 그새 닫혔으면 무시
  const box = document.createElement("div");
  box.className = "fld full";
  box.id = "prodPriceBox";
  box.innerHTML = `<label>거래처별 판매 단가 (원) — 비우면 기본 단가 ${NF(d.unit_price || 0)}원 적용</label>
    <div style="max-height:190px; overflow-y:auto; border:1px solid var(--line-soft); border-radius:8px;">
      ${d.partners.map(p => `<div style="display:flex; align-items:center; gap:8px; padding:5px 10px; border-bottom:1px solid var(--line-soft);">
        <span style="flex:1; font-size:12.5px;">${esc(p.name)}</span>
        <input class="mini-input num" data-pprice="${p.id}" value="${p.price != null ? p.price : ""}"
          inputmode="decimal" placeholder="${NF(d.unit_price || 0)}" style="width:100px">
      </div>`).join("") || '<div class="auto" style="padding:12px">판매처로 등록된 거래처가 없습니다</div>'}
    </div>
    <div class="hint" style="margin-top:3px;">출고를 저장하면 그때의 단가가 기록됩니다 — 나중에 단가를 바꿔도 과거 출고 금액은 그대로입니다.</div>`;
  $("mstForm").appendChild(box);
}
window.closeMaster = () => $("mstOverlay").classList.remove("on");
$("mstSave").onclick = async () => {
  // 숫자칸에 숫자로 해석 안 되는 값이 남아 있으면 저장 차단
  const badEl = [...document.querySelectorAll('#mstForm [inputmode="decimal"]')].find(el =>
    el.value.trim() !== "" && isNaN(Number(el.value.replace(/,/g, ""))));
  if (badEl) {
    badEl.classList.add("bad-num");
    badEl.focus();
    return toast("숫자 칸에 잘못된 값이 있습니다 — 숫자만 입력한 뒤 저장하세요");
  }
  const body = {};
  document.querySelectorAll("#mstForm [data-mf]").forEach(el => {
    let v = el.value.trim();
    body[el.dataset.mf] = v === "" ? null : (el.matches('[inputmode="decimal"]') ? Number(v.replace(/,/g, "")) : v);
  });
  if (mstEdit.type === "line" && body.parent_id != null) body.parent_id = +body.parent_id;
  if (mstEdit.type === "users") {
    // 담당 체크박스는 value가 아니라 체크 상태로 모은다 (복수 지정)
    body.duty = [...document.querySelectorAll("#mstForm [data-newduty]")]
      .filter(c => c.checked).map(c => c.dataset.newduty);
    await api("/api/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    closeMaster(); toast("사용자가 등록되었습니다");
    renderUsersTab();
    return;
  }
  if (!body.name) return toast("이름은 필수입니다");
  // 현재고·기준일을 **둘 다** 안 바꿨을 때만 실사 기록을 만들지 않는다.
  // (예전엔 현재고만 비교해서, 기준일만 고치면 저장이 통째로 버려졌다)
  const ss = document.querySelector('#mstForm [data-mf="stock_set"]');
  const sd = document.querySelector('#mstForm [data-mf="stock_date"]');
  if (ss) {
    const stockSame = ss.value.trim() === (ss.dataset.init || "").trim();
    const dateSame = !sd || sd.value.trim() === (sd.dataset.init || "").trim();
    if (stockSame && dateSame) {
      delete body.stock_set;
      delete body.stock_date;
    } else if (stockSame && !dateSame && ss.value.trim() === "") {
      // 현재고가 비어 있는데 날짜만 바꾼 경우 — 기록할 수량이 없다
      return toast("현재고를 입력해야 그 기준일로 기록됩니다");
    }
  }
  const t = mstEdit.type;
  delete body.pack_set;   // 읽기 전용 표시 필드 — 세트는 pack_set_member(전용 팝업)가 정본
  const priceInputs = [...document.querySelectorAll("#mstForm [data-pprice]")];
  let res = null;
  if (mstEdit.id) res = await api(`/api/masters/${t}/${mstEdit.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  else res = await api(`/api/masters/${t}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  // 거래처별 판매 단가 (제품 수정 시에만 폼에 있음)
  if (t === "product" && mstEdit.id && priceInputs.length) {
    const prices = {};
    priceInputs.forEach(i => { prices[i.dataset.pprice] = i.value.trim(); });
    try {
      await api("/api/prodprice/" + mstEdit.id, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prices }) });
    } catch (e) { /* api()가 토스트 */ }
  }
  closeMaster();
  await reloadMaster(t);
  // 과거 날짜로 실사를 기록하면 목록의 '현재고/최종 기록일'은 더 최신 기록을 계속 보여준다 —
  // 저장이 안 된 걸로 오해하기 쉬워 그 사실을 알려준다
  const savedDate = body.stock_date;
  if ((t === "raw" || t === "sub") && body.stock_set != null && savedDate) {
    const m2 = (M[t] || []).find(x => x.id === mstEdit.id);
    if (m2 && m2.stock_date && m2.stock_date > savedDate) {
      toast(`${savedDate} 실사로 저장했습니다 — 목록의 현재고는 더 최신 기록(${m2.stock_date}) 기준이라 그대로입니다`);
    } else toast("저장되었습니다");
  } else toast("저장되었습니다");
  renderMasters();
  // LOT 관리 화면에서 '소비일 입력'으로 열었으면 LOT 현황 갱신
  if (t === "product" && document.querySelector("#scr-lot.on") && LOT.data) loadLot();
  // 일일 입력에서 '➕ 새 자재 등록'으로 진입한 경우 → 그 행에 방금 만든 자재 자동 선택
  if (pendingNewMat && (t === "raw" || t === "sub") && res && res.id) {
    if (pendingNewMat.list === "usage" && E.usage[pendingNewMat.index]) {
      E.usage[pendingNewMat.index].material_id = res.id;
      renderUsage();
    } else if (pendingNewMat.list === "eMat" && E.mat[pendingNewMat.index]) {
      E.mat[pendingNewMat.index].material_id = res.id;
      E.mat[pendingNewMat.index].prev_qty = 0;
      renderMat();
    } else if (pendingNewMat.list === "eMatIn" && E.matIn[pendingNewMat.index]) {
      E.matIn[pendingNewMat.index].material_id = res.id;
      renderMatIn();
    }
    toast("새 자재가 등록되어 해당 행에 선택되었습니다");
  }
  pendingNewMat = null;
};

/* ══ 기록 조회 ═════════════════════════ */
const lkCal = Calendar("lk", d => loadLookup(d));
// 인원·가동 라인 행 클릭 → 공정별 하위 행 펼침/접힘 (위임: lkDetail은 innerHTML 교체돼도 유지됨)
$("lkDetail").addEventListener("click", e => {
  const line = e.target.closest(".lk-line"); if (!line) return;
  const id = line.dataset.lkgrp;
  const open = line.classList.toggle("lk-open");
  $("lkDetail").querySelectorAll(".lk-proc-" + id).forEach(tr => tr.style.display = open ? "" : "none");
  const caret = $("lkDetail").querySelector(`[data-lkcaret="${id}"]`);
  if (caret) caret.textContent = open ? "▾" : "▸";
});
async function loadLookup(date) {
  const d = await api("/api/day/" + date);
  const prodRows = d.production.map(r => `<tr><td><b>${esc(r.name)}</b></td>
    <td class="r">${r.plan_qty ? NF(r.plan_qty) : "—"}</td><td class="r">${NF(r.prod_qty)}</td>
    <td class="r">${NF(r.defect_qty)}${r.defect_reason ? `<div class="auto" style="font-size:10.5px; color:#98670F;">🏷 ${esc(r.defect_reason)}</div>` : ""}</td></tr>`).join("");
  const shipRows = d.shipment.map(r => `<tr><td><b>${esc(r.name)}</b></td>
    <td>${esc(r.partner || "—")}</td><td class="r">${NF(r.qty)}</td>
    <td class="auto">${r.prod_date ? "LOT " + r.prod_date.slice(5) : "—"}</td></tr>`).join("");
  const matRows = d.materials.filter(r => r.used_qty).sort((a, b) => b.used_qty - a.used_qty).slice(0, 15)
    .map(r => `<tr><td><b>${esc(r.name)}</b></td><td class="r">${NF(r.prev_qty)}</td>
      <td class="r">${NF(r.in_qty)}</td><td class="r">${NF(r.real_qty)}</td>
      <td class="r"><button class="uselink num" onclick="openUseLk(${r.material_id},'${date}')">${NF(r.used_qty)}</button></td></tr>`).join("");
  // 원부자재 입고 (유통기한 포함)
  const inRows = (d.mat_in || []).map(r => `<tr><td><b>${esc(r.name)}</b></td>
    <td class="r">${NF(r.qty)} ${esc(r.unit || "")}</td>
    <td class="auto">${r.expiry || "—"}</td><td class="auto">${esc(r.note || "")}</td></tr>`).join("");
  // 인원·가동 — 같은 라인(공정 여럿)은 한 줄로 묶고, 클릭하면 공정별로 펼침(드롭다운)
  const hcOf = r => (Number(r.headcount) || 0) + (Number(r.agency_count) || 0);
  const agencyTag = r => Number(r.agency_count) > 0 ? ` <span class="auto" style="font-size:10.5px">(용역 ${NF(r.agency_count)})</span>` : "";
  const byLk = {};
  (d.staffing || []).forEach(r => { const k = r.line_group || r.line || "—"; (byLk[k] = byLk[k] || []).push(r); });
  let lkGi = 0;
  const stRows = Object.entries(byLk).map(([line, rs]) => {
    if (rs.length === 1) {   // 공정 1개 라인 = 한 줄
      const r = rs[0];
      return `<tr><td><b>${esc(line)}</b>${r.process ? ` <span class="auto" style="font-size:11px">/ ${esc(r.process)}</span>` : ""}</td>
        <td class="r">${NF(hcOf(r))}명${agencyTag(r)}</td>
        <td class="r">${r.work_hours ? NF(r.work_hours) + "h" : "—"}</td>
        <td class="auto">${esc(r.stop_reason || "")}</td></tr>`;
    }
    const id = lkGi++;
    const head = rs.reduce((a, r) => a + hcOf(r), 0);
    const wh = Math.max(...rs.map(r => r.work_hours || 0));
    const stops = rs.map(r => r.stop_reason).filter(Boolean).join(" · ");
    const parent = `<tr class="lk-line" data-lkgrp="${id}" style="cursor:pointer; font-weight:700; background:var(--bg)"
        title="클릭하면 공정별로 펼쳐집니다">
      <td><span class="lk-caret" data-lkcaret="${id}" style="display:inline-block; width:14px; color:var(--muted)">▸</span><b>${esc(line)}</b>
        <span class="auto" style="font-weight:500; font-size:11px">공정 ${rs.length}개</span></td>
      <td class="r">${NF(head)}명</td>
      <td class="r">${wh ? NF(wh) + "h" : "—"}</td>
      <td class="auto" style="font-weight:500">${esc(stops)}</td></tr>`;
    const kids = rs.map(r => `<tr class="lk-proc lk-proc-${id}" style="display:none">
      <td class="auto" style="padding-left:24px">└ ${esc(r.process || "공정 미지정")}</td>
      <td class="r">${NF(hcOf(r))}명${agencyTag(r)}</td>
      <td class="r auto">${r.work_hours ? NF(r.work_hours) + "h" : "—"}</td>
      <td class="auto">${esc(r.stop_reason || "")}</td></tr>`).join("");
    return parent + kids;
  }).join("");
  // 생산 사진 (보기 전용)
  const photoHtml = (d.photos || []).length
    ? `<div class="photo-grid" style="margin-top:4px;">${d.photos.map(p =>
        `<div class="ph-item"><img src="/dayphoto/${encodeURIComponent(p.file)}" alt="생산 사진"
           style="cursor:pointer" onclick="window.open('/dayphoto/${encodeURIComponent(p.file)}','_blank')"></div>`).join("")}</div>`
    : "";
  const sec = (title, html) => html
    ? `<div style="font-size:12px;font-weight:800;color:var(--muted);margin:14px 0 6px;">${title}</div>${html}` : "";
  const tbl = (head, rows2) => `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody class="num">${rows2}</tbody></table></div>`;
  const hasAny = d.production.length || d.shipment.length || matRows || inRows || stRows || (d.photos || []).length || d.memo;
  $("lkDetail").innerHTML = `
    <h2>${date} (${dowOf(date)}) 기록
      ${d.exists ? '<span class="chip ok">저장됨</span>' : '<span class="chip warn">기록 없음</span>'}
      <span class="spacer"></span>
      <button class="btn sm" onclick="gotoEntry('${date}')">✏️ 이 날짜 수정</button></h2>
    ${d.production.length ? sec("🏭 생산실적",
      tbl('<th>제품명</th><th class="r">계획</th><th class="r">생산</th><th class="r">불량 · 사유</th>', prodRows)) : ""}
    ${d.shipment.length ? sec("🚚 완제품 출고",
      tbl('<th>제품명</th><th>거래처</th><th class="r">출고량</th><th>생산LOT</th>', shipRows)) : ""}
    ${sec("📥 원부자재 입고", inRows
      ? tbl('<th>자재명</th><th class="r">입고량</th><th>유통기한</th><th>비고</th>', inRows) : "")}
    ${matRows ? sec("🧾 원부자재 사용 상위 15",
      tbl('<th>자재명</th><th class="r">전일</th><th class="r">입고</th><th class="r">실재고</th><th class="r">사용량</th>', matRows)) : ""}
    ${sec("👷 인원 · 가동", stRows
      ? tbl('<th>라인 / 공정</th><th class="r">인원</th><th class="r">실가동</th><th>정지사유</th>', stRows) : "")}
    ${(d.lots || []).filter(l => l.kind === "stock").length ? sec("📦 재고 LOT 현황 (생산일자별 · 소비기한)",
      tbl('<th>제품명</th><th class="r">수량</th><th class="r">생산일자</th><th class="r">소비기한</th>',
        d.lots.filter(l => l.kind === "stock").map(l => `<tr>
          <td><b>${esc(l.name)}</b></td><td class="r">${NF(l.qty)}</td>
          <td class="r">${l.made_date || "—"}</td><td class="r">${l.expiry || "—"}</td></tr>`).join(""))) : ""}
    ${sec("📷 생산 현장 사진", photoHtml)}
    ${d.memo ? sec("📝 특이사항",
      `<div style="font-size:13px;border:1px solid var(--line);border-radius:8px;padding:9px 12px;">${esc(d.memo)}</div>`) : ""}
    ${!hasAny ? '<div class="auto">이 날짜에는 기록이 없습니다</div>' : ""}`;
}
window.openUseLk = (mid, date) => openUse(mid, date);
window.gotoEntry = (date) => {
  document.querySelector('#nav button[data-scr="entry"]').click();
  entryCal.ym = date.slice(0, 7); entryCal.sel = date; entryCal.render(); loadDay(date);
};

/* ══ 사이드바 '발주 필요' 알림 ══
   팝업 대신 상주형 — 업무 흐름을 막지 않으면서 놓치지 않게.
   자재 담당(stock)·admin에게만 보인다. */
async function loadLowStock() {
  const canSee = ROLE === "admin" || MYDUTY.has("stock");
  const panel = $("lowPanel");
  if (!canSee) { panel.style.display = "none"; return; }
  let d;
  try { d = await api("/api/lowstock"); } catch (e) { return; }
  const items = d.items || [];
  const todo = items.filter(x => !x.ordered);          // 아직 발주 안 한 것 = 진짜 할 일
  if (!items.length && !d.unset) { panel.style.display = "none"; return; }
  panel.style.display = "flex";
  $("lowpCnt").textContent = items.length
    ? (todo.length ? `${todo.length}종` : "발주 완료") : "";
  $("lowpList").innerHTML = items.map(x => `
    <button class="lowp-item ${x.ordered ? "ordered" : ""}" data-lowmid="${x.id}"
      title="${esc(x.name)} — 재고 ${NF(x.stock)} ${esc(x.unit)} / 안전재고 ${NF(x.safety)}${x.ordered ? `\n이미 발주함${x.order_date ? " (" + x.order_date + ")" : ""}` : `\n${NF(x.shortfall)} ${esc(x.unit)} 부족 — 클릭하면 발주량 입력으로 이동`}">
      <span class="nm">${x.ordered ? "✓ " : ""}${esc(x.name)}</span>
      <span class="qt">${x.ordered ? "발주함" : NF(x.shortfall) + " " + esc(x.unit)}</span></button>`).join("")
    || '<div style="font-size:11px; color:var(--side-muted); padding:2px 6px">안전재고 미달 자재 없음</div>';
  // 안전재고 미설정은 판단 기준이 없어 목록에 넣지 않고 안내만 — 설정하면 알림이 정확해진다
  $("lowpNote").innerHTML = d.unset
    ? `재고 0 이하인데 <b>안전재고 미설정</b> ${d.unset}종<br>
       <button data-lowsetup>기준정보에서 설정하기</button>`
    : "";
}
$("lowPanel").addEventListener("click", e => {
  if (e.target.closest("[data-lowsetup]")) {          // 안전재고 설정하러 가기
    document.querySelector('#nav button[data-scr="items"]').click();
    const t = document.querySelector('#itemTabs button[data-mt="raw"]');
    if (t) t.click();
    toast("안전재고를 설정하면 '발주 필요' 알림이 정확해집니다 — [⚡ 빠른 편집]으로 표에서 바로 입력할 수 있습니다");
    return;
  }
  const it = e.target.closest("[data-lowmid]");
  if (!it) return;
  gotoLowMaterial(+it.dataset.lowmid);
});
/** 그 자재의 발주량 입력칸으로 이동 — 없으면 재고 실사에 행을 추가해 준다 */
async function gotoLowMaterial(mid) {
  document.querySelector('#nav button[data-scr="entry"]').click();
  if (!E.date) { entryCal.sel = todayISO(); await loadDay(todayISO()); }
  entryTab = "stock"; renderEntryTabs();
  await new Promise(r => setTimeout(r, 60));
  let i = (E.mat || []).findIndex(r => r.material_id === mid);
  if (i < 0) {   // 실사 행이 없으면 추가 (자동 반영 행이면 그 값을 가져와서)
    const a = (E.autoMat || []).find(r => r.material_id === mid);
    E.mat.push({ material_id: mid, prev_qty: a ? a.prev_qty : (E.prevStock[mid] ?? 0),
      in_qty: a ? a.in_qty : "", real_qty: a ? a.real_qty : "", order_date: "", order_qty: "" });
    i = E.mat.length - 1;
    renderMat();
  }
  const inp = $("eMat").querySelector(`tr[data-i="${i}"] [data-f="order_qty"]`);
  if (inp) {
    inp.scrollIntoView({ block: "center" });
    inp.focus(); inp.select();
    const m = materialById(mid) || {};
    toast(`'${m.name}' 발주량을 입력하세요 — 저장하면 알림에서 '발주함'으로 바뀝니다`);
  }
}
async function doLkSearch() {
  const q = $("lkSearch").value.trim();
  if (!q) return toast("제품명을 입력하세요");
  const frm = $("lkFrom").value, to = $("lkTo").value;
  if (frm && to && frm > to) return toast("시작일이 종료일보다 늦습니다");
  const d = await api(`/api/search?q=${encodeURIComponent(q)}&frm=${frm}&to=${to}`);
  if (!d.products.length) { $("lkSearchOut").innerHTML = '<div class="auto">검색 결과 없음</div>'; return; }
  const label = frm || to
    ? `${frm || "처음"} ~ ${to || "오늘"} · ${d.history.length}건`
    : `최근 ${d.history.length}건`;
  const tp = d.history.reduce((s, h) => s + h.prod, 0);
  const ts = d.history.reduce((s, h) => s + h.ship, 0);
  $("lkSearchOut").innerHTML = `
    <div style="font-size:12px; font-weight:700; margin-bottom:6px; display:flex; align-items:center; gap:6px;">${esc(d.products[0].name)}
      <span style="font-weight:500;color:var(--muted)">${label}</span>
      <button class="btn ghost sm" style="margin-left:auto" onclick="lkCsv('${esc(d.products[0].name)}')" title="검색 결과를 CSV(엑셀)로 저장">📄 CSV</button></div>
    <div class="tbl-wrap" style="max-height:340px; overflow-y:auto;"><table id="lkResTbl">
    <thead><tr><th>날짜</th><th class="r">생산</th><th class="r">출고</th></tr></thead>
    <tbody class="num">${d.history.map(h => `<tr style="cursor:pointer" onclick="lkPick('${h.date}')">
      <td>${h.date.slice(2)}</td><td class="r">${h.prod ? NF(h.prod) : "·"}</td>
      <td class="r">${h.ship ? NF(h.ship) : "·"}</td></tr>`).join("")
      || '<tr><td colspan="3" class="auto">이 기간 기록 없음</td></tr>'}</tbody>
    ${d.history.length ? `<tfoot><tr style="font-weight:700; background:var(--bg);">
      <td>합계</td><td class="r num">${NF(tp)}</td><td class="r num">${NF(ts)}</td></tr></tfoot>` : ""}
    </table></div>`;
}
window.lkCsv = (label) => {
  const t = $("lkResTbl");
  if (!t) return toast("내보낼 검색 결과가 없습니다");
  tableToCsv(t.tHead, t.tBodies[0], csvName("기록조회", label));
};
// [전체] — 제품명 없이 기간 내 모든 제품의 생산·출고 기록
async function doLkSearchAll() {
  const frm = $("lkFrom").value, to = $("lkTo").value;
  if (frm && to && frm > to) return toast("시작일이 종료일보다 늦습니다");
  const d = await api(`/api/searchall?frm=${frm}&to=${to}`);
  const label = frm || to ? `${frm || "처음"} ~ ${to || "오늘"} · ${d.rows.length}건` : `최근 ${d.rows.length}건`;
  const tp = d.rows.reduce((s, h) => s + h.prod, 0), ts = d.rows.reduce((s, h) => s + h.ship, 0);
  $("lkSearchOut").innerHTML = `
    <div style="font-size:12px; font-weight:700; margin-bottom:6px; display:flex; align-items:center; gap:6px;">전체 기록
      <span style="font-weight:500;color:var(--muted)">${label}</span>
      <button class="btn ghost sm" style="margin-left:auto" onclick="lkCsv('전체기록')" title="검색 결과를 CSV(엑셀)로 저장">📄 CSV</button></div>
    <div class="tbl-wrap" style="max-height:340px; overflow-y:auto;"><table id="lkResTbl">
    <thead><tr><th>날짜</th><th>제품</th><th class="r">생산</th><th class="r">출고</th></tr></thead>
    <tbody class="num">${d.rows.map(h => `<tr style="cursor:pointer" onclick="lkPick('${h.date}')">
      <td>${h.date.slice(2)}</td><td>${esc(h.name)}</td><td class="r">${h.prod ? NF(h.prod) : "·"}</td>
      <td class="r">${h.ship ? NF(h.ship) : "·"}</td></tr>`).join("")
      || '<tr><td colspan="4" class="auto">이 기간 기록 없음</td></tr>'}</tbody>
    ${d.rows.length ? `<tfoot><tr style="font-weight:700; background:var(--bg);">
      <td>합계</td><td></td><td class="r num">${NF(tp)}</td><td class="r num">${NF(ts)}</td></tr></tfoot>` : ""}
    </table></div>`;
}
$("lkSearch").addEventListener("keydown", e => { if (e.key === "Enter") doLkSearch(); });
$("lkGo").onclick = doLkSearch;
$("lkAll").onclick = doLkSearchAll;
$("lkFrom").addEventListener("change", () => { if ($("lkSearch").value.trim()) doLkSearch(); });
$("lkTo").addEventListener("change", () => { if ($("lkSearch").value.trim()) doLkSearch(); });
window.lkPick = (date) => { lkCal.ym = date.slice(0, 7); lkCal.sel = date; lkCal.render(); loadLookup(date); };

/* ══ 분석 (martin_data 대시보드 이식) ═══ */
const ANA = { raw: null, dates: [], P: {}, S: {}, stock: {}, from: null, to: null,
  agg: "d", metric: "stock", prods: [], deadThr: 7, trendSel: -1 };
const PALETTE = ["#121212", "#C2372C", "#3E7A50", "#C4841D", "#4A5FA5", "#8E44AD", "#0E7490", "#6E6A63"];

async function loadAna() {
  if (!ANA.raw) {
    const raw = await api("/api/analytics");
    ANA.raw = raw;
    const ds = new Set();
    raw.prod.forEach(r => ds.add(r.date)); raw.ship.forEach(r => ds.add(r.date));
    (raw.disp || []).forEach(r => ds.add(r.date));   // 폐기만 있는 날도 재고 계산에 포함
    ANA.dates = [...ds].sort();
    raw.prod.forEach(r => { (ANA.P[r.pid] = ANA.P[r.pid] || {})[r.date] = r.p; });
    raw.ship.forEach(r => { (ANA.S[r.pid] = ANA.S[r.pid] || {})[r.date] = r.s; });
    ANA.D = {};
    (raw.disp || []).forEach(r => { (ANA.D[r.pid] = ANA.D[r.pid] || {})[r.date] = r.q; });
    ANA.DF = {};   // 불량: pid → date → qty
    (raw.defect || []).forEach(r => { (ANA.DF[r.pid] = ANA.DF[r.pid] || {})[r.date] = r.d; });
    raw.products.forEach(p => {
      let acc = p.opening;
      ANA.stock[p.id] = ANA.dates.map(d => {
        acc += (ANA.P[p.id]?.[d] || 0) - (ANA.S[p.id]?.[d] || 0) - (ANA.D[p.id]?.[d] || 0); return acc; });
    });
    ANA.from = ANA.dates[0]; ANA.to = ANA.dates[ANA.dates.length - 1];
    $("anaFrom").value = ANA.from; $("anaTo").value = ANA.to;
  }
  renderAna();
  loadCosts();   // 원가·수익성 (admin — 기간과 무관, 1회 캐시)
}

function anaRange() {
  const i0 = ANA.dates.findIndex(d => d >= ANA.from);
  let i1 = ANA.dates.length - 1;
  for (let i = ANA.dates.length - 1; i >= 0; i--) if (ANA.dates[i] <= ANA.to) { i1 = i; break; }
  return [Math.max(i0, 0), i1];
}
function bucketKey(d) {
  if (ANA.agg === "d") return d;
  if (ANA.agg === "m") return d.slice(0, 7);
  const dt = new Date(d + "T00:00:00");
  const mon = new Date(dt); mon.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
  return fmtISO(mon);
}
function bucketLabel(k) {
  if (ANA.agg === "d") return k.slice(5).replace("-", "/");
  if (ANA.agg === "m") return Number(k.slice(5)) + "월";
  return k.slice(5).replace("-", "/") + "주";
}

function renderAna() {
  const [i0, i1] = anaRange();
  const idxs = []; for (let i = i0; i <= i1; i++) idxs.push(i);
  const pids = ANA.raw.products.map(p => p.id);
  const dayProd = idxs.map(i => pids.reduce((s, id) => s + (ANA.P[id]?.[ANA.dates[i]] || 0), 0));
  const dayShip = idxs.map(i => pids.reduce((s, id) => s + (ANA.S[id]?.[ANA.dates[i]] || 0), 0));
  const dayStock = idxs.map(i => pids.reduce((s, id) => s + ANA.stock[id][i], 0));
  const dayTypes = idxs.map(i => pids.filter(id => (ANA.P[id]?.[ANA.dates[i]] || 0) > 0).length);

  // KPI
  const totP = dayProd.reduce((a, b) => a + b, 0), totS = dayShip.reduce((a, b) => a + b, 0);
  const endStock = dayStock[dayStock.length - 1] || 0;
  const oos = pids.filter(id => ANA.stock[id][i1] <= 0 &&
    (Object.keys(ANA.P[id] || {}).length || Object.keys(ANA.S[id] || {}).length)).length;
  let maxPi = 0; dayProd.forEach((v, i) => { if (v > dayProd[maxPi]) maxPi = i; });
  let maxTi = 0; dayTypes.forEach((v, i) => { if (v > dayTypes[maxTi]) maxTi = i; });
  const kpi = (l, v, s) => `<div class="kpi"><div class="lbl">${l}</div>
    <div class="val num" style="font-size:20px">${v}</div><div class="delta num">${s || ""}</div></div>`;
  $("anaKpis").innerHTML =
    kpi("기간 생산량", NF(totP), `${idxs.length}일 기록`) +
    kpi("기간 출고량", NF(totS)) +
    kpi("기말 재고량", NF(endStock), ANA.dates[i1]) +
    kpi("결품", oos + "종", "기말 재고 ≤ 0") +
    kpi("일 최대 생산", NF(dayProd[maxPi] || 0), ANA.dates[i0 + maxPi]) +
    kpi("최다 품목 생산일", (dayTypes[maxTi] || 0) + "종", ANA.dates[i0 + maxTi]);

  // 추이 (집계 버킷)
  const bk = [], bmap = {};
  idxs.forEach((gi, k) => {
    const key = bucketKey(ANA.dates[gi]);
    if (!(key in bmap)) { bmap[key] = bk.length; bk.push({ key, p: 0, s: 0, st: 0, last: gi }); }
    const b = bk[bmap[key]];
    b.p += dayProd[k]; b.s += dayShip[k]; b.st = dayStock[k]; b.last = gi;
  });
  lineChart($("anaTrend"), {
    labels: bk.map(b => bucketLabel(b.key)),
    series: [
      { name: "재고", color: "#C2372C", values: bk.map(b => b.st), dash: "5 3" },
      { name: "출고", color: "#B0ADA6", values: bk.map(b => b.s) },
      { name: "생산", color: "#121212", values: bk.map(b => b.p), fill: "rgba(18,18,18,.05)" },
    ],
    zoom: true,
    onClick: i => { $("anaTrendD").innerHTML =
      `<b>${bk[i].key}</b> — 생산 <b>${NF(bk[i].p)}개</b> · 출고 <b>${NF(bk[i].s)}개</b> · 재고 <b>${NF(bk[i].st)}개</b>`; },
  });

  // 요일별 평균 (일별 데이터 기준)
  const dowSum = Array.from({ length: 7 }, () => ({ p: 0, s: 0, n: 0 }));
  idxs.forEach((gi, k) => {
    const w = new Date(ANA.dates[gi] + "T00:00:00").getDay();
    dowSum[w].p += dayProd[k]; dowSum[w].s += dayShip[k]; dowSum[w].n++;
  });
  drawDow($("anaDow"), dowSum);

  // 월별 집계
  const mm = {}; const morder = [];
  idxs.forEach((gi, k) => {
    const key = ANA.dates[gi].slice(0, 7);
    if (!(key in mm)) { mm[key] = { p: 0, s: 0, st: 0 }; morder.push(key); }
    mm[key].p += dayProd[k]; mm[key].s += dayShip[k]; mm[key].st = dayStock[k];
  });
  $("anaMonthly").innerHTML = morder.map(k => `<tr><td><b>${k}</b></td>
    <td class="r">${NF(mm[k].p)}</td><td class="r">${NF(mm[k].s)}</td><td class="r">${NF(mm[k].st)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="auto">데이터 없음</td></tr>`;

  renderAnaTop(idxs);
  renderAnaDefect(idxs);
  renderRotation(idxs, i1);
}

/* 제품 TOP — 선택 기간 생산/출고 상위 10 (가로 막대 랭킹) */
function renderAnaTop(idxs) {
  const key = ANA.topMode || "prod";
  const src = key === "prod" ? ANA.P : ANA.S;
  const rows = ANA.raw.products.map(p => ({
    p, sum: idxs.reduce((s, gi) => s + (src[p.id]?.[ANA.dates[gi]] || 0), 0),
  })).filter(r => r.sum > 0).sort((a, b) => b.sum - a.sum).slice(0, 10);
  const max = rows.length ? rows[0].sum : 1;
  const medals = ["🥇", "🥈", "🥉"];
  $("anaTop").innerHTML = rows.length ? rows.map((r, i) => `
    <div class="rank-row">
      <span class="rk ${i < 3 ? "top" : ""}">${medals[i] || (i + 1)}</span>
      <div style="flex:1; min-width:0">
        <div class="rname">${esc(r.p.name)}</div>
        <div class="rbar" style="width:${Math.max(6, Math.round(r.sum / max * 100))}%"></div>
      </div>
      <span class="rqty num">${NF(r.sum)}개</span>
    </div>`).join("")
    : `<div class="auto">기간 내 ${key === "prod" ? "생산" : "출고"} 기록이 없습니다</div>`;
}
$("anaTopTabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-top]"); if (!b) return;
  document.querySelectorAll("#anaTopTabs button").forEach(x => x.classList.toggle("on", x === b));
  ANA.topMode = b.dataset.top;
  renderAna();
});

/* 불량 분석 — 기간 불량량·불량률 + 사유별 집계 */
function renderAnaDefect(idxs) {
  const from = ANA.dates[idxs[0]], to = ANA.dates[idxs[idxs.length - 1]] || from;
  const pids = ANA.raw.products.map(p => p.id);
  const totProd = idxs.reduce((s, gi) => s + pids.reduce((s2, id) => s2 + (ANA.P[id]?.[ANA.dates[gi]] || 0), 0), 0);
  const totDef = idxs.reduce((s, gi) => s + pids.reduce((s2, id) => s2 + (ANA.DF[id]?.[ANA.dates[gi]] || 0), 0), 0);
  const rate = totProd > 0 ? (totDef / totProd * 100) : 0;
  $("anaDefKpi").innerHTML = [
    ["🚫 기간 불량", NF(totDef) + "개", totDef > 0 ? "var(--crit)" : ""],
    ["📉 불량률", rate.toFixed(2) + "%", rate > 1 ? "var(--crit)" : ""],
    ["🍩 기간 생산", NF(totProd) + "개", ""],
  ].map(([t, v, c]) => `<span class="pchip num" style="${c ? "color:" + c + "; border-color:" + c : ""}">${t} <b style="margin-left:4px">${v}</b></span>`).join("");
  // 사유별 집계 (기간 필터)
  const byReason = {};
  (ANA.raw.reasons || []).forEach(r => {
    if (r.date < from || r.date > to) return;
    byReason[r.reason] = (byReason[r.reason] || 0) + r.q;
  });
  const list = Object.entries(byReason).sort((a, b) => b[1] - a[1]);
  const tot = list.reduce((s, [, q]) => s + q, 0) || 1;
  $("anaDefReasons").innerHTML = list.length ? list.map(([reason, q]) => `<tr>
      <td>${reason === "사유 미입력" ? '<span class="auto">사유 미입력</span>' : "🏷 " + esc(reason)}</td>
      <td class="r">${NF(q)}</td>
      <td class="r">${Math.round(q / tot * 100)}%</td></tr>`).join("")
    : '<tr><td colspan="3" class="auto">기간 내 불량 기록이 없습니다 — 일일 입력의 불량 수량·사유가 여기 집계됩니다</td></tr>';
}
$("anaRotCsv").onclick = () => tableToCsv($("anaRotHead"), $("anaRotation"),
  csvName("재고회전", `${ANA.from}_${ANA.to}`));

/* 원가·수익성 (admin) — 배합비×자재단가 + 개당 노무비. 제품 클릭 = 자재별 구성 */
let COSTS = null;
async function loadCosts() {
  if (!canM("cost")) { $("anaCostCard").style.display = "none"; return; }
  $("anaCostCard").style.display = "";
  if (!COSTS) COSTS = await api("/api/costs");
  const d = COSTS;
  $("anaCostSub").textContent = `노무비 배분: 최근 30일 ₩${NF(d.labor_total)} ÷ 양품 ${NF(d.good_total)}개 = 개당 ₩${NF(d.labor_rate)}`;
  // 신뢰도 경고: 단가 미입력 자재가 있으면 원가가 실제보다 낮게 계산됨
  const missTotal = d.rows.reduce((s, r) => s + r.missing, 0);
  $("anaCostWarn").innerHTML = (missTotal || d.no_bom)
    ? `<div class="mh-wrap" style="margin-bottom:10px;">📋 ${missTotal ? `<b>단가 미입력 자재 사용 ${missTotal}건</b> — 해당 제품 원가는 실제보다 낮게 계산됩니다 (기준정보 › 원/부재료에서 단가 입력)` : ""}
       ${d.no_bom ? ` · 배합비 없는 제품 ${d.no_bom}종은 제외` : ""}</div>` : "";
  const rows2 = [...d.rows].sort((a, b) => {
    const am = a.sell > 0 ? (a.sell - a.mat_cost - d.labor_rate) / a.sell : 9e9;
    const bm = b.sell > 0 ? (b.sell - b.mat_cost - d.labor_rate) / b.sell : 9e9;
    return am - bm;   // 마진율 낮은(문제) 제품부터 · 판매가 미입력은 뒤로
  });
  $("anaCostBody").innerHTML = rows2.map(r => {
    const cost = r.mat_cost + d.labor_rate;
    const margin = r.sell > 0 ? r.sell - cost : null;
    const rate = r.sell > 0 ? margin / r.sell * 100 : null;
    const color = rate == null ? "" : rate < 0 ? "var(--crit)" : rate < 20 ? "#B45309" : "var(--ok)";
    return `<tr>
      <td><button class="uselink" data-cost="${r.id}" style="display:inline-flex; align-items:center; gap:7px;">
        ${r.image ? `<img src="/image/${encodeURIComponent(r.image)}" style="width:26px; height:26px; object-fit:cover; border-radius:5px; border:1px solid var(--line)">` : ""}
        <b>${esc(r.name)}</b></button>
        ${r.missing ? `<span class="chip warn" title="단가 미입력 자재 ${r.missing}종 — 원가가 실제보다 낮음">단가 ${r.missing}종 빠짐</span>` : ""}</td>
      <td class="r">${r.sell > 0 ? NF(r.sell) : '<span class="auto">미입력</span>'}</td>
      <td class="r">${NF(Math.round(r.mat_cost * 10) / 10)}</td>
      <td class="r">${NF(d.labor_rate)}</td>
      <td class="r" style="font-weight:700">${NF(Math.round(cost * 10) / 10)}</td>
      <td class="r" style="color:${color}">${margin == null ? "—" : NF(Math.round(margin * 10) / 10)}</td>
      <td class="r" style="color:${color}; font-weight:800">${rate == null ? "—" : rate.toFixed(1) + "%"}</td></tr>`;
  }).join("") || '<tr><td colspan="7" class="auto">배합비가 등록된 제품이 없습니다</td></tr>';
}
$("anaCostBody").addEventListener("click", e => {
  const b = e.target.closest("[data-cost]"); if (!b || !COSTS) return;
  const r = COSTS.rows.find(x => x.id === +b.dataset.cost); if (!r) return;
  $("costTitle").textContent = `${r.name} — 원가 구성`;
  const cost = r.mat_cost + COSTS.labor_rate;
  $("costHint").textContent = `판매가 ${r.sell > 0 ? "₩" + NF(r.sell) : "미입력"} · 원가 ₩${NF(Math.round(cost * 10) / 10)}`
    + (r.sell > 0 ? ` · 마진 ₩${NF(Math.round((r.sell - cost) * 10) / 10)}` : "");
  $("costBody").innerHTML = r.detail.map(m => `<tr ${m.price <= 0 ? 'style="color:var(--warn)"' : ""}>
      <td>${esc(m.name)}${m.price <= 0 ? ' <span class="chip warn">단가 미입력</span>' : ""}</td>
      <td class="r">${NF(m.qty)} ${esc(m.unit)}</td>
      <td class="r">${m.price > 0 ? NF(m.price) : "—"}</td>
      <td class="r">${m.cost > 0 ? NF(m.cost) : "—"}</td></tr>`).join("")
    + `<tr style="font-weight:700; background:var(--bg)"><td>자재비 합계</td><td></td><td></td><td class="r">${NF(Math.round(r.mat_cost * 10) / 10)}</td></tr>
       <tr style="font-weight:700"><td>노무비 (개당 배분)</td><td></td><td></td><td class="r">${NF(COSTS.labor_rate)}</td></tr>
       <tr style="font-weight:800; background:var(--bg)"><td>원가 합계</td><td></td><td></td><td class="r">${NF(Math.round(cost * 10) / 10)}</td></tr>`;
  $("costOverlay").classList.add("on");
});
window.closeCost = () => $("costOverlay").classList.remove("on");
$("anaCostCsv").onclick = () => tableToCsv($("anaCostHead"), $("anaCostBody"), csvName("원가수익성", todayISO()));

function drawDow(el, dowSum) {
  const avg = dowSum.map(d => ({ p: d.n ? d.p / d.n : 0, s: d.n ? d.s / d.n : 0 }));
  mkChart(el, {
    type: "bar",
    data: { labels: DOW.map(d => d + "요일"), datasets: [
      { label: "평균 생산", data: avg.map(a => Math.round(a.p)),
        backgroundColor: "#121212", borderRadius: 4, maxBarThickness: 22 },
      { label: "평균 출고", data: avg.map(a => Math.round(a.s)),
        backgroundColor: "#B0ADA6", borderRadius: 4, maxBarThickness: 22 },
    ]},
    options: baseOptions({}, 2),
  });
}

function renderRotation(idxs, i1) {
  const days = idxs.length || 1;
  const rows = ANA.raw.products.map(p => {
    const cur = ANA.stock[p.id][i1];
    const prodSum = idxs.reduce((s, gi) => s + (ANA.P[p.id]?.[ANA.dates[gi]] || 0), 0);
    const shipSum = idxs.reduce((s, gi) => s + (ANA.S[p.id]?.[ANA.dates[gi]] || 0), 0);
    const hasAct = Object.keys(ANA.P[p.id] || {}).length || Object.keys(ANA.S[p.id] || {}).length;
    if (!hasAct) return null;
    const avg = shipSum / days;
    const doi = avg > 0 ? cur / avg : (cur > 0 ? Infinity : 0);
    const shipDates = Object.entries(ANA.S[p.id] || {}).filter(([, v]) => v > 0).map(([d]) => d).sort();
    const lastShip = shipDates[shipDates.length - 1] || null;
    const elapsed = lastShip ? Math.round((new Date(ANA.to) - new Date(lastShip)) / 86400000) : null;
    return { p, cur, prodSum, shipSum, avg, doi, lastShip, elapsed };
  }).filter(Boolean).filter(r => r.cur > 0 || r.shipSum > 0)
    .sort((a, b) => (b.doi === Infinity ? 1e15 : b.doi) - (a.doi === Infinity ? 1e15 : a.doi));
  $("anaRotation").innerHTML = rows.map(r => {
    const dead = r.cur > 0 && (r.elapsed == null || r.elapsed >= ANA.deadThr);
    return `<tr ${dead ? 'style="color:var(--crit)"' : ""}>
      <td><button class="uselink" data-pdetail="${r.p.id}">${esc(r.p.name)}</button></td>
      <td class="r">${NF(r.cur)}</td><td class="r">${NF(r.prodSum)}</td><td class="r">${NF(r.shipSum)}</td>
      <td class="r">${NF(Math.round(r.avg))}</td>
      <td class="r">${r.doi === Infinity ? "∞" : NF(Math.round(r.doi * 10) / 10) + "일"}</td>
      <td class="r">${r.lastShip || "없음"}</td>
      <td class="r">${r.elapsed == null ? "—" : r.elapsed + "일"}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="auto">데이터 없음</td></tr>`;
  applyRotFilter();
}
/* 재고회전 표 제품 검색 (표시 필터) */
function applyRotFilter() {
  const q = ($("anaRotFilter").value || "").trim().toLowerCase();
  document.querySelectorAll("#anaRotation tr").forEach(tr => {
    const name = tr.cells[0] ? tr.cells[0].textContent.toLowerCase() : "";
    tr.style.display = !q || name.includes(q) ? "" : "none";
  });
}
$("anaRotFilter").addEventListener("input", applyRotFilter);

/* 분석 이벤트 */
$("anaFrom").addEventListener("change", e => { ANA.from = e.target.value; renderAna(); });
$("anaTo").addEventListener("change", e => { ANA.to = e.target.value; renderAna(); });
$("anaAll").onclick = () => {
  ANA.from = ANA.dates[0]; ANA.to = ANA.dates[ANA.dates.length - 1];
  $("anaFrom").value = ANA.from; $("anaTo").value = ANA.to; renderAna();
};
$("anaAgg").addEventListener("click", e => {
  const b = e.target.closest("button[data-agg]"); if (!b) return;
  document.querySelectorAll("#anaAgg button").forEach(x => x.classList.toggle("on", x === b));
  ANA.agg = b.dataset.agg; renderAna();
});
$("anaDeadThr").addEventListener("change", e => { ANA.deadThr = +e.target.value; renderAna(); });
$("anaRotation").addEventListener("click", e => {
  const b = e.target.closest("[data-pdetail]"); if (!b) return;
  openAnaP(+b.dataset.pdetail);
});

/* 제품 상세 모달 */
function openAnaP(pid) {
  const p = ANA.raw.products.find(x => x.id === pid);
  const [i0, i1] = anaRange();
  let tp = 0, ts = 0;
  for (let i = i0; i <= i1; i++) {
    tp += ANA.P[pid]?.[ANA.dates[i]] || 0;
    ts += ANA.S[pid]?.[ANA.dates[i]] || 0;
  }
  const acts = [];
  for (let i = i1; i >= 0 && acts.length < 12; i--) {
    const d = ANA.dates[i];
    const pr = ANA.P[pid]?.[d] || 0, sh = ANA.S[pid]?.[d] || 0;
    if (pr || sh) acts.push({ d, pr, sh, st: ANA.stock[pid][i] });
  }
  // 추이 버킷 (현재 기간·집계 단위)
  const bkP = [], bmapP = {};
  for (let i = i0; i <= i1; i++) {
    const key = bucketKey(ANA.dates[i]);
    if (!(key in bmapP)) { bmapP[key] = bkP.length; bkP.push({ key, p: 0, s: 0, st: 0 }); }
    const b = bkP[bmapP[key]];
    b.p += ANA.P[pid]?.[ANA.dates[i]] || 0;
    b.s += ANA.S[pid]?.[ANA.dates[i]] || 0;
    b.st = ANA.stock[pid][i];
  }
  $("anaPTitle").textContent = p.name;
  $("anaPHint").textContent = `${p.category || "카테고리 미지정"} · 기간 ${ANA.from} ~ ${ANA.to}`;
  $("anaPBody").innerHTML = `
    <div class="kpis" style="grid-template-columns:repeat(3,1fr); margin-bottom:12px;">
      <div class="kpi"><div class="lbl">기간 생산</div><div class="val num" style="font-size:19px">${NF(tp)}</div></div>
      <div class="kpi"><div class="lbl">기간 출고</div><div class="val num" style="font-size:19px">${NF(ts)}</div></div>
      <div class="kpi"><div class="lbl">현재고</div><div class="val num" style="font-size:19px">${NF(ANA.stock[pid][i1])}</div></div>
    </div>
    <div style="font-size:12px; font-weight:800; color:var(--muted); margin-bottom:4px;">추이</div>
    <div class="legend" style="margin-bottom:4px;">
      <span><i style="background:#121212"></i>생산</span>
      <span><i style="background:#B0ADA6"></i>출고</span>
      <span><i style="background:#C2372C"></i>재고</span>
    </div>
    <div class="chartbox" style="height:200px"><canvas id="anaPChart"></canvas></div>
    <div style="font-size:12px; font-weight:800; color:var(--muted); margin-bottom:6px;">최근 활동 12건</div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>날짜</th><th class="r">생산</th><th class="r">출고</th><th class="r">재고</th></tr></thead>
      <tbody class="num">${acts.map(a => `<tr><td>${a.d}</td>
        <td class="r">${a.pr ? "+" + NF(a.pr) : "·"}</td>
        <td class="r">${a.sh ? "−" + NF(a.sh) : "·"}</td>
        <td class="r">${NF(a.st)}</td></tr>`).join("")}</tbody>
    </table></div>`;
  lineChart($("anaPChart"), {
    labels: bkP.map(b => bucketLabel(b.key)),
    series: [
      { color: "#C2372C", values: bkP.map(b => b.st), dash: "5 3" },
      { color: "#B0ADA6", values: bkP.map(b => b.s) },
      { color: "#121212", values: bkP.map(b => b.p), fill: "rgba(18,18,18,.05)" },
    ],
    tipHtml: i => `<span class="tt">${bkP[i].key}</span>생산 <b>${NF(bkP[i].p)}</b><br>출고 <b>${NF(bkP[i].s)}</b><br>재고 <b>${NF(bkP[i].st)}</b>`,
  });
  $("anaOverlay").classList.add("on");
}
window.closeAnaP = () => $("anaOverlay").classList.remove("on");

/* 제품 이력 팝업 (기준정보 제품명 클릭): 생산일자별 현재고 LOT + 최근 생산/출고 */
async function openProdHistory(pid) {
  const d = await api("/api/prodhistory/" + pid);
  $("anaPTitle").textContent = d.name;
  $("anaPHint").textContent =
    `${d.category || "—"}${d.spec ? " · " + esc(d.spec) : ""} · 현재고 ${NF(d.stock)}` +
    ` · 마지막 생산 ${d.last_prod || "기록 없음"} · 마지막 출고 ${d.last_ship || "기록 없음"}` +
    ` · 누적 생산 ${NF(d.total_prod)} / 누적 출고 ${NF(d.total_ship)}`;
  const now = Date.now();
  const canLot = canLotManage();
  const lotRows = d.lots.map(l => {
    const days = l.made ? Math.floor((now - new Date(l.made).getTime()) / 864e5) : null;
    const dleft = l.expiry ? Math.ceil((new Date(l.expiry).getTime() - now) / 864e5) : null;
    const expCell = l.planned
      ? `<span>${l.expiry || "—"}</span> <span class="auto" style="font-size:10.5px">(생산실적 분할)</span>`
      : canLot
      ? `<input class="mini-input datepick" type="text" readonly placeholder="📅 소비기한" data-plexp data-pid="${pid}" data-made="${esc(l.made)}"
           value="${esc(l.expiry || "")}" style="width:135px" title="이 LOT(생산일)의 소비기한 — 생산일마다 다르게 지정 가능">`
        + (dleft != null ? ` <span class="num" style="${dleft < 0 ? "color:var(--crit); font-weight:700" : dleft <= 7 ? "color:#B45309; font-weight:700" : ""}">${dleft >= 0 ? "D-" + dleft : "만료 +" + (-dleft) + "일"}</span>` : "")
      : `<span class="auto" ${dleft != null && dleft < 0 ? 'style="color:var(--crit); font-weight:700"' : ""}>${l.expiry
          ? l.expiry + (dleft != null ? ` (${dleft >= 0 ? "D-" + dleft : "만료 " + (-dleft) + "일"})` : "")
          : "—"}</span>`;
    return `<tr>
      <td>${l.made || "생산일 미상 (이월)"}</td>
      <td class="r" style="font-weight:700">${NF(l.qty)}</td>
      <td class="r">${days != null ? days + "일" : "—"}</td>
      <td>${expCell}</td></tr>`;
  }).join("") || '<tr><td colspan="4" class="auto">재고 LOT 없음</td></tr>';
  const lotTotal = d.lots.reduce((s, l) => s + l.qty, 0);
  const totalRow = d.lots.length ? `<tr style="font-weight:800; background:var(--bg);">
      <td>합계 (전체 수량)</td><td class="r">${NF(Math.round(lotTotal * 1000) / 1000)}</td>
      <td></td><td class="auto" style="font-weight:400">LOT ${d.lots.length}개</td></tr>` : "";
  const canEdit = ROLE !== "guest";
  const imgBlock = `<div style="display:flex; gap:14px; align-items:flex-start; margin-bottom:14px;">
    <div style="flex:none; width:120px; height:120px; border:1px solid var(--line); border-radius:10px; overflow:hidden; display:flex; align-items:center; justify-content:center; background:var(--bg);">
      ${d.image ? `<img src="/image/${encodeURIComponent(d.image)}?t=${now}" style="max-width:100%; max-height:100%; object-fit:contain;">` : '<span class="auto" style="font-size:11px">이미지 없음</span>'}
    </div>
    ${canEdit ? `<div style="display:flex; flex-direction:column; gap:6px;">
      <button class="btn sm" id="prodImgBtn">📷 이미지 ${d.image ? "변경" : "추가"}</button>
      ${d.image ? `<button class="btn ghost sm" id="prodImgDel" style="color:var(--crit)">이미지 삭제</button>` : ""}
      <span class="auto" style="font-size:11px; max-width:150px; line-height:1.5;">png·jpg·webp·gif (8MB↓)<br>Image 폴더에 제품명으로 저장</span>
    </div>` : ""}
  </div>`;
  $("anaPBody").innerHTML = imgBlock + `
    <h3 style="font-size:12.5px; margin:2px 0 6px;">📦 생산일자별 현재고 (LOT)</h3>
    <div class="tbl-wrap"><table>
      <thead><tr><th>생산일자</th><th class="r">수량</th><th class="r">보관일수</th><th>소비기한</th></tr></thead>
      <tbody class="num">${lotRows}${totalRow}</tbody></table></div>
    <p class="hint" style="margin:4px 0 12px;">${d.lot_base
      ? `수불부 LOT 스냅샷(${d.lot_base}) 기준 + 이후 생산·출고 반영 추정`
      : "생산·출고 기록 기반 추정"}${canLot ? " · 소비기한은 LOT(생산일)마다 개별 지정 — 비우면 " : " · "}${d.shelf_days ? `소비일 ${d.shelf_days}일 자동 계산` : "소비일 미등록 (제품 수정에서 입력하면 자동 계산)"}</p>
    <h3 style="font-size:12.5px; margin:2px 0 6px;">🏭 최근 생산·출고 (${d.recent.length}일)</h3>
    <div class="tbl-wrap"><table>
      <thead><tr><th>날짜</th><th class="r">생산</th><th class="r">출고</th><th>출고처</th></tr></thead>
      <tbody class="num">${d.recent.map(r => `<tr ${r.prod > 0 ? 'style="background:var(--ok-soft)"' : ""}>
        <td>${r.date}</td>
        <td class="r" ${r.prod > 0 ? 'style="color:var(--ok); font-weight:700"' : ""}>${r.prod ? "+" + NF(r.prod) : "·"}</td>
        <td class="r">${r.ship ? NF(r.ship) : "·"}</td>
        <td class="auto" style="font-size:11.5px">${r.ship ? esc(r.partners || "거래처 미상") : ""}</td></tr>`).join("")
        || '<tr><td colspan="4" class="auto">기록 없음</td></tr>'}</tbody></table></div>`;
  // 이미지 버튼 연결
  prodImgPid = pid;
  const bImg = $("prodImgBtn");
  if (bImg) bImg.onclick = () => { $("prodImgInput").click(); };
  const bDel = $("prodImgDel");
  if (bDel) bDel.onclick = async () => {
    if (!confirm("이 제품 이미지를 삭제할까요?")) return;
    await api(`/api/product/${pid}/image`, { method: "DELETE" });
    toast("이미지 삭제됨");
    await reloadMaster("product");
    openProdHistory(pid);
    if (mTab === "product" && document.querySelector("#scr-items.on")) renderMasters();
  };
  $("anaOverlay").classList.add("on");
}
// 파일 선택 → base64로 업로드 (제품 이미지)
let prodImgPid = null;
$("prodImgInput").addEventListener("change", async e => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f || !prodImgPid) return;
  if (f.size > 8 * 1024 * 1024) return toast("이미지는 8MB 이하만 가능합니다");
  const data = await new Promise((res, rej) => {
    const rd = new FileReader(); rd.onload = () => res(rd.result); rd.onerror = rej; rd.readAsDataURL(f);
  });
  try {
    await api(`/api/product/${prodImgPid}/image`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ data }) });
    toast("이미지가 저장되었습니다");
    const pid = prodImgPid;
    await reloadMaster("product");
    openProdHistory(pid);   // 팝업 새 이미지로 즉시 갱신
    if (mTab === "product" && document.querySelector("#scr-items.on")) renderMasters();
  } catch (err) { /* api()가 토스트 표시 */ }
});

// 제품 팝업의 LOT별 소비기한 인라인 저장 (LOT 관리 화면과 동일 API)
$("anaPBody").addEventListener("change", async e => {
  const inp = e.target.closest("[data-plexp]"); if (!inp) return;
  const pid = +inp.dataset.pid;
  await api("/api/lotexpiry", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product_id: pid, made: inp.dataset.made, expiry: inp.value }) });
  toast(inp.value ? `소비기한 ${inp.value} 저장됨` : "소비기한 제거됨 — 제품 소비일로 자동 계산");
  openProdHistory(pid);                                        // 팝업 D-day 갱신
  if (document.querySelector("#scr-lot.on")) loadLot();        // LOT 화면도 갱신
});

/* 자재 입·출고 이력 팝업 (기준정보 자재명 클릭) */
async function openMatHistory(mid) {
  const d = await api("/api/mathistory/" + mid);
  $("anaPTitle").textContent = d.name;
  $("anaPHint").textContent =
    `${d.kind === "raw" ? "원재료" : "부재료"} · 최근 ${d.rows.length}일` +
    ` · 마지막 입고 ${d.last_in ? d.last_in.date + " (+" + NF(d.last_in.in_qty) + d.unit + ")" : "기록 없음"}` +
    ` · 마지막 사용 ${d.last_use ? d.last_use.date + " (" + NF(d.last_use.used_qty) + d.unit + ")" : "기록 없음"}`;
  $("anaPBody").innerHTML = `<div class="tbl-wrap"><table>
    <thead><tr><th>날짜</th><th class="r">전일</th><th class="r">입고</th><th class="r">사용</th><th class="r">실재고</th><th>발주</th></tr></thead>
    <tbody class="num">${d.rows.map(r => `<tr ${r.in_qty > 0 ? 'style="background:var(--ok-soft)"' : ""}>
      <td>${r.date}${r.src === "auto" ? ' <span class="chip cat">자동</span>' : ""}</td>
      <td class="r">${NF(r.prev_qty)}</td>
      <td class="r" ${r.in_qty > 0 ? 'style="color:var(--ok); font-weight:700"' : ""}>${r.in_qty ? "+" + NF(r.in_qty) + (d.in_expiry && d.in_expiry[r.date] ? ` <span class="auto" style="font-weight:400">(유통 ${esc(d.in_expiry[r.date])})</span>` : "") : "·"}</td>
      <td class="r">${r.used_qty ? NF(r.used_qty) : "·"}</td>
      <td class="r" style="font-weight:700">${NF(r.real_qty)}</td>
      <td class="auto">${esc(r.order_date || "")}${r.order_qty ? " (" + NF(r.order_qty) + ")" : ""}</td></tr>`).join("")
      || '<tr><td colspan="6" class="auto">기록 없음</td></tr>'}</tbody></table></div>`;
  $("anaOverlay").classList.add("on");
}

/* ══ 표 헤더 클릭 정렬 (읽기 전용 표 전체) ══ */
document.addEventListener("click", e => {
  const th = e.target.closest("th");
  if (!th || !th.textContent.trim()) return;
  const table = th.closest("table");
  if (!table || !table.tBodies.length) return;
  const tbody = table.tBodies[0];
  if (tbody.querySelector("input, select")) return;   // 입력용 표는 제외
  if (tbody.rows.length < 2) return;
  const idx = [...th.parentNode.children].indexOf(th);
  const dir = th.dataset.sort === "asc" ? -1 : 1;
  table.querySelectorAll("th").forEach(h => {
    delete h.dataset.sort;
    h.textContent = h.textContent.replace(/ [▲▼]$/, "");
  });
  th.dataset.sort = dir === 1 ? "asc" : "desc";
  th.textContent = th.textContent + (dir === 1 ? " ▲" : " ▼");

  const PIN = /^(합계|월계|연간 누계|이론 합계|수불부 사용량|차이|용도별)/;
  const rows = [...tbody.rows];
  const pinned = rows.filter(r => PIN.test((r.cells[0]?.innerText || "").trim()));
  const sortable = rows.filter(r => !pinned.includes(r));
  const val = tr => {
    const t = (tr.cells[idx]?.innerText || "").trim();
    if (t === "∞") return Infinity;
    if (!t || ["—", "·", "없음", "-"].includes(t)) return null;
    if (/^\d{1,4}[-\/.]\d/.test(t)) return t;                 // 날짜류 → 문자열(시간순)
    const m = t.replace(/,/g, "").match(/^[+\-]?\d+(\.\d+)?/);
    if (m) return parseFloat(m[0]);
    return t;
  };
  sortable.sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;                                  // 빈값은 항상 뒤로
    if (vb == null) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    if (typeof va === "number") return -1 * dir;
    if (typeof vb === "number") return 1 * dir;
    return String(va).localeCompare(String(vb), "ko") * dir;
  });
  sortable.forEach(r => tbody.appendChild(r));
  pinned.forEach(r => tbody.appendChild(r));
});

/* ══ 출고 현황 ═════════════════════════ */
const SHIP = { mode: "d", date: "", q: "" };
async function loadShip() {
  if (!SHIP.date) SHIP.date = todayISO();
  const d = await api(`/api/shipstatus?mode=${SHIP.mode}&date=${SHIP.date}`);
  SHIP.data = d;
  const [a, b] = d.range;
  const dowLbl = SHIP.mode === "d" ? ` (${dowOf(d.date)})` : "";
  $("shipLbl").textContent = SHIP.mode === "d" ? d.date + dowLbl
    : SHIP.mode === "y" ? d.date.slice(0, 4) + "년"
    : `${a} ~ ${b}`;
  renderShipStatus();
}
function renderShipStatus() {
  const d = SHIP.data, admin = canM("prod");   // 출고 금액 열람
  $("shipKpis").innerHTML = [
    ["총 출고량", NF(d.total_qty)],
    ["제품 수", d.by_product.length + "종"],
    ["거래처 수", d.by_partner.length + "곳"],
    ...(admin && d.total_amount ? [["출고 금액", NF(Math.round(d.total_amount)) + "원"]] : []),
  ].map(([t, v]) => `<span class="pchip num">${t} <b style="margin-left:4px">${v}</b></span>`).join("");
  const q = SHIP.q.toLowerCase();
  const amtCol = admin ? '<th class="r">금액(원)</th>' : "";
  // 제품별 집계
  const sumQ = arr => arr.reduce((a, r) => a + (Number(r.qty) || 0), 0);
  const sumA = arr => arr.reduce((a, r) => a + (Number(r.amount) || 0), 0);
  const totRow = (label, arr) => `<tr style="font-weight:800; background:var(--bg);">
    <td>${label}</td><td class="r">${NF(Math.round(sumQ(arr)))}</td>
    ${admin ? `<td class="r">${NF(Math.round(sumA(arr)))}</td>` : ""}</tr>`;
  const bp = d.by_product.filter(r => !q || r.name.toLowerCase().includes(q));
  const prodTbl = `<div class="tbl-wrap"><table>
    <thead><tr><th>제품명</th><th class="r">출고량</th>${amtCol}</tr></thead>
    <tbody class="num">${bp.map(r => `<tr><td><b>${esc(r.name)}</b></td>
      <td class="r" style="font-weight:700">${NF(r.qty)}</td>
      ${admin ? `<td class="r">${r.amount ? NF(Math.round(r.amount)) : "—"}</td>` : ""}</tr>`).join("")
      || `<tr><td colspan="${admin ? 3 : 2}" class="auto">출고 없음</td></tr>`}
      ${bp.length ? totRow("합계", bp) : ""}</tbody></table></div>`;
  // 거래처별 집계
  const pt = d.by_partner.filter(r => !q || r.partner.toLowerCase().includes(q));
  const partTbl = `<div class="tbl-wrap"><table>
    <thead><tr><th>거래처</th><th class="r">출고량</th>${amtCol}</tr></thead>
    <tbody class="num">${pt.map(r => `<tr><td><b>${esc(r.partner)}</b></td>
      <td class="r" style="font-weight:700">${NF(r.qty)}</td>
      ${admin ? `<td class="r">${r.amount ? NF(Math.round(r.amount)) : "—"}</td>` : ""}</tr>`).join("")
      || `<tr><td colspan="${admin ? 3 : 2}" class="auto">출고 없음</td></tr>`}
      ${pt.length ? totRow("합계", pt) : ""}</tbody></table></div>`;
  // 개별 출고 내역 (거래처·LOT·소비기한)
  const rw = d.rows.filter(r => !q || (r.name + " " + r.partner).toLowerCase().includes(q));
  // 소비기한 D-day 표시 (만료 빨강 · 3일 이내 주황)
  const today = todayISO();
  const expCell = exp => {
    if (!exp) return '<td class="auto">—</td>';
    const dleft = Math.round((new Date(exp + "T00:00:00") - new Date(today + "T00:00:00")) / 86400000);
    const style = dleft < 0 ? "color:var(--crit); font-weight:700" : dleft <= 3 ? "color:#B45309; font-weight:700" : "";
    return `<td class="num" style="${style}">${exp} <span style="font-size:11px">(${dleft < 0 ? "만료 +" + (-dleft) + "일" : "D-" + dleft})</span></td>`;
  };
  const detail = `<div class="tbl-wrap"><table id="shipDetailTbl">
    <thead><tr>${SHIP.mode !== "d" ? "<th>출고일</th>" : ""}<th>제품명</th><th>거래처</th>
      <th class="r">출고량</th><th class="r">생산 LOT</th><th>소비기한</th>${amtCol}</tr></thead>
    <tbody class="num">${rw.map(r => `<tr>
      ${SHIP.mode !== "d" ? `<td>${r.date}</td>` : ""}
      <td><b>${esc(r.name)}</b></td><td>${esc(r.partner)}</td>
      <td class="r">${NF(r.qty)}</td>
      <td class="r auto">${r.prod_date || "—"}</td>
      ${expCell(r.expiry)}
      ${admin ? `<td class="r">${r.amount ? NF(Math.round(r.amount)) : "—"}</td>` : ""}</tr>`).join("")
      || `<tr><td colspan="7" class="auto">이 기간 출고 내역이 없습니다</td></tr>`}
      ${rw.length ? `<tr style="font-weight:800; background:var(--bg);">
        <td${SHIP.mode !== "d" ? ' colspan="3"' : ' colspan="2"'}>합계 (${rw.length}건)</td>
        <td class="r">${NF(Math.round(sumQ(rw)))}</td><td></td><td></td>
        ${admin ? `<td class="r">${NF(Math.round(sumA(rw)))}</td>` : ""}</tr>` : ""}</tbody></table></div>`;
  $("shipSections").innerHTML =
    psec(1, "제품별 출고", `${SHIP.mode === "d" ? d.date : d.range.join(" ~ ")}`, prodTbl, !d.by_product.length) +
    psec(2, "거래처별 출고", "", partTbl, !d.by_partner.length) +
    psec(3, "출고 내역", `개별 출고 건 · 생산 LOT · 소비기한`, detail, !d.rows.length);
  renderShipCharts();
}
// 출고현황 차트 — 일별 추이(주/월/년) + 거래처 도넛
function renderShipCharts() {
  const d = SHIP.data;
  const showTrend = SHIP.mode !== "d";   // 일일은 단일값이라 추이 생략
  let trendShown = false;
  if (showTrend) {
    const byDay = {};
    d.rows.forEach(r => { byDay[r.date] = (byDay[r.date] || 0) + r.qty; });
    const days = Object.keys(byDay).sort();
    if (days.length) {
      let labels, vals;
      if (SHIP.mode === "y") {   // 년간은 월별 집계
        const byM = {};
        days.forEach(dt2 => { const k = dt2.slice(0, 7); byM[k] = (byM[k] || 0) + byDay[dt2]; });
        const keys = Object.keys(byM).sort();
        labels = keys.map(k => Number(k.slice(5)) + "월");
        vals = keys.map(k => byM[k]);
      } else {
        labels = days.map(x => x.slice(5).replace("-", "/") + (SHIP.mode === "w" ? ` ${dowOf(x)}` : ""));
        vals = days.map(x => byDay[x]);
      }
      barChart($("shipChart"), { labels, series: [{ name: "출고", values: vals, color: "#8B5E34" }] });
      trendShown = true;
    }
  }
  $("shipTrendBox").style.display = trendShown ? "" : "none";
  if (!trendShown && CHARTS.shipChart) { CHARTS.shipChart.destroy(); delete CHARTS.shipChart; }
  // 거래처 비중 도넛
  const bp = d.by_partner || [];
  $("shipDonutBox").style.display = bp.length ? "" : "none";
  if (bp.length) {
    const top = bp.slice(0, 6);
    const etc = bp.slice(6).reduce((s, r) => s + r.qty, 0);
    if (etc > 0) top.push({ partner: "기타", qty: etc });
    const tot = top.reduce((s, r) => s + r.qty, 0) || 1;
    mkChart($("shipDonut"), {
      type: "doughnut",
      data: { labels: top.map(r => r.partner), datasets: [{ data: top.map(r => r.qty),
        backgroundColor: top.map((_, i) => DONUT_PALETTE[i % DONUT_PALETTE.length]),
        borderColor: "#fff", borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "60%",
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => ` ${c.label}: ${NF(c.parsed)}개 (${Math.round(c.parsed / tot * 100)}%)` } } } },
    });
    $("shipDonutLeg").innerHTML = top.map((r, i) => `<div class="dl-row">
      <span class="dl-dot" style="background:${DONUT_PALETTE[i % DONUT_PALETTE.length]}"></span>
      <span class="dl-name">${esc(r.partner)}</span>
      <span class="dl-val num">${NF(r.qty)}개 · ${Math.round(r.qty / tot * 100)}%</span></div>`).join("");
  } else if (CHARTS.shipDonut) { CHARTS.shipDonut.destroy(); delete CHARTS.shipDonut; }
}
$("shipCsv").onclick = () => {
  const t = $("shipDetailTbl");
  if (!t) return toast("내보낼 출고 내역이 없습니다");
  tableToCsv(t.tHead, t.tBodies[0], csvName("출고현황", $("shipLbl").textContent));
};
$("shipTabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-st]"); if (!b) return;
  document.querySelectorAll("#shipTabs button").forEach(x => x.classList.toggle("on", x === b));
  SHIP.mode = b.dataset.st; loadShip();
});
$("shipSections").addEventListener("click", e => {
  const h = e.target.closest("[data-ptoggle]");
  if (h) h.closest(".psec").classList.toggle("closed");
});
function shipStep(dir) {
  const d = new Date(SHIP.date + "T00:00:00");
  if (SHIP.mode === "d") d.setDate(d.getDate() + dir);
  else if (SHIP.mode === "w") d.setDate(d.getDate() + dir * 7);
  else if (SHIP.mode === "m") d.setMonth(d.getMonth() + dir);
  else d.setFullYear(d.getFullYear() + dir);
  SHIP.date = fmtISO(d); loadShip();
}
$("shipPrev").onclick = () => shipStep(-1);
$("shipNext").onclick = () => shipStep(1);
$("shipFilter").addEventListener("input", e => { SHIP.q = e.target.value.trim(); renderShipStatus(); });
// 날짜 라벨 클릭 → 달력 팝업 (생산 현황과 동일)
const shipCal = Calendar("scal", d => { SHIP.date = d; hideShipCal(); loadShip(); });
function hideShipCal() { $("shipCalPop").style.display = "none"; }
$("shipLbl").addEventListener("click", () => {
  const pop = $("shipCalPop");
  if (pop.style.display === "none") {
    shipCal.ym = (SHIP.date || todayISO()).slice(0, 7);
    shipCal.sel = SHIP.date;
    shipCal.render();
    pop.style.display = "";
  } else hideShipCal();
});
document.addEventListener("click", e => {
  if (!e.target.closest("#shipCalPop") && !e.target.closest("#shipLbl")) hideShipCal();
});

/* ══ LOT 관리 ═════════════════════════ */
const LOT = { data: null, filter: "all", q: "", shipQ: "", openMap: {},   // openMap: 제품별 펼침 상태 (세션 유지)
  lotLimit: 100, shipFrom: "", shipTo: "", shipLimit: 50,
  dispQ: "", dispFrom: "", dispTo: "", dispLimit: 50 };
async function loadLot() {
  LOT.data = await api("/api/lotboard");
  renderLot();
}
const LOT_CHIP = {
  expired: '<span class="chip warn">만료</span>',
  soon: '<span class="chip" style="background:#FEF3E2; color:#B45309;">임박</span>',
  ok: '<span class="chip ok">정상</span>',
  unknown: '<span class="chip cat">기한미상</span>',
};
function renderLot() {
  const d = LOT.data;
  if (!d) return;
  const s = d.summary;
  $("lotKpis").innerHTML = [
    ["⚠️ 만료 LOT", s.expired, s.expired > 0 ? "var(--crit)" : ""],
    ["⏰ 임박 LOT (≤7일)", s.soon, s.soon > 0 ? "#B45309" : ""],
    ["✅ 정상 LOT", s.ok, ""],
    ["❔ 기한미상 LOT", s.unknown, ""],
    ["📦 총 재고량", NF(s.total_qty), ""],
    ...(s.total_amount ? [["💰 재고금액", NF(s.total_amount) + "원", ""]] : []),
  ].map(([t, v, c]) => `<span class="pchip num" style="${c ? "color:" + c + "; border-color:" + c : ""}">
      ${t} <b style="margin-left:4px">${v}</b></span>`).join("");
  // 만료 LOT 일괄 폐기 버튼 (관리 가능 + 만료 있을 때만)
  const bulkBtn = $("lotBulkDisp");
  bulkBtn.style.display = (canLotManage() && s.expired > 0) ? "" : "none";
  bulkBtn.textContent = `🗑 만료 LOT 일괄 폐기 (${s.expired})`;
  // 사이드바 배지: 만료 + 임박
  const warn = s.expired + s.soon;
  $("navLotCnt").style.display = warn > 0 ? "" : "none";
  $("navLotCnt").textContent = warn;
  const q = LOT.q.toLowerCase();
  const canLot = canLotManage();
  const matched = d.lots.filter(l =>
    (LOT.filter === "all" || l.status === "expired" || l.status === "soon")
    && (!q || l.name.toLowerCase().includes(q)));
  // 목록 개수 제한 (0 = 전체) — 소비기한 임박 순으로 이미 정렬돼 있어 급한 LOT부터 표시
  const lim = LOT.lotLimit || 0;
  const list = lim > 0 ? matched.slice(0, lim) : matched;
  $("lotCnt").textContent = (lim > 0 && matched.length > lim)
    ? `${list.length}건 표시 / 조건 ${matched.length}건 / 전체 ${d.lots.length}건 · 기준 ${d.date}`
    : `${list.length}건 / 전체 ${d.lots.length}건 · 기준 ${d.date}`;
  // 제품별 그룹: 헤더 행(합계·가장 임박 기한) + 펼치면 LOT 행 — 목록이 길어져도 한눈에
  const groups = [];
  const byPid = {};
  list.forEach(l => {
    if (!byPid[l.product_id]) { byPid[l.product_id] = []; groups.push(l.product_id); }
    byPid[l.product_id].push(l);
  });
  const ORDER = { expired: 0, soon: 1, ok: 2, unknown: 3 };
  const dday = v => v == null ? "—" : v >= 0 ? "D-" + v : "만료 +" + (-v) + "일";
  const ddayStyle = v => v != null && v < 0 ? "color:var(--crit); font-weight:700"
    : v != null && v <= 7 ? "color:#B45309; font-weight:700" : "";
  const lotRow = l => `<tr>
      <td class="auto" style="padding-left:26px;">└</td>
      <td class="r">${l.made
        ? `<button class="uselink num" data-golot="${l.made}" title="이 생산일의 기록 보기">${l.made}</button>`
        : '<span class="auto">미상 (이월)</span>'}</td>
      <td class="r">${NF(l.qty)}</td>
      <td class="r">${l.days_kept != null ? l.days_kept + "일" : "—"}</td>
      <td class="r">${l.planned
        ? `${l.expiry || "—"} <span class="auto" style="font-size:10.5px">(생산실적)</span>`
        : (canLot
          ? `<input class="mini-input datepick" type="text" readonly placeholder="📅 소비기한" data-lexp data-pid="${l.product_id}" data-made="${esc(l.made)}"
               value="${esc(l.expiry || "")}" style="width:135px" title="이 LOT의 소비기한 — 비우면 제품 소비일로 자동 계산">`
          : (l.expiry || "—"))}</td>
      <td class="r" style="${ddayStyle(l.days_left)}">${dday(l.days_left)}</td>
      <td>${LOT_CHIP[l.status]}</td>
      <td>${canLot ? `<button class="btn ghost sm" style="color:var(--crit)"
        data-disp="${l.product_id}" data-made="${esc(l.made)}" data-qty="${l.qty}">폐기</button>` : ""}</td></tr>`;
  // 그룹 = 급한 순 정렬 (만료 → 임박 → 정상 → 기한미상, 같은 상태끼리는 D-day 오름차순)
  const gmeta = {};
  groups.forEach(pid => {
    const ls = byPid[pid];
    gmeta[pid] = {
      worst: ls.reduce((w, l) => ORDER[l.status] < ORDER[w.status] ? l : w, ls[0]),
      minD: ls.reduce((m, l) => l.days_left != null && (m == null || l.days_left < m) ? l.days_left : m, null),
    };
  });
  groups.sort((a, b) => (ORDER[gmeta[a].worst.status] - ORDER[gmeta[b].worst.status])
    || ((gmeta[a].minD ?? 9e9) - (gmeta[b].minD ?? 9e9))
    || byPid[a][0].name.localeCompare(byPid[b][0].name, "ko"));
  $("lotBody").innerHTML = groups.map(pid => {
    const ls = byPid[pid];
    const total = ls.reduce((s, l) => s + l.qty, 0);
    const { worst, minD } = gmeta[pid];
    const minExp = ls.filter(l => l.expiry).map(l => l.expiry).sort()[0] || "";
    // 임박·만료가 있는 제품은 자동 펼침 (사용자 토글은 세션 동안 유지)
    const open = LOT.openMap[pid] ?? (ORDER[worst.status] <= 1);
    const thumb = ls[0].image
      ? `<img src="/image/${encodeURIComponent(ls[0].image)}" style="width:26px; height:26px; object-fit:cover; border-radius:5px; border:1px solid var(--line); vertical-align:middle; margin-right:6px;">`
      : "";
    const head = `<tr class="lotg" data-lg="${pid}">
      <td><span class="tri">${open ? "▾" : "▸"}</span>
        ${thumb}<button class="uselink" data-phist="${pid}">${esc(ls[0].name)}</button></td>
      <td class="r auto" style="font-weight:500">LOT ${ls.length}개</td>
      <td class="r">${NF(Math.round(total * 1000) / 1000)}</td>
      <td class="r auto" style="font-weight:500">—</td>
      <td class="r auto" style="font-weight:500">${minExp ? "~" + minExp : "—"}</td>
      <td class="r" style="${ddayStyle(minD)}">${dday(minD)}</td>
      <td>${LOT_CHIP[worst.status]}</td><td></td></tr>`;
    return head + (open ? ls.map(lotRow).join("") : "");
  }).join("")
    || `<tr><td colspan="8" class="auto">${LOT.filter === "warn" ? "임박·만료 LOT이 없습니다 👍" : "재고가 있는 LOT이 없습니다 — 생산을 저장하면 생산일자별 LOT이 생깁니다"}</td></tr>`;
  renderShipHist();
  renderDispHist();
}
// 기간 필터 라벨 (둘 다 비면 전체, 같으면 하루, 아니면 범위)
function dateScope(from, to) {
  if (!from && !to) return "전체";
  if (from && from === to) return from;
  return `${from || "처음"}~${to || "현재"}`;
}
// 출고 이력(납품) — 소비기한·거래처 확인용 · 기간·개수 필터
function renderShipHist() {
  const d = LOT.data; if (!d) return;
  const sq = (LOT.shipQ || "").toLowerCase();
  const all = d.shipments || [];
  let filt = all;
  if (LOT.shipFrom) filt = filt.filter(r => r.date >= LOT.shipFrom);
  if (LOT.shipTo) filt = filt.filter(r => r.date <= LOT.shipTo);
  if (sq) filt = filt.filter(r => (r.name + " " + r.partner).toLowerCase().includes(sq));
  const lim = LOT.shipLimit || 0;
  const list = lim > 0 ? filt.slice(0, lim) : filt;
  const scope = dateScope(LOT.shipFrom, LOT.shipTo);
  $("shipHistCnt").textContent = (lim > 0 && filt.length > lim)
    ? `${list.length}건 표시 / ${scope} ${filt.length}건 (전체 ${all.length})`
    : `${list.length}건 / ${scope}${(LOT.shipFrom || LOT.shipTo) ? "" : " " + all.length + "건"}`;
  $("shipHistBody").innerHTML = list.map(r => `<tr>
      <td><button class="uselink num" data-golot="${r.date}" title="이 날짜 기록 보기">${r.date}</button></td>
      <td><b>${esc(r.name)}</b></td>
      <td>${esc(r.partner)}</td>
      <td class="r">${NF(r.qty)}</td>
      <td class="r auto">${r.prod_date ? r.prod_date : "—"}</td></tr>`).join("")
    || `<tr><td colspan="5" class="auto">출고 이력이 없습니다</td></tr>`;
}
// 폐기 이력 — 날짜·개수 필터
function renderDispHist() {
  const d = LOT.data; if (!d) return;
  const canLot = canLotManage();
  const dq = (LOT.dispQ || "").toLowerCase();
  const all = d.disposals || [];
  let filt = all;
  if (LOT.dispFrom) filt = filt.filter(r => r.date >= LOT.dispFrom);
  if (LOT.dispTo) filt = filt.filter(r => r.date <= LOT.dispTo);
  if (dq) filt = filt.filter(r => (r.name || "").toLowerCase().includes(dq));
  const lim = LOT.dispLimit || 0;
  const list = lim > 0 ? filt.slice(0, lim) : filt;
  const scope = dateScope(LOT.dispFrom, LOT.dispTo);
  $("dispHistCnt").textContent = (lim > 0 && filt.length > lim)
    ? `${list.length}건 표시 / ${scope} ${filt.length}건 (전체 ${all.length})`
    : `${list.length}건 / ${scope}${(LOT.dispFrom || LOT.dispTo) ? "" : " " + all.length + "건"}`;
  $("dispBody").innerHTML = list.map(r => `<tr>
      <td>${r.date}</td><td><b>${esc(r.name)}</b></td>
      <td class="r">${r.prod_date || "—"}</td><td class="r">${NF(r.qty)}</td>
      <td>${esc(r.reason || "—")}</td><td class="auto">${esc(r.note || "")}</td>
      <td>${canLot ? `<button class="btn ghost sm" data-dispundo="${r.id}">취소</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="7" class="auto">폐기 이력이 없습니다</td></tr>`;
}
// LOT 관리(폐기·소비기한)는 생산 담당만 — guest는 보기 전용
function canLotManage() {
  return ROLE === "admin" || (ROLE !== "guest" && MYDUTY.has("lot"));
}
$("lotTabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-lf]"); if (!b) return;
  document.querySelectorAll("#lotTabs button").forEach(x => x.classList.toggle("on", x === b));
  LOT.filter = b.dataset.lf; renderLot();
});
$("lotFilter").addEventListener("input", e => { LOT.q = e.target.value.trim(); renderLot(); });
$("lotFilter").addEventListener("focus", () => {
  $("qaProducts").innerHTML = M.product.map(o => `<option value="${esc(o.name)}">`).join("");
});
$("lotLimit").addEventListener("change", e => { LOT.lotLimit = +e.target.value; renderLot(); });
// 출고 이력 필터
$("shipHistFilter").addEventListener("input", e => { LOT.shipQ = e.target.value.trim(); renderShipHist(); });
$("shipHistFilter").addEventListener("focus", () => {
  $("qaProducts").innerHTML = M.product.map(o => `<option value="${esc(o.name)}">`).join("");
});
$("shipHistFrom").addEventListener("change", e => { LOT.shipFrom = e.target.value; renderShipHist(); });
$("shipHistTo").addEventListener("change", e => { LOT.shipTo = e.target.value; renderShipHist(); });
$("shipHistDateClear").addEventListener("click", () => {
  LOT.shipFrom = ""; LOT.shipTo = ""; $("shipHistFrom").value = ""; $("shipHistTo").value = ""; renderShipHist();
});
$("shipHistLimit").addEventListener("change", e => { LOT.shipLimit = +e.target.value; renderShipHist(); });
// 폐기 이력 필터
$("dispHistFilter").addEventListener("input", e => { LOT.dispQ = e.target.value.trim(); renderDispHist(); });
$("dispHistFilter").addEventListener("focus", () => {
  $("qaProducts").innerHTML = M.product.map(o => `<option value="${esc(o.name)}">`).join("");
});
$("dispHistFrom").addEventListener("change", e => { LOT.dispFrom = e.target.value; renderDispHist(); });
$("dispHistTo").addEventListener("change", e => { LOT.dispTo = e.target.value; renderDispHist(); });
$("dispHistDateClear").addEventListener("click", () => {
  LOT.dispFrom = ""; LOT.dispTo = ""; $("dispHistFrom").value = ""; $("dispHistTo").value = ""; renderDispHist();
});
$("dispHistLimit").addEventListener("change", e => { LOT.dispLimit = +e.target.value; renderDispHist(); });
$("shipHistBody").addEventListener("click", e => {
  const go = e.target.closest("[data-golot]");
  if (go) gotoLookup(go.dataset.golot);
});
/* 폐기 모달 */
let dispCtx = null;
function openDisp(pid, made, qty) {
  const p = productById(pid);
  dispCtx = { product_id: pid, prod_date: made, maxQty: qty };
  $("dispTarget").innerHTML = `${esc(p ? p.name : "?")} — LOT ${made || "생산일 미상"} <span class="auto">(현재 ${NF(qty)}개)</span>`;
  $("dispQty").value = qty;
  $("dispDate").value = todayISO();
  $("dispReason").value = "소비기한 만료";
  $("dispNote").value = "";
  $("dispOverlay").classList.add("on");
  $("dispQty").focus();
}
window.closeDisp = () => $("dispOverlay").classList.remove("on");
$("dispSave").onclick = async () => {
  const qty = Number(String($("dispQty").value).replace(/,/g, ""));
  if (!(qty > 0)) return toast("폐기 수량을 입력하세요");
  if (dispCtx && dispCtx.maxQty != null && qty - dispCtx.maxQty > 0.5)
    return toast(`폐기 수량이 이 LOT 재고 ${NF(dispCtx.maxQty)}개를 초과합니다`);
  await api("/api/disposal", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...dispCtx, qty, date: $("dispDate").value,
      reason: $("dispReason").value, note: $("dispNote").value.trim() }) });
  closeDisp();
  toast("폐기 처리됨 — 재고에서 차감되었습니다");
  ANA.raw = null;
  await reloadMaster("product");
  loadLot();
};
$("lotBody").addEventListener("click", e => {
  const d = e.target.closest("[data-disp]");
  if (d) { openDisp(+d.dataset.disp, d.dataset.made, +d.dataset.qty); return; }
  const ph = e.target.closest("[data-phist]");
  if (ph) { openProdHistory(+ph.dataset.phist); return; }
  const go = e.target.closest("[data-golot]");
  if (go) { gotoLookup(go.dataset.golot); return; }   // 생산일 클릭 → 그날 기록 조회로 이동
  const g = e.target.closest("tr.lotg");              // 제품 그룹 행 클릭 = 접기/펼치기
  if (g) {
    const pid = +g.dataset.lg;
    const ORDER = { expired: 0, soon: 1, ok: 2, unknown: 3 };
    const cur = LOT.openMap[pid] ?? (LOT.data.lots.some(l =>
      l.product_id === pid && ORDER[l.status] <= 1));
    LOT.openMap[pid] = !cur;
    renderLot();
  }
});
// LOT별 소비기한 인라인 입력 → 즉시 저장 (비우면 제품 소비일 폴백)
$("lotBody").addEventListener("change", async e => {
  const inp = e.target.closest("[data-lexp]"); if (!inp) return;
  await api("/api/lotexpiry", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product_id: +inp.dataset.pid, made: inp.dataset.made, expiry: inp.value }) });
  toast(inp.value ? `소비기한 ${inp.value} 저장됨` : "소비기한 제거됨 — 제품 소비일로 자동 계산");
  loadLot();
});
window.gotoLookup = (date) => {
  document.querySelector('#nav button[data-scr="lookup"]').click();
  lkCal.ym = date.slice(0, 7); lkCal.sel = date; lkCal.render(); loadLookup(date);
};
$("dispBody").addEventListener("click", async e => {
  const u = e.target.closest("[data-dispundo]"); if (!u) return;
  if (!confirm("이 폐기 기록을 취소할까요? 차감됐던 재고가 복구됩니다.")) return;
  await api("/api/disposal/" + u.dataset.dispundo, { method: "DELETE" });
  toast("폐기 취소됨 — 재고 복구");
  ANA.raw = null;
  await reloadMaster("product");
  loadLot();
});
// 만료 LOT 일괄 폐기 — 만료된 LOT 전부를 '소비기한 만료' 사유로 한 번에 처리 (건별 취소 가능)
$("lotBulkDisp").onclick = async () => {
  const expired = (LOT.data?.lots || []).filter(l => l.status === "expired" && l.qty > 0);
  if (!expired.length) return toast("만료된 LOT이 없습니다");
  const lines = expired.map(l => `· ${l.name} — LOT ${l.made || "미상"} · ${NF(l.qty)}개 (만료 +${-l.days_left}일)`);
  if (!confirm(`만료 LOT ${expired.length}건을 전부 폐기 처리할까요?\n(사유: 소비기한 만료 · 폐기 이력에서 건별 취소 가능)\n\n${lines.join("\n")}`)) return;
  let ok = 0, fail = 0;
  for (const l of expired) {
    try {
      await api("/api/disposal", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: l.product_id, prod_date: l.made, qty: l.qty,
          date: todayISO(), reason: "소비기한 만료", note: "일괄 폐기" }) });
      ok++;
    } catch (err) { fail++; }
  }
  toast(`만료 LOT 일괄 폐기: ${ok}건 완료${fail ? ` · ${fail}건 실패` : ""}`);
  ANA.raw = null;
  await reloadMaster("product");
  loadLot();
};

/* ── 숫자패드 (일일 입력 · 배합비의 숫자칸 클릭 시) ── */
const numPad = $("numPad");
let padTarget = null;
let padSkipScrollHide = false;   // Tab 이동 중 focus 스크롤로 패드가 닫히는 것 방지
function padEligible(el) {
  if (!(el instanceof HTMLInputElement)) return false;
  if (el.type === "date") return false;
  if (el.classList.contains("datepick")) return false;   // 날짜칸은 달력 팝업 사용
  if (el.dataset.f === "order_date" || el.dataset.f === "note") return false;
  if (el.hasAttribute("list")) return false;   // 🔍 검색칸(자동완성 datalist)은 글자 입력 — 키패드 제외
  if (!el.classList.contains("mini-input")) return false;
  // 배합비 표는 #scr-items 안에 있음 (#mt-bom 요소는 존재하지 않아 배합비 키패드가 안 뜨던 버그 수정)
  // 소비기한 분할 모달·폐기 모달의 숫자칸도 키패드 지원
  return !!(el.closest("#scr-entry") || el.closest("#lotSplitOverlay") || el.closest("#dispOverlay")
    || (el.closest("#scr-items") && mTab === "bom"));
}
function evalExpr(s) {
  s = String(s).replace(/×/g, "*").replace(/÷/g, "/").replace(/−/g, "-").replace(/,/g, "").trim();
  if (!/^[-0-9.+*/() ]+$/.test(s) || !/\d/.test(s)) return null;
  s = s.replace(/[+\-*/. ]+$/, "");
  try {
    const r = Function('"use strict";return (' + s + ")")();
    return Number.isFinite(r) ? Math.round(r * 1000) / 1000 : null;
  } catch (e) { return null; }
}
/** 키패드 상단: 이전 저장값 ↔ 현재 입력값 (수정 중 원래 값을 확인할 수 있게) */
function renderPadInfo() {
  const box = $("padInfo");
  if (!padTarget) { box.innerHTML = ""; return; }
  const prev = savedValueOf(padTarget);
  const curRaw = String(padTarget.value ?? "").replace(/,/g, "").trim();
  const cur = curRaw === "" ? null : Number(curRaw);
  const fmt = v => v == null || !isFinite(v) ? "—" : NF(Math.round(v * 1000) / 1000);
  let diff = "";
  if (prev != null && cur != null && isFinite(cur)) {
    const d = Math.round((cur - Number(prev)) * 1000) / 1000;
    diff = d === 0 ? '<span class="diff" style="color:var(--ok)">변경 없음</span>'
      : `<span class="diff" style="color:${d > 0 ? "var(--ok)" : "var(--crit)"}">${d > 0 ? "+" : ""}${NF(d)}</span>`;
  }
  box.innerHTML = `<span class="pv prev"><span class="lbl">이전 저장값</span><b>${prev == null ? "—" : fmt(Number(prev))}</b></span>
    ${diff}
    <span class="pv cur" style="text-align:right"><span class="lbl">현재 입력값</span><b>${curRaw === "" ? "—" : (isFinite(cur) ? fmt(cur) : esc(curRaw))}</b></span>`;
}
function showPad(el) {
  padTarget = el;
  numPad.style.display = "grid";
  renderPadInfo();
  const r = el.getBoundingClientRect(), pw = 5 * 50 + 4 * 6 + 20;
  const ph = 4 * 44 + 3 * 6 + 20 + numPad.querySelector("#padInfo").offsetHeight;   // 상단 정보 높이 포함
  let x = Math.min(r.left, innerWidth - pw - 8);
  let y = r.bottom + 6;
  if (y + ph > innerHeight - 8) y = r.top - ph - 6;
  numPad.style.left = Math.max(8, x) + "px";
  numPad.style.top = Math.max(8, y) + "px";
}
function hidePad() { numPad.style.display = "none"; padTarget = null; }
/** 수식(45*3 등)이 남아 있으면 계산해 값으로 확정 — 계산 불가면 false (이동·닫기 중단) */
function padCommit(t) {
  if (/[+*/]|(?<=\d)-/.test(t.value)) {
    const r = evalExpr(t.value);
    if (r == null) { toast("수식을 계산할 수 없습니다"); return false; }
    t.value = r;
    t.dispatchEvent(new Event("input", { bubbles: true }));
  }
  return true;
}
/** Tab 이동 대상 — 같은 화면(모달) 안의 보이는 숫자칸들을 화면 순서대로 */
function padInputs(el) {
  const scope = el.closest("#lotSplitOverlay, #dispOverlay, #scr-entry, #scr-items") || document;
  return [...scope.querySelectorAll("input.mini-input")]
    .filter(x => padEligible(x) && !x.disabled && !x.readOnly && x.offsetParent !== null);
}
document.addEventListener("focusin", e => { if (padEligible(e.target)) showPad(e.target); });
document.addEventListener("click", e => { if (padEligible(e.target)) showPad(e.target); });
// 숫자칸 키보드 — Enter = 확인(수식 계산 후 닫기), Tab = 다음 숫자칸 (Shift+Tab = 이전)
document.addEventListener("keydown", e => {
  if (!padEligible(e.target)) return;
  if (e.key !== "Enter" && e.key !== "Tab") return;
  const el = e.target;
  if (!padCommit(el)) { e.preventDefault(); return; }   // 수식 오류면 그 칸에 머무름
  if (e.key === "Enter") { e.preventDefault(); hidePad(); el.blur(); return; }
  const list = padInputs(el);
  const to = list[list.indexOf(el) + (e.shiftKey ? -1 : 1)];
  if (!to) return;                                      // 처음·끝이면 기본 Tab 동작에 맡김
  e.preventDefault();
  // focus()가 그 칸을 보이게 스크롤하면 scroll 핸들러가 패드를 닫아버린다 —
  // 이동하는 동안만 닫기를 막고, 스크롤이 끝난 위치에 패드를 다시 띄운다.
  padSkipScrollHide = true;
  to.focus(); to.select();                              // 바로 덮어쓸 수 있게 선택 상태로
  setTimeout(() => { padSkipScrollHide = false; if (document.activeElement === to) showPad(to); }, 60);
});
numPad.addEventListener("mousedown", e => {
  e.preventDefault();                       // 버튼을 눌러도 입력칸 포커스 유지
  const b = e.target.closest("button[data-k]"); if (!b || !padTarget) return;
  const k = b.dataset.k;
  if (k === "OK") {
    const t = padTarget;
    if (!padCommit(t)) return;
    hidePad(); t.blur(); return;
  }
  let v = padTarget.value;
  if (k === "C") v = "";
  else if (k === "⌫") v = v.slice(0, -1);
  else if ("+-*/".includes(k)) v = v.replace(/[+\-*/]$/, "") + k;   // 연산자 연타 방지
  else v += k;
  padTarget.value = v;
  padTarget.dispatchEvent(new Event("input", { bubbles: true }));   // 데이터 바인딩 + 자동계산
  renderPadInfo();                                                 // 현재 입력값·차이 갱신
});
// 키보드로 직접 칠 때도 현재 입력값 표시 갱신
document.addEventListener("input", e => {
  if (padTarget && e.target === padTarget) renderPadInfo();
});
document.addEventListener("mousedown", e => {
  if (numPad.style.display === "none") return;
  if (e.target.closest("#numPad")) return;
  if (padEligible(e.target)) return;        // 다른 숫자칸 클릭 → focusin이 재배치
  hidePad();
});
window.addEventListener("scroll", () => { if (!padSkipScrollHide) hidePad(); }, true);
// 수식(45*3 등)을 남긴 채 칸을 떠나면 자동 계산
document.addEventListener("change", e => {
  if (!padEligible(e.target)) return;
  if (/[+*/]|(?<=\d)-/.test(e.target.value)) {
    const r = evalExpr(e.target.value);
    if (r != null) {
      e.target.value = r;
      e.target.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
});

/* ── 숫자칸 글자 입력 경고 — 숫자·콤마·소수점·사칙연산 수식만 허용 ── */
function isNumericField(el) {
  if (!(el instanceof HTMLInputElement) || el.type === "date") return false;
  if (el.hasAttribute("list")) return false;                                  // 검색칸
  if (el.dataset.f === "order_date" || el.dataset.f === "note" || el.dataset.bf === "note")
    return false;                                                             // 메모성 텍스트칸
  return (el.classList.contains("mini-input") && el.classList.contains("num"))
      || el.matches('#mstForm [inputmode="decimal"]');
}
let _numWarnAt = 0;
document.addEventListener("input", e => {
  const el = e.target;
  if (!isNumericField(el)) return;
  if (el.dataset._numClean) return;   // 아래 재발화 루프 방지
  if (!/[^\d+\-*/.,()×÷−\s]/.test(el.value)) { el.classList.remove("bad-num"); return; }
  // 허용 외 문자는 즉시 제거 (진짜 차단) — 잠깐 빨갛게 + 안내
  const pos = el.selectionStart;
  const removed = (el.value.match(/[^\d+\-*/.,()×÷−\s]/g) || []).length;
  el.value = el.value.replace(/[^\d+\-*/.,()×÷−\s]/g, "");
  try { el.setSelectionRange(Math.max(0, pos - removed), Math.max(0, pos - removed)); } catch (_) {}
  el.dataset._numClean = "1";
  el.dispatchEvent(new Event("input", { bubbles: true }));   // 정리된 값을 상태에 재반영
  delete el.dataset._numClean;
  el.classList.add("bad-num");
  setTimeout(() => el.classList.remove("bad-num"), 900);
  if (Date.now() - _numWarnAt > 2500) {   // 토스트 도배 방지
    toast("숫자만 입력할 수 있습니다 (수식 45*3 가능) — 글자는 자동으로 지워집니다");
    _numWarnAt = Date.now();
  }
});

/* ── 모달 공통 닫기 ─────────────────── */
["mstOverlay", "useOverlay", "stopOverlay", "anaOverlay", "dispOverlay", "lotSplitOverlay", "packSetOverlay"].forEach(id => {
  $(id).addEventListener("click", e => { if (e.target.id === id) $(id).classList.remove("on"); });
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    ["mstOverlay", "useOverlay", "stopOverlay", "anaOverlay", "packSetOverlay"].forEach(id => $(id).classList.remove("on"));
    hidePad();
  }
});

/* ── 관리 도구 (admin): 백업/복원 + 데이터 점검 ── */
async function openAdmin() {
  $("adminOverlay").classList.add("on");
  $("integOut").innerHTML = "";
  $("integFix").style.display = "none";
  $("updApply").style.display = "none";
  $("updNotes").textContent = "";
  $("updMsg").textContent = "";
  UPD.latest = null;
  loadBackups();
  updCheck(true);   // 열 때 조용히 현재 버전 표시(있으면 새 버전도)
}
window.closeAdmin = () => $("adminOverlay").classList.remove("on");
$("btnAdmin").onclick = openAdmin;

/* ── 프로그램 업데이트 ── */
const UPD = { latest: null };
async function updCheck(quiet) {
  $("updMsg").textContent = quiet ? "" : "확인 중…";
  $("updApply").style.display = "none";
  let d;
  try { d = await api("/api/update/check"); }
  catch (e) { $("updMsg").textContent = "확인 실패"; return; }
  $("updCur").textContent = "v" + d.current + (d.frozen ? "" : " (개발)");
  if (d.error) { $("updMsg").textContent = quiet ? "" : "⚠ " + d.error; return; }
  if (!d.latest) { $("updMsg").textContent = ""; return; }
  if (d.newer) {
    UPD.latest = d.latest;
    $("updMsg").innerHTML = `<span style="color:var(--crit); font-weight:700">🆕 새 버전 v${esc(d.latest)}</span>`;
    $("updNotes").textContent = d.notes || "";
    $("updApply").style.display = d.frozen ? "" : "none";
    if (!d.frozen) $("updMsg").innerHTML += ' <span class="auto">— exe 실행 시에만 자동 교체됩니다</span>';
  } else {
    $("updMsg").innerHTML = '<span style="color:var(--ok)">✓ 최신 버전입니다</span>';
    $("updNotes").textContent = "";
  }
}
$("updCheck").onclick = () => updCheck(false);
$("updApply").onclick = async () => {
  if (!UPD.latest) return;
  const online = (CHAT.count || 1) - 1;   // 나 제외 접속자
  if (!confirm(`v${UPD.latest}(으)로 업데이트합니다.\n\n`
    + `• 데이터(DB)는 그대로 유지되고, 교체 전 자동 백업됩니다\n`
    + `• 프로그램이 잠깐 재시작됩니다`
    + (online > 0 ? `\n• 지금 다른 사용자 ${online}명이 접속 중입니다 — 잠시 끊깁니다` : "")
    + `\n\n진행할까요?`)) return;
  $("updApply").disabled = true;
  $("updMsg").textContent = "⬇️ 다운로드 중… 완료되면 자동으로 재시작됩니다 (창이 잠깐 닫힐 수 있어요)";
  try {
    await api("/api/update/apply", { method: "POST" });
    $("updMsg").innerHTML = '✅ 다운로드 완료 — 곧 재시작됩니다. <b>이 창(브라우저)은 30초 뒤 새로고침</b>하세요.';
    setTimeout(() => location.reload(), 30000);
  } catch (e) {
    $("updApply").disabled = false;
    $("updMsg").textContent = "⚠ 업데이트 실패 — 기존 버전이 그대로 유지됩니다";
  }
};
async function loadBackups() {
  const list = await api("/api/backups");
  const fmt = n => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
  $("bkList").innerHTML = list.length ? list.map(b => `
    <div style="display:flex; align-items:center; gap:8px; padding:7px 10px; border-bottom:1px solid var(--line-soft); font-size:12.5px;">
      <span class="num" style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;">${esc(b.name)}</span>
      <span class="auto num" style="font-size:11px;">${b.at} · ${fmt(b.size)}</span>
      <button class="btn ghost sm" data-bkrestore="${esc(b.name)}" style="color:var(--crit)">복원</button>
    </div>`).join("")
    : '<div class="auto" style="padding:10px;">백업이 아직 없습니다 — 프로그램을 실행해 두면 매일 자동 생성됩니다</div>';
}
$("bkNow").onclick = async () => {
  const r = await api("/api/backup", { method: "POST" });
  toast(`💾 백업 완료: ${r.name}`);
  loadBackups();
};
$("bkList").addEventListener("click", async e => {
  const b = e.target.closest("[data-bkrestore]"); if (!b) return;
  const name = b.dataset.bkrestore;
  if (!confirm(`'${name}' 시점으로 복원할까요?\n\n· 현재 DB가 그 시점 상태로 되돌아갑니다\n· 복원 직전 상태도 자동 백업됩니다 (되돌리기 가능)\n· 복원 후 화면이 새로고침됩니다`)) return;
  const r = await api("/api/backup/restore", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  toast(`복원 완료 — 직전 상태는 ${r.safety} 에 저장됨. 새로고침합니다`);
  setTimeout(() => location.reload(), 1500);
});
$("integRun").onclick = async () => {
  $("integOut").innerHTML = '<span class="auto">점검 중…</span>';
  const d = await api("/api/integrity");
  const line = (label, arr) => arr.length
    ? `<div style="color:var(--crit)">⚠ ${label}: <b>${arr.length}건</b> — ${arr.slice(0, 5).map(esc).join(", ")}${arr.length > 5 ? " …" : ""}</div>`
    : `<div style="color:var(--ok)">✅ ${label}: 이상 없음</div>`;
  $("integOut").innerHTML =
    `<div class="auto" style="font-size:11.5px; margin-bottom:4px;">자재 ${d.materials}종 검사</div>` +
    line("전일재고 체인", d.chain) +
    line("자동차감 정합", d.auto_bad) +
    line("자재 음수 재고", d.negative) +
    line("완제품 음수 재고", d.product_negative) +
    (d.orphans ? `<div style="color:var(--crit)" title="지워진 인원·가동 기록을 가리키는 짝 잃은 데이터">⚠ 연결 끊긴 기록: ${d.orphans}건</div>`
      : '<div style="color:var(--ok)">✅ 연결 끊긴 기록: 없음</div>') +
    (d.ok ? '<div style="margin-top:6px; font-weight:700; color:var(--ok)">데이터가 깨끗합니다 👍</div>' : "");
  $("integFix").style.display = (d.chain.length || d.auto_bad.length) ? "" : "none";
};
$("integFix").onclick = async () => {
  if (!confirm("전 자재의 재고 체인을 재계산합니다.\n(실사 값은 그대로 두고 전일재고·자동차감만 다시 이어 붙입니다)\n진행할까요?")) return;
  const r = await api("/api/integrity/fix", { method: "POST" });
  toast(`🔧 자재 ${r.fixed}종 체인 재계산 완료`);
  $("integRun").click();
};

/* ── 모바일: 햄버거 사이드바 ── */
function sideOpen(on) {
  document.querySelector(".side").classList.toggle("open", on);
  $("sideDim").classList.toggle("on", on);
}
$("menuBtn").onclick = () => sideOpen(!document.querySelector(".side").classList.contains("open"));
$("sideDim").onclick = () => sideOpen(false);
$("nav").addEventListener("click", () => sideOpen(false));   // 메뉴 선택 시 서랍 닫기

/* ── 로그인 / 부팅 ───────────────────── */
function showLogin() {
  $("loginOverlay").style.display = "flex";
  setTimeout(() => $("loginId").focus(), 60);
}
async function startApp(me) {
  ROLE = me.role; USERNAME = me.username; DUTY = me.duty || "all";
  MYDUTY = ROLE === "admin" ? new Set(DUTY_KEYS) : dutySet(DUTY);
  MPERM = new Set(me.money_perms || []);
  $("loginOverlay").style.display = "none";
  $("userBox").style.display = "";
  const dutyLbl = (ROLE === "admin" || MYDUTY.size === DUTY_KEYS.length) ? ""
    : MYDUTY.size === 0 ? " · 담당 없음(입력 불가)"
    : " · " + [...MYDUTY].map(k => DUTY_LABELS[k]).join("·");
  $("userLbl").textContent = `${me.username} · ${({ admin: "관리자", op: "운영", guest: "게스트(보기 전용)" })[me.role] || me.role}${dutyLbl}`;
  if (ROLE === "admin") {
    $("tabUsers").style.display = "";
    $("tabAudit").style.display = "";
    $("btnAdmin").style.display = "";
  }
  // 담당별 일일 입력: 자기 탭이 기본 + 담당 아닌 탭 저장 버튼 비활성 (서버도 403으로 강제)
  const canProd = ROLE === "admin" || PROD_DUTIES.some(k => MYDUTY.has(k));
  const canStock = ROLE === "admin" || MYDUTY.has("stock");
  if (!canProd && canStock) { entryTab = "stock"; renderEntryTabs(); }
  const noDuty = "담당이 지정되지 않아 일일 입력을 저장할 수 없습니다";
  $("btnSaveDay").disabled = !canProd;
  $("btnSaveDay").title = canProd ? ""
    : (MYDUTY.size === 0 ? noDuty : "생산 입력 담당이 아니어서 저장할 수 없습니다");
  $("btnSaveStock").disabled = !canStock;
  $("btnSaveStock").title = canStock ? ""
    : (MYDUTY.size === 0 ? noDuty : "재고·입고 담당이 아니어서 저장할 수 없습니다");
  await loadMasters();
  await loadDash();
  loadLowStock();   // 사이드바 '발주 필요' 알림 (자재 담당·admin)
  $("dbStatus").textContent = `DB 연결됨 · 제품 ${M.product.length} · 자재 ${M.raw.length + M.sub.length}`;
  // 권한 실시간 반영: admin이 권한을 바꾸면(서버 세션은 즉시 교체됨) 화면도 20초 내 자동 새로고침
  setInterval(async () => {
    try {
      const r = await fetch("/api/me");
      if (r.status === 401) { location.reload(); return; }
      const me2 = await r.json();
      const mp2 = (me2.money_perms || []).slice().sort().join(",");
      if (me2.role !== ROLE || (me2.duty || "all") !== DUTY
          || (ROLE !== "admin" && mp2 !== [...MPERM].sort().join(","))) {
        toast("권한/담당이 변경되었습니다 — 화면을 새로 불러옵니다");
        setTimeout(() => location.reload(), 1200);
      }
    } catch (e) { /* 서버 재시작 등 일시 오류 무시 */ }
  }, 20000);
  startPresence();
}

/* ── 접속 인원 + 채팅 ───────────────────── */
const CHAT = { lastId: 0, open: false, unread: 0, started: false, day: null,
  viewDay: null,      // null = 오늘 대화(실시간), 날짜 문자열 = 지난 대화 보기(읽기 전용)
  me: "", users: [], reads: {}, mentionUnread: 0, pending: null, mentList: [], mentIdx: -1 };
function startPresence() {
  if (CHAT.started) return;
  CHAT.started = true;
  $("chatWidget").style.display = "flex";
  $("chatToggle").onclick = toggleChat;
  $("chatClose").onclick = () => { $("chatPanel").classList.remove("on"); CHAT.open = false; };
  $("chatSend").onclick = chatSend;
  $("chatInput").addEventListener("keydown", chatInputKey);
  $("chatInput").addEventListener("input", chatMentionUpdate);
  $("chatMentions").addEventListener("mousedown", e => {   // mousedown: 입력 포커스 유지
    const b = e.target.closest("[data-ment]"); if (!b) return;
    e.preventDefault(); chatMentionPick(b.dataset.ment);
  });
  $("chatPrev").onclick = () => chatStep("prev");
  $("chatNext").onclick = () => chatStep("next");
  $("chatToday").onclick = chatGoToday;
  $("chatAttach").onclick = () => $("chatFile").click();
  $("chatFile").addEventListener("change", chatPickFile);
  $("chatFileDel").onclick = () => { CHAT.pending = null; $("chatFileRow").classList.remove("on"); };
  pollPresence();
  setInterval(pollPresence, 8000);
}
const chatDayHeader = d => `<div class="cday">— ${d} (${dowOf(d)}) —</div>`;
// 메시지 한 건 → HTML (시스템 / 멘션 / 첨부 / 읽음 표시)
function chatMsgHtml(m, me) {
  const t = (m.at || "").slice(11, 16);
  if (m.kind === "system")
    return `<div class="csys">${esc(m.text)}<span class="cat">${t}</span></div>`;
  const mine = m.username === me;
  const mentioned = !mine && (m.mentions || "").includes("," + me + ",");
  let body = esc(m.text);
  // @이름 하이라이트 — 긴 이름부터 한 번에 치환 (짧은 이름이 긴 이름 안을 파고들지 않게)
  const names = (CHAT.users || []).slice().sort((a, b) => b.length - a.length)
    .map(u => esc(u).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (names.length) {
    body = body.replace(new RegExp("@(" + names.join("|") + ")", "g"),
      (s, u) => `<span class="cment${u === esc(me) ? " me" : ""}">@${u}</span>`);
  }
  let att = "";
  if (m.file) {
    const url = "/chatfile/" + encodeURIComponent(m.file);
    att = m.fkind === "image"
      ? `<a href="${url}" target="_blank"><img class="cimg" src="${url}" alt="${esc(m.fname)}"></a>`
      : `<a class="cfile" href="${url}" download="${esc(m.fname)}">📎 ${esc(m.fname)}</a>`;
  }
  return `<div class="cmsg ${mine ? "me" : "them"}${mentioned ? " mentioned" : ""}" data-mid="${m.id}">
    ${mine ? "" : `<div class="cwho">${esc(m.username)}</div>`}${body}${att}
    <span class="cat">${mine ? `<span class="cread" data-cread="${m.id}"></span>` : ""}${t}</span></div>`;
}
// 내 메시지의 '읽음 N' — 나 말고 그 메시지 id까지 읽은 사람 수
function refreshReadMarks(me, reads) {
  const es = Object.entries(reads || {});
  $("chatMsgs").querySelectorAll("[data-cread]").forEach(el => {
    const id = +el.dataset.cread;
    const n = es.filter(([u, v]) => u !== me && v >= id).length;
    el.textContent = n > 0 ? `읽음 ${n} ` : "";
  });
}
function renderChatNav() {
  const past = !!CHAT.viewDay;
  const d = CHAT.viewDay || CHAT.day || "";
  $("chatDayNav").classList.toggle("past", past);
  $("chatDayLbl").textContent = d ? `${d} (${dowOf(d)})${past ? " · 지난 대화" : ""}` : "";
  $("chatToday").style.display = past ? "" : "none";
  $("chatInput").disabled = $("chatSend").disabled = $("chatAttach").disabled = past;
  $("chatInput").placeholder = past ? "지난 대화 보기 — [오늘]에서 대화하세요" : "메시지 입력… (@이름으로 호출)";
}
// ◀ ▶ — 대화가 있는 이전/다음 날짜로 이동 (없는 날은 건너뜀)
async function chatStep(dir) {
  const base = CHAT.viewDay || CHAT.day || new Date().toISOString().slice(0, 10);
  try {
    const r = await api("/api/chat/day?d=" + encodeURIComponent(base));
    const to = dir === "prev" ? r.prev : r.next;
    if (!to) { toast(dir === "prev" ? "이전 대화가 없습니다" : "다음 대화가 없습니다"); return; }
    await chatOpenDay(to);
  } catch (e) { /* api()가 토스트 */ }
}
async function chatOpenDay(d) {
  const r = await api("/api/chat/day?d=" + encodeURIComponent(d));
  if (r.day === r.today) { await chatGoToday(); return; }
  CHAT.viewDay = r.day;
  $("chatMsgs").innerHTML = chatDayHeader(r.day) +
    (r.messages.map(m => chatMsgHtml(m, CHAT.me)).join("") || '<div class="cday">대화 없음</div>');
  refreshReadMarks(CHAT.me, r.reads);
  renderChatNav();
  $("chatMsgs").scrollTop = $("chatMsgs").scrollHeight;
}
async function chatGoToday() {
  CHAT.viewDay = null; CHAT.day = null; CHAT.lastId = 0;   // day=null → 폴링이 날짜 헤더부터 새로 그림
  $("chatMsgs").innerHTML = "";
  await pollPresence();
  renderChatNav();
  $("chatMsgs").scrollTop = $("chatMsgs").scrollHeight;
}
/* @멘션 자동완성 */
function chatMentionUpdate() {
  const inp = $("chatInput"), box = $("chatMentions");
  const m = inp.value.slice(0, inp.selectionStart).match(/@([^\s@]*)$/);
  const list = m ? (CHAT.users || []).filter(u => u.toLowerCase().startsWith(m[1].toLowerCase())).slice(0, 6) : [];
  if (!list.length) { box.classList.remove("on"); CHAT.mentList = []; CHAT.mentIdx = -1; return; }
  CHAT.mentList = list; CHAT.mentIdx = 0;
  box.innerHTML = list.map((u, i) => `<button data-ment="${esc(u)}" class="${i === 0 ? "on" : ""}">@${esc(u)}</button>`).join("");
  box.classList.add("on");
}
function chatMentionPick(u) {
  const inp = $("chatInput"), pos = inp.selectionStart;
  const before = inp.value.slice(0, pos).replace(/@([^\s@]*)$/, "@" + u + " ");
  inp.value = before + inp.value.slice(pos);
  inp.focus();
  inp.selectionStart = inp.selectionEnd = before.length;
  $("chatMentions").classList.remove("on"); CHAT.mentList = []; CHAT.mentIdx = -1;
}
function chatInputKey(e) {
  const open = CHAT.mentList.length > 0;
  if (open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault();
    CHAT.mentIdx = (CHAT.mentIdx + (e.key === "ArrowDown" ? 1 : -1) + CHAT.mentList.length) % CHAT.mentList.length;
    [...$("chatMentions").children].forEach((b, i) => b.classList.toggle("on", i === CHAT.mentIdx));
    return;
  }
  if (open && (e.key === "Enter" || e.key === "Tab")) { e.preventDefault(); chatMentionPick(CHAT.mentList[CHAT.mentIdx]); return; }
  if (open && e.key === "Escape") { $("chatMentions").classList.remove("on"); CHAT.mentList = []; return; }
  if (e.key === "Enter") chatSend();
}
/* 첨부 */
async function chatPickFile(e) {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  if (f.size > 8 * 1024 * 1024) { toast("첨부는 8MB 이하만 가능합니다"); return; }
  const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(f); });
  CHAT.pending = { data, name: f.name };
  $("chatFileName").textContent = "📎 " + f.name;
  $("chatFileRow").classList.add("on");
  $("chatInput").focus();
}
function toggleChat() {
  CHAT.open = !CHAT.open;
  $("chatPanel").classList.toggle("on", CHAT.open);
  if (CHAT.open) {
    CHAT.unread = 0; CHAT.mentionUnread = 0; renderChatBadge();
    renderChatNav();
    pollPresence();     // 열자마자 '읽음'을 기록해 상대에게 읽음 표시가 뜨게
    setTimeout(() => { $("chatMsgs").scrollTop = $("chatMsgs").scrollHeight; $("chatInput").focus(); }, 30);
  }
}
function renderChatBadge() {
  const b = $("chatBadge");
  const ment = CHAT.mentionUnread > 0;
  b.textContent = (ment ? "@" : "") + (CHAT.unread > 99 ? "99+" : CHAT.unread);
  b.classList.toggle("on", CHAT.unread > 0 || ment);
  b.classList.toggle("ment", ment);           // 나를 부른 메시지가 있으면 주황
  $("chatToggle").title = ment ? "나를 호출한 메시지가 있습니다" : "";
}
async function pollPresence() {
  try {
    const past = !!CHAT.viewDay;      // 지난 대화 보기 중엔 오늘 메시지를 화면에 그리지 않는다
    const usedAfter = CHAT.lastId;
    // 패널을 열고 오늘 대화를 보고 있으면 '여기까지 읽음'을 같이 기록 (읽음 표시)
    const readQ = (CHAT.open && !past && CHAT.lastId) ? `&read=${CHAT.lastId}` : "";
    // 일일 입력 화면을 보고 있으면 그 날짜를 알려 '동시 편집' 감지에 참여 (다른 화면이면 빠짐)
    const editQ = (document.querySelector("#scr-entry.on") && E.date) ? `&edit=${E.date}` : "";
    let d = await api(`/api/presence?after=${usedAfter}${readQ}${editQ}`);
    if (editQ) {
      const prev = (E.viewers || []).join(",");
      E.viewers = d.viewers || [];
      if (prev !== E.viewers.join(",")) renderEntryViewers();   // 들어오고 나감을 실시간 반영
      // 내가 연 뒤 다른 사람이 저장했으면 알림 (내 저장 중에는 건너뜀 — 곧 loadDay가 버전을 맞춘다)
      if (!SAVING && d.day_ver !== undefined && d.day_ver !== E.version) showDayUpdated(d.day_ver, d.day_by);
    } else if ((E.viewers || []).length) {
      E.viewers = [];
    }
    $("chatCount").textContent = d.count;
    $("chatOnline").textContent = "🟢 " + (d.online.join(", ") || "-");
    CHAT.me = d.me;
    if (d.users) CHAT.users = d.users;
    CHAT.reads = d.reads || {};
    CHAT.mentionUnread = d.mention_unread || 0;
    // 채팅창은 하루 단위 — 날짜가 바뀌면(자정 넘김) 창을 비우고 그날 대화만 새로 시작
    if (d.day && d.day !== CHAT.day) {
      const first = CHAT.day == null;
      CHAT.day = d.day;
      CHAT.lastId = 0;
      if (!past) {
        $("chatMsgs").innerHTML = chatDayHeader(d.day);
        if (!first) { CHAT.unread = 0; }                    // 날짜가 바뀌면 미읽음도 초기화
        // 위 요청은 옛 lastId로 나갔으므로, 그날 대화를 처음부터 다시 받아 채운다
        if (usedAfter !== 0) d = await api("/api/presence?after=0");
      }
      renderChatNav();
    }
    // 기준정보 실시간 동기화 — 다른 접속자가 제품/자재/거래처/배합비 등을 바꾸면 캐시 자동 갱신
    if (d.mver != null) {
      if (CHAT.mver == null) CHAT.mver = d.mver;             // 첫 폴링: 기준값만 기억 (부팅 때 이미 로딩됨)
      else if (d.mver !== CHAT.mver) {
        CHAT.mver = d.mver;
        await loadMasters();
        BOMALL = null; COSTS = null;
        try { ANA.raw = null; } catch (e) {}
        // 기준정보 화면을 보고 있으면 목록도 갱신 (빠른 편집·배합비 편집 중엔 방해하지 않음)
        if (document.querySelector("#scr-items.on") && !mQuick && mTab !== "bom") renderMasters();
      }
    }
    if (past) { renderChatBadge(); return; }   // 지난 대화 보는 중 — 오늘 메시지는 배지로만 알림
    if (d.messages && d.messages.length) {
      const atBottom = (() => { const el = $("chatMsgs"); return el.scrollHeight - el.scrollTop - el.clientHeight < 40; })();
      $("chatMsgs").insertAdjacentHTML("beforeend", d.messages.map(m => chatMsgHtml(m, d.me)).join(""));
      CHAT.lastId = d.last_id;
      const fresh = d.messages.filter(m => m.username !== d.me && m.kind !== "system").length;
      if (!CHAT.open && fresh) CHAT.unread += fresh;
      if (CHAT.open && atBottom) $("chatMsgs").scrollTop = $("chatMsgs").scrollHeight;
    } else if (d.last_id) {
      CHAT.lastId = d.last_id;
    }
    renderChatBadge();
    refreshReadMarks(d.me, d.reads);   // 다른 사람이 읽으면 내 메시지의 '읽음 N'이 늘어난다
  } catch (e) { /* 폴링 일시 오류 무시 */ }
}
async function chatSend() {
  if (CHAT.viewDay) return;                       // 지난 대화 보기는 읽기 전용
  const inp = $("chatInput"), text = inp.value.trim(), file = CHAT.pending;
  if (!text && !file) return;
  inp.value = "";
  CHAT.pending = null; $("chatFileRow").classList.remove("on");
  $("chatMentions").classList.remove("on"); CHAT.mentList = [];
  try {
    await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, file }) });
  } catch (e) {   // 실패하면 입력 내용·첨부를 되돌려 준다
    inp.value = text; CHAT.pending = file;
    if (file) $("chatFileRow").classList.add("on");
    return;
  }
  pollPresence();
}
async function doLogin() {
  $("loginMsg").textContent = "";
  const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: $("loginId").value, password: $("loginPw").value }) });
  if (!r.ok) {
    let msg = "로그인 실패";
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    $("loginMsg").textContent = msg;
    return;
  }
  location.reload();   // 세션 쿠키 반영 후 깨끗하게 재시작
}
$("loginBtn").onclick = doLogin;
$("loginPw").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("loginId").addEventListener("keydown", e => { if (e.key === "Enter") $("loginPw").focus(); });
$("btnLogout").onclick = async () => { await fetch("/api/logout", { method: "POST" }); location.reload(); };
$("btnPw").onclick = () => { $("pwOld").value = ""; $("pwNew").value = ""; $("pwOverlay").classList.add("on"); };
$("pwSaveBtn").onclick = async () => {
  await api("/api/password", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old: $("pwOld").value, new: $("pwNew").value }) });
  $("pwOverlay").classList.remove("on");
  toast("비밀번호가 변경되었습니다");
};

(async function boot() {
  const t = new Date();
  $("todayLbl").textContent = `${todayISO()} (${DOW[t.getDay()]})`;
  const r = await fetch("/api/me");
  if (r.status === 401) { showLogin(); return; }
  await startApp(await r.json());
})();
