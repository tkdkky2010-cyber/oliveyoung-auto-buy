"use strict";
// 올리브영 자동구매 대시보드. 상태를 바꾸는 요청에는 x-oy-dashboard 헤더를 붙인다(서버가 CSRF 방지로 확인).
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const won = (n) => (n ? Number(n).toLocaleString("ko-KR") + "원" : "");
const STATUS_KO = {
  registered: "등록됨", running: "실행 중", pausing: "일시정지 요청", paused: "일시정지", stopping: "중지 요청",
  stopped: "중지", completed: "완료", completed_with_failures: "완료(실패 있음)", failed: "실패", blocked: "결제 확인 필요",
  pending: "대기", prepared: "점검 완료(결제 안 함)", skipped: "건너뜀", unknown: "결과 불명",
  PENDING: "결제 진행 중", UNKNOWN: "결과 불명", COMPLETE: "결제 완료", NOT_PAID: "결제 안 됨",
};
const MODE_KO = { pay: "실제 결제", prepare: "점검(dry-run)", simulate: "모의" };
let selectedRun = null;
let lastStatus = null;

async function api(path, opts = {}) {
  const o = { ...opts, headers: { "content-type": "application/json", "x-oy-dashboard": "1", ...(opts.headers || {}) } };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
  return data;
}

function pill(id, text, cls) { const el = $(id); el.textContent = text; el.className = "pill " + (cls || ""); }

function renderStatus(st) {
  lastStatus = st;
  pill("#pill-server", "서버 실행 중", "ok");
  const o = st.ocr;
  if (o.ready) pill("#pill-ocr", `OCR 준비 (${o.det_model.replace("PP-OCRv6_", "v6 ").replace("_det", "")})`, "ok");
  else if (o.state === "error") pill("#pill-ocr", "OCR 오류", "bad");
  else pill("#pill-ocr", "OCR 로딩 중…", "warn");
  const a = st.adb || {};
  if (a.error) pill("#pill-adb", "ADB 없음", "bad");
  else if (a.connected) pill("#pill-adb", `기기 연결: ${a.selected}`, "ok");
  else if (a.selected) pill("#pill-adb", `기기 끊김: ${a.selected}`, "bad");
  else pill("#pill-adb", (a.devices || []).length ? "기기 선택 필요" : "기기 없음", "warn");

  $("#ocr-stats").innerHTML = [
    ["검출 모델", o.det_model], ["인식 모델", o.rec_model], ["장치", (o.providers || ["-"])[0]?.replace("ExecutionProvider", "")],
    ["최근 처리시간", o.last_ms ? o.last_ms + " ms" : "-"], ["최근 평균 신뢰도", o.last_conf ? (o.last_conf * 100).toFixed(1) + "%" : "-"],
    ["OCR 호출 / 캐시", `${o.calls ?? 0} / ${o.cache_hits ?? 0}`],
  ].map(([k, v]) => `<div class="stat"><b>${esc(v)}</b><span>${k}</span></div>`).join("") + (o.load_error ? `<div class="muted">${esc(o.load_error)}</div>` : "");
  const t = st.today.real;
  $("#today-stats").innerHTML = `<div class="stat"><b>${t.orders}건</b><span>오늘 결제(실제)</span></div><div class="stat"><b>${won(t.amount) || "0원"}</b><span>오늘 결제금액</span></div>`;

  const unk = st.unknown_payments || [];
  const b = $("#banner-unknown");
  b.classList.toggle("hidden", !unk.length);
  b.textContent = unk.length ? `결제 결과 확인이 필요한 건 ${unk.length}개 — 주문내역 확인 전에는 해당 계정을 다시 결제하지 않습니다.` : "";
  renderUnknown(unk);
  const authJob = (st.runs || []).flatMap((r) => r.jobs).find((j) => ["AUTHENTICATION_REQUIRED", "PAYMENT_AUTH_REQUIRED"].includes(j.state) && j.status === "running");
  const ab = $("#banner-auth");
  ab.classList.toggle("hidden", !authJob);
  if (authJob) ab.textContent = `${authJob.account_no}번 계정: ${authJob.state_label} — 폰에서 직접 인증해 주세요. 완료되면 자동으로 이어갑니다. ${authJob.detail || ""}`;

  if (selectedRun) {
    const snap = (st.runs || []).find((r) => r.run.id === selectedRun);
    if (snap) renderDetail(snap);
  }
}

