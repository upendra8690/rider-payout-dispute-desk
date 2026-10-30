/* Ops console for the Rider Payout Dispute Desk. Vanilla JS; every value from the API is escaped. */
(() => {
  "use strict";

  const S = { token: null, actor: null, queue: "pending", rider: null, tab: "chat", traceFilter: "all",
              detail: null, conversations: [], timer: null };
  const $ = (id) => document.getElementById(id);

  function store(key, value) { try { value == null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch (_) {} }
  function load(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } }

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const rs = (n) => (n == null ? "–" : (Number(n) < 0 ? "−₹" : "₹") + Math.abs(Number(n)).toLocaleString("en-IN"));
  const when = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }); };
  const clock = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleTimeString("en-IN", { hour12: false }); };
  const badge = (v) => `<span class="badge ${esc(String(v).replace(/[^a-z_]/gi, ""))}">${esc(String(v).replace(/_/g, " "))}</span>`;
  const json = (v) => `<pre>${esc(JSON.stringify(v, null, 2))}</pre>`;

  function toast(msg) {
    const t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3500);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-Ops-Token": S.token || "", ...(opts.headers || {}) },
    });
    if (res.status === 401) { showLogin("Token rejected. Check OPS_API_TOKEN."); throw new Error("unauthorized"); }
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.detail) || `HTTP ${res.status}`);
    return body;
  }

  // ------------------------------------------------------------------ auth
  function showLogin(err) {
    $("app").hidden = true; $("login").hidden = false;
    $("login-error").hidden = !err; $("login-error").textContent = err || "";
    $("login-name").value = S.actor || "";
  }
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    S.token = $("login-token").value.trim(); S.actor = $("login-name").value.trim();
    store("ops_token", S.token); store("ops_actor", S.actor);
    try { await api("/ops/stats"); $("login").hidden = true; $("app").hidden = false; refreshAll(); }
    catch (err) { if (err.message !== "unauthorized") showLogin(err.message); }
  });
  $("btn-logout").addEventListener("click", () => { S.token = null; store("ops_token", null); showLogin(); });

  // ------------------------------------------------------------------ KPIs
  async function loadStats() {
    const s = await api("/ops/stats");
    $("k-total").textContent = s.disputes_total;
    $("k-total-foot").textContent = `${s.conversations} conversations · ${s.messages_in} messages`;
    $("k-auto").textContent = s.auto_resolved;
    $("k-auto-foot").textContent = `${s.auto_paid} auto-paid · rest explained`;
    $("k-appr").textContent = s.pending_approvals;
    $("k-appr-foot").textContent = `${rs(s.pending_approval_amount)} waiting`;
    $("k-esc").textContent = s.escalations_open;
    $("k-paid").textContent = rs(s.payouts_processed_amount);
    const modes = s.payouts_by_mode || {};
    $("k-paid-foot").textContent = `${s.payouts_processed_count} payouts · auto ${rs(modes.auto || 0)} · approved ${rs(modes.approved || 0)}`;
    if (s.payswift.reachable) {
      $("k-ps").textContent = rs(s.payswift.total);
      $("k-ps-foot").textContent = `${s.payswift.count} payouts in PaySwift`;
    } else { $("k-ps").textContent = "offline"; $("k-ps-foot").textContent = "cannot reconcile"; }
    const chipPs = $("chip-payswift");
    chipPs.textContent = `PaySwift: ${s.payswift.reachable ? "reachable" : "unreachable"}`;
    chipPs.className = "chip " + (s.payswift.reachable ? "ok" : "bad");
    $("chip-llm").textContent = `interpreter: ${s.policy.llm_provider}`;
    renderDQ(s.data_quality, s.policy);
  }

  function renderDQ(dq, policy) {
    if (!dq) { $("dq").innerHTML = '<p class="empty">No import report.</p>'; return; }
    $("dq").innerHTML = `
      <div><b>${esc(dq.rider_id_normalised_rows)}</b>trip rows with rider ids like ${esc((dq.rider_id_variants || []).join(", "))} mapped to canonical ids</div>
      <div><b>${esc(dq.duplicate_trip_rows_removed)}</b>duplicate trip rows removed</div>
      <div><b>${esc(dq.distance_in_metres_rows)}</b>distances recorded in metres converted to km (${esc((dq.distance_in_metres_riders || []).join(", "))})</div>
      <div><b>${esc(dq.trips_where_ist_day_differs_from_utc_day)}</b>trips whose IST day differs from the UTC day (policy day = IST)</div>
      <div><b>${esc(policy.auto_pay_max_per_dispute)} / ${esc(policy.auto_pay_max_per_rider_per_day)}</b>auto-pay limit ₹ per dispute / per rider per day · window ${esc(policy.dispute_window_days)} days</div>`;
  }

  // ------------------------------------------------------------------ queue
  async function loadQueue() {
    const items = await api(`/ops/items?status=${S.queue}`);
    const el = $("queue");
    if (!items.length) { el.innerHTML = `<p class="empty">${S.queue === "pending" ? "Nothing waiting. 🎉" : "No history yet."}</p>`; return; }
    el.innerHTML = items.map((it) => `
      <div class="item" data-id="${esc(it.id)}">
        <div class="item-row">
          <div>${badge(it.type)} <a href="#" data-rider="${esc(it.rider_id)}" class="rider-link">${esc(it.rider_id)}</a>
            ${it.amount != null ? `<span class="amount">${rs(it.amount)}</span>` : ""}</div>
          <span class="item-meta">${esc(when(it.created_at))}</span>
        </div>
        <p>${esc(it.reason)}</p>
        <div class="item-meta">${esc(it.reason_code)}${it.dispute_id ? " · " + esc(it.dispute_id) : ""}${it.status !== "pending" ? " · " + esc(it.status) + " by " + esc(it.decided_by || "?") + (it.decision_note ? ": " + esc(it.decision_note) : "") : ""}</div>
        ${it.status === "pending" ? `
        <div class="actions" style="margin-top:6px">
          <input type="text" maxlength="500" placeholder="note for the audit trail (optional)" aria-label="note">
          ${it.type === "approval"
            ? `<button class="btn small approve" data-act="approve" type="button">Approve &amp; pay</button>
               <button class="btn small reject" data-act="reject" type="button">Reject</button>`
            : `<button class="btn small" data-act="resolve" type="button">Mark resolved</button>`}
        </div>` : ""}
      </div>`).join("");
  }

  $("queue").addEventListener("click", async (e) => {
    const link = e.target.closest(".rider-link");
    if (link) { e.preventDefault(); openRider(link.dataset.rider); return; }
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const item = btn.closest(".item");
    const note = item.querySelector("input").value.trim() || null;
    const act = btn.dataset.act;
    item.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const res = await api(`/ops/items/${encodeURIComponent(item.dataset.id)}/${act}`,
        { method: "POST", body: JSON.stringify({ actor: S.actor || "ops", note }) });
      toast(act === "approve" ? (res.message || "Approved") : act === "reject" ? "Rejected" : "Resolved");
    } catch (err) { toast(`Failed: ${err.message}`); }
    refreshAll();
  });

  document.querySelectorAll("[data-queue]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-queue]").forEach((x) => x.classList.toggle("active", x === b));
    S.queue = b.dataset.queue; loadQueue().catch(() => {});
  }));

  // ------------------------------------------------------------------ conversations
  async function loadConversations() {
    S.conversations = await api("/ops/conversations");
    renderConversations();
  }
  function renderConversations() {
    const f = $("conv-filter").value.trim().toLowerCase();
    const rows = S.conversations.filter((c) => !f || `${c.rider_id} ${c.rider_name} ${c.last_message}`.toLowerCase().includes(f));
    const el = $("conversations");
    if (!rows.length) { el.innerHTML = '<p class="empty">No conversations yet. Send a message to POST /messages.</p>'; return; }
    el.innerHTML = rows.map((c) => `
      <div class="item conv ${c.rider_id === S.rider ? "active" : ""}" data-rider="${esc(c.rider_id)}">
        <div class="item-row"><strong>${esc(c.rider_id)} · ${esc(c.rider_name || "")}</strong><span class="item-meta">${esc(when(c.last_message_at))}</span></div>
        <div class="last">${esc(c.last_message || "")}</div>
        <div class="item-meta">${esc(c.disputes)} dispute(s) · ${esc(c.language)} ${c.open_ops_items ? " · " + badge(c.open_ops_items + " open") : ""} ${c.status === "closed" ? badge("closed") : ""}</div>
      </div>`).join("");
  }
  $("conv-filter").addEventListener("input", renderConversations);
  $("conversations").addEventListener("click", (e) => { const c = e.target.closest(".conv"); if (c) openRider(c.dataset.rider); });

  // ------------------------------------------------------------------ rider detail
  async function openRider(riderId) {
    S.rider = riderId; renderConversations();
    S.detail = await api(`/ops/riders/${encodeURIComponent(riderId)}`);
    const r = S.detail.rider;
    $("detail-title").textContent = r ? `${r.rider_id} · ${r.name} · ${r.city}` : riderId;
    renderDetail();
  }
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
    S.tab = b.dataset.tab; renderDetail();
  }));

  function renderDetail() {
    const d = S.detail; const el = $("detail");
    if (!d) return;
    const r = { chat: renderChat, disputes: renderDisputes, payments: renderPayments, trace: renderTrace, audit: renderAudit }[S.tab];
    el.innerHTML = r(d);
  }

  function renderChat(d) {
    if (!d.messages.length) return '<p class="empty">No messages.</p>';
    return `<div class="chat">${d.messages.map((m) => `
      <div class="bubble ${m.direction}"><span class="text">${esc(m.text)}</span><span class="meta">${m.direction === "in" ? `rider · ${esc(m.message_id)} · ${esc(when(m.received_at))}${m.delivery_count > 1 ? ` · delivered ${esc(m.delivery_count)}×` : ""}` : "agent"}</span></div>`).join("")}</div>`;
  }

  function verificationTable(v) {
    const items = (v && v.scoped_items) || [];
    if (!items.length) return "";
    return `<table><thead><tr><th>Item</th><th>Finding</th><th class="num">Expected</th><th class="num">Paid (lines)</th><th class="num">Paid (desk)</th><th class="num">Short</th></tr></thead><tbody>
      ${items.map((i) => `<tr><td>${esc(i.trip_id || i.key)}${i.facts && i.facts.surge && i.facts.surge !== "1" ? ` <span class="item-meta">${esc(i.facts.surge)}x</span>` : ""}${i.facts && i.facts.distance_km ? ` <span class="item-meta">${esc(i.facts.distance_km)} km</span>` : ""}</td>
        <td>${badge(i.finding)}</td><td class="num">${rs(i.expected)}</td><td class="num">${rs(i.paid_lines)}</td><td class="num">${rs(i.paid_desk)}</td><td class="num">${rs(i.shortfall)}</td></tr>`).join("")}
    </tbody></table>`;
  }

  function renderDisputes(d) {
    if (!d.disputes.length) return '<p class="empty">No disputes for this rider.</p>';
    return d.disputes.slice().reverse().map((x) => {
      const v = (x.findings || {}).verification || {};
      const day = v.day_summary || {};
      const pays = d.payments.filter((p) => p.dispute_id === x.id);
      return `<div class="card">
        <h3>${esc(x.id)} ${badge(x.status)} <span class="item-meta">day ${esc(x.dispute_date)} · raised ${esc(x.business_date)} · ${esc((x.categories || []).join(", "))}${x.trip_ids && x.trip_ids.length ? " · " + esc(x.trip_ids.join(", ")) : ""}</span></h3>
        <div class="item-meta">“${esc(x.claim_text)}”${x.claimed_amount ? ` · rider claimed ${rs(x.claimed_amount)}` : ""}</div>
        <div class="money">
          <div><span>Expected (scope)</span><b>${rs(x.expected_amount)}</b></div>
          <div><span>Actually paid</span><b>${rs(x.actual_amount)}</b></div>
          <div><span>Verified owed</span><b>${rs(x.owed_amount)}</b></div>
          <div><span>Paid by desk</span><b>${rs(x.paid_amount)}</b></div>
        </div>
        ${day.day ? `<div class="item-meta">Whole day ${esc(day.day)}: ${esc(day.completed_trips)} completed trips · expected ${rs(day.expected_total)} · paid ${rs(day.actual_total)} · net short ${rs(day.net_shortfall)}${v.widened_to_whole_day ? " · widened to whole day" : ""}${v.capped_by_day_net ? " · capped by day net" : ""}</div>` : ""}
        <div class="why"><strong>Decision:</strong> ${esc(x.decision || "–")}: ${esc(x.decision_reason || "")}${x.findings && x.findings.explanation ? `<br>${esc(x.findings.explanation)}` : ""}</div>
        ${verificationTable(v)}
        ${pays.map((p) => `<div class="item-meta">Payment ${esc(p.id)} ${badge(p.status)} ${rs(p.amount)} · ${esc(p.mode)} · ${esc(p.payswift_payout_id || "no payout id yet")} · ${esc(p.attempts)} attempt(s)</div>`).join("")}
        <details><summary>Raw findings</summary>${json(x.findings)}</details>
      </div>`;
    }).join("");
  }

  function renderPayments(d) {
    const live = d.payswift_payouts;
    let html = "";
    if (!d.payments.length) html += '<p class="empty">No payments made by the desk for this rider.</p>';
    html += d.payments.slice().reverse().map((p) => `
      <div class="card">
        <h3>${esc(p.id)} ${badge(p.status)} <span class="amount">${rs(p.amount)}</span> <span class="item-meta">${esc(p.mode)}${p.approved_by ? " · approved by " + esc(p.approved_by) : ""}</span></h3>
        <div class="item-meta">PaySwift payout: <code>${esc(p.payswift_payout_id || "–")}</code> · idempotency key <code>${esc(p.idempotency_key)}</code></div>
        <div class="item-meta">reference <code>${esc(p.reference)}</code></div>
        ${p.last_error ? `<div class="error">${esc(p.last_error)}</div>` : ""}
        <table><thead><tr><th>#</th><th>Kind</th><th>Outcome</th><th>HTTP</th><th class="num">ms</th><th>At</th></tr></thead><tbody>
        ${(p.attempt_log || []).map((a) => `<tr><td>${esc(a.attempt_no)}</td><td>${esc(a.kind)}</td><td>${badge(a.outcome)}${a.error ? ` <span class="item-meta">${esc(a.error)}</span>` : ""}</td><td>${esc(a.http_status ?? "–")}</td><td class="num">${esc(a.duration_ms ?? "")}</td><td>${esc(clock(a.at))}</td></tr>`).join("")}
        </tbody></table>
      </div>`).join("");
    html += `<div class="card"><h3>PaySwift ledger for this rider (live)</h3>${live == null ? '<p class="error">PaySwift unreachable</p>' :
      !live.length ? '<p class="item-meta">No payouts in PaySwift.</p>' :
      `<table><thead><tr><th>Payout</th><th class="num">Amount</th><th>Status</th><th>Reference</th></tr></thead><tbody>${live.map((p) =>
        `<tr><td><code>${esc(p.payout_id)}</code></td><td class="num">${rs(p.amount)}</td><td>${badge(p.status)}</td><td class="item-meta">${esc(p.reference)}</td></tr>`).join("")}</tbody></table>`}</div>`;
    return html;
  }

  function renderTrace(d) {
    const types = ["all", "message_in", "tool_call", "decision", "reply", "error"];
    const steps = d.trace.filter((s) => S.traceFilter === "all" || s.type === S.traceFilter);
    return `<div class="filter-row">${types.map((t) => `<button type="button" class="btn small ${S.traceFilter === t ? "primary" : ""}" data-trace="${t}">${t.replace("_", " ")}</button>`).join("")}</div>
      <ul class="timeline">${steps.map((s) => `
        <li><span class="t">${esc(clock(s.at))}</span><span>${badge(s.type)}</span>
          <div><div class="n">${esc(s.name)}</div>
            ${s.type === "reply" ? `<div>${esc(s.output)}</div>` : s.type === "message_in" ? `<div>${esc((s.input || {}).text)}</div>` : ""}
            <details><summary>input / output</summary>${json({ input: s.input, output: s.output })}</details></div></li>`).join("")}</ul>`;
  }
  $("detail").addEventListener("click", (e) => {
    const b = e.target.closest("[data-trace]");
    if (b) { S.traceFilter = b.dataset.trace; renderDetail(); }
  });

  function renderAudit(d) {
    if (!d.audit.length) return '<p class="empty">No audit entries.</p>';
    return `<table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead><tbody>
      ${d.audit.slice().reverse().map((a) => `<tr><td>${esc(when(a.at))}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td><code>${esc(a.entity_id)}</code></td>
        <td class="item-meta">${esc(JSON.stringify(a.details))}${a.note ? `<br>note: ${esc(a.note)}` : ""}</td></tr>`).join("")}</tbody></table>`;
  }

  // ------------------------------------------------------------------ refresh loop
  async function refreshAll() {
    try {
      await Promise.all([loadStats(), loadQueue(), loadConversations()]);
      if (S.rider) await openRider(S.rider);
    } catch (err) { if (err.message !== "unauthorized") toast(`Refresh failed: ${err.message}`); }
  }
  $("btn-refresh").addEventListener("click", refreshAll);
  function schedule() {
    clearInterval(S.timer);
    if ($("auto-refresh").checked) S.timer = setInterval(() => { if (!document.hidden && !$("app").hidden) refreshAll(); }, 6000);
  }
  $("auto-refresh").addEventListener("change", schedule);

  S.token = load("ops_token"); S.actor = load("ops_actor");
  if (S.token) { api("/ops/stats").then(() => { $("app").hidden = false; refreshAll(); }).catch(() => showLogin()); }
  else showLogin();
  schedule();
})();
