// Expense Requests — vanilla JS SPA.
// The server owns validation; here we mirror rules for UX and show server errors verbatim.

const state = {
  meta: null,
  users: [],
  me: null,
  requests: [],
  editing: null,   // request object when editing, else null
  detailId: null,
};

// -------- API helpers --------

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.me) headers["X-User-Id"] = state.me.id;
  const res = await fetch(path, { ...opts, headers });
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(body?.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

const money = (cents) => (cents == null ? "" : `$${(cents / 100).toFixed(2)}`);
const centsFromInput = (str) => {
  if (str === "" || str == null) return null;
  const n = Number(str);
  if (!Number.isFinite(n)) return NaN;
  return Math.round(n * 100);
};
const userName = (id) => state.users.find((u) => u.id === id)?.name || id;

// -------- Boot --------

async function boot() {
  state.meta = await fetch("/api/meta").then((r) => r.json());
  state.users = await fetch("/api/users").then((r) => r.json());

  const sel = document.getElementById("user-select");
  state.users.forEach((u) => {
    const opt = document.createElement("option");
    opt.value = u.id;
    opt.textContent = `${u.name} (${u.role})`;
    sel.appendChild(opt);
  });
  sel.value = localStorage.getItem("as") || state.users[0].id;
  await switchUser(sel.value);
  sel.addEventListener("change", (e) => switchUser(e.target.value));

  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.addEventListener("click", () => showTab(b.dataset.tab))
  );
  document.getElementById("filter-status").addEventListener("change", renderList);
  document.getElementById("filter-mine").addEventListener("change", renderList);
  document.getElementById("filter-todo").addEventListener("change", renderList);
  document.getElementById("btn-save").addEventListener("click", () => saveForm(false));
  document.getElementById("btn-submit").addEventListener("click", () => saveForm(true));
  document.getElementById("back-to-list").addEventListener("click", () => showTab("list"));

  renderForm();
  await refreshRequests();
}

async function switchUser(id) {
  localStorage.setItem("as", id);
  state.me = state.users.find((u) => u.id === id);
  document.getElementById("user-role").textContent = state.me.role;
  await refreshRequests();
  if (state.detailId) renderDetail();
}

// -------- Tabs --------

function showTab(name) {
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name && name !== "detail")
  );
  document.getElementById(`tab-${name}`).classList.add("active");
  if (name === "new" && !state.editing) {
    // Fresh form
    resetForm();
  }
}

// -------- List --------

async function refreshRequests() {
  state.requests = await api("/api/requests");
  renderList();
}

function renderList() {
  const status = document.getElementById("filter-status").value;
  const mine = document.getElementById("filter-mine").checked;
  const todo = document.getElementById("filter-todo").checked;

  const tbody = document.querySelector("#requests-table tbody");
  tbody.innerHTML = "";
  const rows = state.requests.filter((r) => {
    if (status && r.status !== status) return false;
    if (mine && r.requesterId !== state.me.id) return false;
    if (todo && !(r.status === "Submitted" && r.currentApproverId === state.me.id)) return false;
    return true;
  });
  if (rows.length === 0) {
    const tr = tbody.insertRow();
    const td = tr.insertCell();
    td.colSpan = 7;
    td.textContent = "No requests match the current filters.";
    td.style.textAlign = "center";
    td.style.color = "#888";
    return;
  }
  rows.forEach((r) => {
    const tr = tbody.insertRow();
    tr.className = "clickable";
    tr.addEventListener("click", () => openDetail(r.id));
    tr.insertCell().textContent = r.id;
    tr.insertCell().textContent = userName(r.requesterId);
    tr.insertCell().textContent = r.values.expenseType || "—";
    tr.insertCell().textContent = money(r.values.amountCents);
    const s = tr.insertCell();
    s.innerHTML = `<span class="status status-${r.status}">${r.status}</span>`;
    tr.insertCell().textContent = r.currentApproverId ? userName(r.currentApproverId) : "";
    tr.insertCell().textContent = "›";
  });
}

// -------- Form --------