function renderUnknown(list) {
  const el = $("#unknown-list");
  if (!list.length) { el.innerHTML = '<span class="muted">없음</span>'; return; }
  el.innerHTML = list.map((p) => `<div class="unk" data-id="${p.id}">
    <b>${esc(p.account_no)}번</b> <span>${won(p.amount)}</span> <span class="muted">${esc(p.created_at)} · 작업 #${esc(p.run_id)}</span>
    <span class="muted">${esc(p.note)}</span>
    <input placeholder="주문번호 Y…" class="ord">
    <button class="primary paid">결제됨</button><button class="danger notpaid">결제 안 됨</button></div>`).join("");
  el.querySelectorAll(".unk").forEach((row) => {
    const id = row.dataset.id;
    $(".paid", row).onclick = async () => {
      try { await api(`/api/payments/${id}/resolve`, { method: "POST", body: { paid: true, order_no: $(".ord", row).value.trim() } }); refreshAll(); }
      catch (e) { alert(e.message); }
    };
    $(".notpaid", row).onclick = async () => {
      if (!confirm("주문내역에 이 결제가 없는 것을 확인했나요?")) return;
      try { await api(`/api/payments/${id}/resolve`, { method: "POST", body: { paid: false } }); refreshAll(); }
      catch (e) { alert(e.message); }
    };
  });
}

async function loadRuns() {
  const { runs } = await api("/api/runs");
  const el = $("#runs");
  if (!runs.length) { el.innerHTML = '<span class="muted">등록된 작업 없음</span>'; return; }
  if (!selectedRun) selectedRun = runs[0].id;
  el.innerHTML = runs.map((r) => `<div class="run-row ${r.id === selectedRun ? "sel" : ""}" data-id="${r.id}">
    <b>#${r.id}</b> <span class="st ${esc(r.status)}">${esc(STATUS_KO[r.status] || r.status)}</span>
    <span>${esc(MODE_KO[r.mode] || r.mode)}</span> <span>${r.card === "lotte" ? "롯데" : "현대"}</span>
    <span>계정 ${esc(r.accounts.split(",").length)}개</span> <span class="link">${esc(r.link)}</span>
    <span class="muted">${esc(r.created_at.replace("T", " "))}</span></div>`).join("");
  el.querySelectorAll(".run-row").forEach((row) => row.onclick = () => { selectedRun = Number(row.dataset.id); loadRuns(); loadDetail(); });
}

async function loadDetail() {
  if (!selectedRun) return;
  try { renderDetail(await api(`/api/runs/${selectedRun}`)); } catch (e) { /* 지워진 작업 */ }
}

function renderDetail(snap) {
  const r = snap.run;
  $("#detail-title").textContent = `#${r.id} · ${MODE_KO[r.mode]} · ${STATUS_KO[r.status] || r.status}`;
  $("#progress-bar").style.width = r.progress + "%";
  const jobs = snap.jobs;
  const count = (st) => jobs.filter((j) => j.status === st).length;
  const resting = r.current && r.current.state === "REST";
  const cur = r.current && !resting ? `${r.current.account_no}번` : "-";
  const curJob = r.current && !resting ? jobs.find((j) => j.id === r.current.job_id) : null;
  const step = resting ? `다음 계정까지 ${Math.max(0, Math.ceil(r.current.rest_until - Date.now() / 1000))}초 대기` : (curJob ? curJob.state_label : "-");
  $("#detail-stats").innerHTML = [
    ["진행률", r.progress + "%"], ["현재 계정", cur], ["현재 단계", step],
    ["성공", count("completed")], ["점검 완료", count("prepared")], ["실패", count("failed")], ["결과 불명", count("unknown")],
    ["건너뜀", count("skipped")], ["총 결제금액", won(r.total_amount) || "0원"],
  ].map(([k, v]) => `<div class="stat"><b>${esc(v)}</b><span>${k}</span></div>`).join("") + (r.error ? `<div class="muted">${esc(r.error)}</div>` : "");
  const btn = (act, label, cls, on) => `<button data-act="${act}" class="${cls || ""}" ${on ? "" : "disabled"}>${label}</button>`;
  const s = r.status;
  $("#detail-controls").innerHTML =
    btn("start", "실행", "primary", s === "registered") +
    btn("pause", "일시정지", "", s === "running") +
    btn("resume", "재개", "", ["paused", "pausing", "failed"].includes(s)) +
    btn("stop", "중지", "danger", !["completed", "stopped", "completed_with_failures"].includes(s)) +
    btn("retry", "실패 계정 다시 대기열로", "", ["failed", "completed_with_failures", "stopped", "paused"].includes(s) && !r.alive);
  $("#detail-controls").querySelectorAll("button").forEach((b) => b.onclick = () => control(r, b.dataset.act));
  $("#jobs tbody").innerHTML = jobs.map((j) => `<tr class="${r.current && r.current.job_id === j.id ? "current" : ""}">
    <td>${j.seq}</td><td>${esc(j.account_no)}번</td><td class="st ${esc(j.status)}">${esc(STATUS_KO[j.status] || j.status)}</td>
    <td>${esc(j.state_label)}</td><td>${esc(j.order_no)}</td><td class="num">${won(j.amount)}</td>
    <td>${esc(j.error || j.detail)}</td></tr>`).join("");
}

async function control(run, act) {
  if (act === "start" && run.mode === "pay" &&
      !confirm(`실제 결제를 시작합니다.\n계정 ${run.accounts.split(",").length}개 · ${run.card === "lotte" ? "롯데카드" : "현대카드"}\n계속할까요?`)) return;
  if (act === "stop" && !confirm("중지할까요? 결제 진행 중이면 결과를 확인한 뒤 멈춥니다.")) return;
  try { renderDetail(await api(`/api/runs/${run.id}/${act}`, { method: "POST" })); loadRuns(); }
  catch (e) { alert(e.message); }
}

async function loadDevices() {
  const d = await api("/api/devices");
  const el = $("#devices");
  if (d.error) { el.innerHTML = `<span class="muted">${esc(d.error)}</span>`; return; }
  if (!d.devices.length) { el.innerHTML = '<span class="muted">연결된 기기 없음 — USB 디버깅을 켜고 연결하거나 무선 ADB 주소를 입력하세요.</span>'; return; }
  el.innerHTML = `<table><thead><tr><th>serial</th><th>상태</th><th>모델</th><th>연결</th><th></th></tr></thead><tbody>${d.devices.map((x) => `<tr>
    <td>${esc(x.serial)}</td><td>${esc(x.state)}</td><td>${esc(x.model)}</td><td>${x.transport === "wifi" ? "무선" : "USB"}</td>
    <td>${x.serial === d.selected ? "<b>사용 중</b>" + (d.size ? ` (${d.size.join("x")})` : "") : `<button class="small" data-s="${esc(x.serial)}" ${x.state === "device" ? "" : "disabled"}>사용</button>`}</td></tr>`).join("")}</tbody></table>`;
  el.querySelectorAll("button[data-s]").forEach((b) => b.onclick = async () => {
    try { await api("/api/devices/select", { method: "POST", body: { serial: b.dataset.s } }); loadDevices(); } catch (e) { alert(e.message); }
  });
}

async function loadOrders() {
  const { orders } = await api("/api/orders");
  $("#orders tbody").innerHTML = orders.map((p) => `<tr><td>${esc(p.created_at.replace("T", " "))}</td><td>#${esc(p.run_id)}</td>
    <td>${esc(p.account_no)}번</td><td class="st ${esc(p.status)}">${esc(STATUS_KO[p.status] || p.status)}</td><td>${esc(p.order_no)}</td>
    <td class="num">${won(p.amount)}</td><td>${p.card === "lotte" ? "롯데" : p.card === "hyundai" ? "현대" : esc(p.card)}</td><td>${esc(p.note)}</td></tr>`).join("")
    || '<tr><td colspan="8" class="muted">없음</td></tr>';
}

async function loadAccounts() {
  const { accounts, error } = await api("/api/accounts");
  $("#acct-hint").textContent = error ? error : `accounts.json: ${accounts.length}개 (${accounts.map((a) => a.no).join(", ")})`;
}