function currentFormValues() {
  const v = {};
  const f = document.getElementById("request-form");
  v.expenseType = f.elements.expenseType.value || null;
  const amountRaw = f.elements.amountCents.value;
  v.amountCents = amountRaw === "" ? null : centsFromInput(amountRaw);
  v.description = f.elements.description.value;
  v.billable = f.elements.billable.checked;
  if (v.billable) v.client = f.elements.client.value || null;
  if (v.expenseType === "Other") v.otherReason = f.elements.otherReason.value;
  if (v.amountCents != null && Number.isFinite(v.amountCents) && v.amountCents >= state.meta.largeAmountCents) {
    v.additionalJustification = f.elements.additionalJustification.value;
  }
  return v;
}

function resetForm() {
  state.editing = null;
  document.getElementById("form-title").textContent = "New Request";
  renderForm({ expenseType: "", amountCents: "", description: "", billable: false });
}

function renderForm(prefill) {
  const values = prefill ?? (state.editing ? state.editing.values : {});
  const f = document.getElementById("request-form");
  const large = state.meta.largeAmountCents;
  const isOther = values.expenseType === "Other";
  const isBillable = !!values.billable;
  const amtDollars = values.amountCents == null || values.amountCents === "" ? "" : (values.amountCents / 100).toFixed(2);
  const showJust = Number.isFinite(Number(values.amountCents)) && Number(values.amountCents) >= large;

  f.innerHTML = `
    <div class="field" data-field="expenseType">
      <label>Expense type *
        <select name="expenseType">
          <option value="">— select —</option>
          ${state.meta.expenseTypes.map((t) => `<option${values.expenseType === t ? " selected" : ""}>${t}</option>`).join("")}
        </select>
      </label>
    </div>
    <div class="field" data-field="amountCents">
      <label>Amount (USD) *
        <input type="number" step="0.01" min="0" name="amountCents" value="${amtDollars}" />
        <span class="hint">Stored as whole cents.</span>
      </label>
    </div>
    <div class="field" data-field="description">
      <label>Description *
        <textarea name="description">${escapeHtml(values.description || "")}</textarea>
      </label>
    </div>
    <div class="field" data-field="billable">
      <label style="flex-direction: row; align-items: center; gap: 6px;">
        <input type="checkbox" name="billable"${isBillable ? " checked" : ""} />
        Billable to a client?
      </label>
    </div>
    <div class="field" data-field="client" style="${isBillable ? "" : "display:none"}">
      <label>Client *
        <select name="client">
          <option value="">— select —</option>
          ${state.meta.clients.map((c) => `<option${values.client === c ? " selected" : ""}>${c}</option>`).join("")}
        </select>
      </label>
    </div>
    <div class="field" data-field="otherReason" style="${isOther ? "" : "display:none"}">
      <label>Other reason *
        <input type="text" name="otherReason" value="${escapeHtml(values.otherReason || "")}" />
      </label>
    </div>
    <div class="field" data-field="additionalJustification" style="${showJust ? "" : "display:none"}">
      <label>Extra justification * <span class="hint">(required at $${(large / 100).toFixed(0)} or more)</span>
        <textarea name="additionalJustification">${escapeHtml(values.additionalJustification || "")}</textarea>
      </label>
    </div>
  `;

  // Wire conditionals: re-render on relevant changes
  ["expenseType", "billable", "amountCents"].forEach((name) => {
    f.elements[name].addEventListener("change", () => renderForm(currentFormValues()));
  });
  f.elements.amountCents.addEventListener("input", () => {
    // Re-render only when crossing the threshold, to avoid stealing focus
    const cur = centsFromInput(f.elements.amountCents.value);
    const showing = f.querySelector('[data-field="additionalJustification"]').style.display !== "none";
    const shouldShow = Number.isFinite(cur) && cur >= large;
    if (showing !== shouldShow) renderForm(currentFormValues());
  });

  document.getElementById("form-error").textContent = "";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
  }[c]));
}

function showFieldErrors(errors) {
  document.querySelectorAll(".field").forEach((el) => {
    el.classList.remove("has-error");
    const existing = el.querySelector(".field-error");
    if (existing) existing.remove();
  });
  Object.entries(errors || {}).forEach(([field, msg]) => {
    const el = document.querySelector(`.field[data-field="${field}"]`);
    if (!el) return;
    el.classList.add("has-error");
    const span = document.createElement("span");
    span.className = "field-error";
    span.textContent = msg;
    el.appendChild(span);
  });
}