function addLog(ev) {
  const log = $("#log");
  const line = document.createElement("div");
  line.className = ev.level === "error" ? "error" : ev.level === "warn" ? "warn" : ev.kind === "state" ? "state" : "";
  line.dataset.level = ev.level;
  line.textContent = `${ev.ts.slice(11)} ${ev.run_id ? "#" + ev.run_id + " " : ""}${ev.message}`;
  if ($("#log-errors").checked && ev.level !== "error") line.style.display = "none";
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.appendChild(line);
  while (log.childNodes.length > 1500) log.removeChild(log.firstChild);
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (ev.kind === "run" || ev.kind === "control" || ev.kind === "payment") { loadRuns(); loadOrders(); }
  if (ev.run_id && ev.run_id === selectedRun && ev.kind !== "log") scheduleDetail();
}

let detailTimer = null;
function scheduleDetail() {   // 상태 이벤트가 몰려도 0.3초에 한 번만 다시 읽는다
  if (detailTimer) return;
  detailTimer = setTimeout(() => { detailTimer = null; loadDetail(); }, 300);
}

function connectStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => pill("#pill-stream", "실시간 연결", "ok");
  es.addEventListener("status", (e) => renderStatus(JSON.parse(e.data)));
  es.addEventListener("log", (e) => addLog(JSON.parse(e.data)));
  es.onerror = () => { pill("#pill-stream", "실시간 재연결 중…", "warn"); pill("#pill-server", "서버 응답 없음", "bad"); };
}

function refreshAll() { loadRuns(); loadDetail(); loadOrders(); loadDevices(); }

$("#form-run").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    link: f.link.value.trim(), accounts: f.accounts.value.trim(), card: f.card.value, mode: f.mode.value,
    option: f.option.value ? Number(f.option.value) : null, quantity: Number(f.quantity.value || 1),
    max_amount_per_account: Number(f.max_amount_per_account.value || 0), max_total_amount: Number(f.max_total_amount.value || 0),
    address_keyword: f.address_keyword.value.trim(),
    pin_mode: f.pin_mode.value, account_delay: Number(f.account_delay.value || 0), stop_on_failure: f.stop_on_failure.checked, allow_repeat: f.allow_repeat.checked,
  };
  try {
    const snap = await api("/api/runs", { method: "POST", body });
    selectedRun = snap.run.id;
    $("#form-msg").textContent = `작업 #${snap.run.id} 등록됨 — '실행'을 눌러 시작하세요.`;
    loadRuns(); renderDetail(snap);
  } catch (err) { $("#form-msg").textContent = "등록 실패: " + err.message; }
};
$("#form-connect").onsubmit = async (e) => {
  e.preventDefault();
  try { const r = await api("/api/devices/connect", { method: "POST", body: { address: e.target.address.value.trim() } }); alert(r.ok ? "연결됨" : "연결 실패"); loadDevices(); }
  catch (err) { alert(err.message); }
};
$("#btn-refresh-runs").onclick = loadRuns;
$("#btn-devices").onclick = loadDevices;
$("#btn-orders").onclick = loadOrders;
$("#log-errors").onchange = (e) => $("#log").querySelectorAll("div").forEach((d) => d.style.display = e.target.checked && d.dataset.level !== "error" ? "none" : "");

function showView() {
  const v = (location.hash || "#main").slice(1);
  const view = ["main", "log", "orders"].includes(v) ? v : "main";
  document.querySelectorAll(".view").forEach((m) => m.classList.toggle("hidden", m.id !== "view-" + view));
  document.querySelectorAll("#menu a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  if (view === "orders") loadOrders();
  if (view === "log") { const l = $("#log"); l.scrollTop = l.scrollHeight; }
}
function toggleMenu(open) {
  const m = $("#menu"), b = $("#menu-btn");
  const o = open ?? m.classList.contains("hidden");
  m.classList.toggle("hidden", !o);
  b.setAttribute("aria-expanded", String(o));
}
$("#menu-btn").onclick = (e) => { e.stopPropagation(); toggleMenu(); };
$("#menu").onclick = () => toggleMenu(false);
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) toggleMenu(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleMenu(false); });
window.addEventListener("hashchange", showView);
showView();

api("/api/status").then(renderStatus).catch(() => pill("#pill-server", "서버 응답 없음", "bad"));
refreshAll();
loadAccounts();
connectStream();