async function saveForm(submitAfter) {
  document.getElementById("form-error").textContent = "";
  showFieldErrors({});
  const values = currentFormValues();

  try {
    let req;
    if (state.editing) {
      req = await api(`/api/requests/${state.editing.id}`, {
        method: "PATCH",
        body: JSON.stringify({ values }),
      });
    } else {
      req = await api("/api/requests", {
        method: "POST",
        body: JSON.stringify({ values }),
      });
    }
    state.editing = req;
    if (submitAfter) {
      req = await api(`/api/requests/${req.id}/submit`, { method: "POST" });
      state.editing = null;
      await refreshRequests();
      openDetail(req.id);
      return;
    }
    document.getElementById("form-title").textContent = `Draft ${req.id}`;
    await refreshRequests();
  } catch (e) {
    if (e.body?.errors) {
      showFieldErrors(e.body.errors);
      document.getElementById("form-error").textContent = "Please fix the highlighted fields.";
    } else {
      document.getElementById("form-error").textContent = e.message;
    }
  }
}

// -------- Detail --------

function openDetail(id) {
  state.detailId = id;
  showTab("detail");
  renderDetail();
}

async function renderDetail() {
  const r = await api(`/api/requests/${state.detailId}`);
  document.getElementById("detail-title").textContent = `${r.id} — ${r.values.expenseType || "(no type)"}`;
  const body = document.getElementById("detail-body");
  body.innerHTML = `
    <dl>
      <dt>Status</dt><dd><span class="status status-${r.status}">${r.status}</span></dd>
      <dt>Requester</dt><dd>${userName(r.requesterId)}</dd>
      <dt>Amount</dt><dd>${money(r.values.amountCents)}</dd>
      <dt>Description</dt><dd>${escapeHtml(r.values.description || "")}</dd>
      ${r.values.billable ? `<dt>Client</dt><dd>${escapeHtml(r.values.client || "")}</dd>` : ""}
      ${r.values.otherReason ? `<dt>Other reason</dt><dd>${escapeHtml(r.values.otherReason)}</dd>` : ""}
      ${r.values.additionalJustification ? `<dt>Extra justification</dt><dd>${escapeHtml(r.values.additionalJustification)}</dd>` : ""}
      ${r.currentApproverId ? `<dt>Awaiting</dt><dd>${userName(r.currentApproverId)}</dd>` : ""}
    </dl>
  `;
  const hist = document.getElementById("detail-history");
  hist.innerHTML = r.events.map((ev) => {
    const who = userName(ev.actorId);
    const when = new Date(ev.at).toLocaleString();
    let text = `<b>${ev.type}</b> by ${who} at ${when}`;
    if (ev.type === "submitted" && ev.approverId) text += ` → routed to ${userName(ev.approverId)}`;
    return `<li>${text}</li>`;
  }).join("");

  // Actions
  const actions = document.getElementById("detail-actions");
  actions.innerHTML = "";
  document.getElementById("detail-error").textContent = "";
  const canEdit = r.status === "Draft" && r.requesterId === state.me.id;
  const canDecide = r.status === "Submitted" && r.currentApproverId === state.me.id;

  if (canEdit) {
    addAction(actions, "Edit", () => {
      state.editing = r;
      document.getElementById("form-title").textContent = `Editing ${r.id}`;
      renderForm();
      showTab("new");
    });
    addAction(actions, "Submit", async () => {
      try {
        await api(`/api/requests/${r.id}/submit`, { method: "POST" });
        await refreshRequests();
        renderDetail();
      } catch (e) {
        const msg = e.body?.errors
          ? "Cannot submit: " + Object.entries(e.body.errors).map(([f, m]) => `${f}: ${m}`).join("; ")
          : e.message;
        document.getElementById("detail-error").textContent = msg;
      }
    });
  }
  if (canDecide) {
    addAction(actions, "Approve", () => decide(r.id, "approve"));
    addAction(actions, "Reject", () => decide(r.id, "reject"));
  }
}

function addAction(container, label, handler) {
  const b = document.createElement("button");
  b.textContent = label;
  b.addEventListener("click", handler);
  container.appendChild(b);
}

async function decide(id, action) {
  try {
    await api(`/api/requests/${id}/${action}`, { method: "POST" });
    await refreshRequests();
    renderDetail();
  } catch (e) {
    document.getElementById("detail-error").textContent = e.message;
  }
}

boot();
