// Settings/wizard behaviour: draggable per-provider model lists (per-model batch
// + daily limit), the OpenRouter model browser, provider tabs, key tests, the
// path-mapping table, and auto-save. Each block no-ops if its markup isn't present.

// ── Model widgets (one per AI provider) ──────────────────────────────────────
function initModelWidget(root) {
  const ul = root.querySelector(".model-list");
  if (!ul) return;
  const DEFAULT_BATCH = parseInt(root.dataset.defaultBatch, 10) || 150;
  const DEFAULT_LIMIT = parseInt(root.dataset.defaultLimit, 10) || 18;
  const modelsUrl = root.dataset.modelsUrl;
  const keyName = root.dataset.keyField;
  const isBrowser = root.dataset.browser === "1";
  const modelsField = root.querySelector(".models-field");
  const batchField = root.querySelector(".batch-field");
  const limitField = root.querySelector(".limit-field");
  let dragEl = null;

  const urlName = root.dataset.urlField;
  function keyValue() {
    const el = keyName ? document.querySelector("[name=" + keyName + "]") : null;
    return el ? el.value : "";
  }
  function urlValue() {
    const el = urlName ? document.querySelector("[name=" + urlName + "]") : null;
    return el ? el.value : "";
  }
  function sync(notify) {
    const items = [...ul.querySelectorAll("li")];
    modelsField.value = JSON.stringify(items.map((li) => li.dataset.model));
    const batch = {}, limit = {};
    items.forEach((li) => {
      batch[li.dataset.model] = parseInt(li.querySelector(".mbatch").value, 10) || DEFAULT_BATCH;
      limit[li.dataset.model] = parseInt(li.querySelector(".mlimit").value, 10) || DEFAULT_LIMIT;
    });
    batchField.value = JSON.stringify(batch);
    limitField.value = JSON.stringify(limit);
    if (notify) modelsField.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wire(li) {
    li.draggable = true;
    li.addEventListener("dragstart", () => { dragEl = li; li.classList.add("drag"); });
    li.addEventListener("dragend", () => { li.classList.remove("drag"); sync(true); });
    li.querySelector(".x").addEventListener("click", () => { li.remove(); sync(true); markBrowser(); });
    li.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("input", () => sync(true));
      inp.addEventListener("mousedown", () => { li.draggable = false; });
      inp.addEventListener("mouseup", () => { li.draggable = true; });
      inp.addEventListener("blur", () => { li.draggable = true; });
    });
  }
  function exists(name) {
    return [...ul.querySelectorAll("li")].some((li) => li.dataset.model === name);
  }
  function makeLi(name) {
    const li = document.createElement("li");
    li.dataset.model = name;
    li.innerHTML = '<span class="grip">⠿</span><span class="mname"></span>' +
      '<input type="number" class="mbatch" min="20" step="10" title="batch size (cues per request)">' +
      '<input type="number" class="mlimit" min="1" title="daily request limit">' +
      '<button type="button" class="x" title="Remove">×</button>';
    li.querySelector(".mname").textContent = name;
    li.querySelector(".mbatch").value = DEFAULT_BATCH;
    li.querySelector(".mlimit").value = DEFAULT_LIMIT;
    wire(li);
    return li;
  }
  function add(name) {
    name = (name || "").trim();
    if (name && !exists(name)) { ul.appendChild(makeLi(name)); sync(true); }
  }
  function remove(name) {
    [...ul.querySelectorAll("li")].forEach((li) => { if (li.dataset.model === name) li.remove(); });
    sync(true);
  }

  ul.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (!dragEl) return;
    const after = [...ul.querySelectorAll("li:not(.drag)")].reduce((closest, child) => {
      const box = child.getBoundingClientRect();
      const off = e.clientY - box.top - box.height / 2;
      return (off < 0 && off > closest.off) ? { off, el: child } : closest;
    }, { off: -Infinity }).el;
    if (after) ul.insertBefore(dragEl, after); else ul.appendChild(dragEl);
  });
  ul.querySelectorAll("li").forEach(wire);
  sync(false);

  const input = root.querySelector(".model-input");
  const msg = root.querySelector(".model-msg");
  root.querySelector(".model-add-btn").addEventListener("click", () => { add(input.value); input.value = ""; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(input.value); input.value = ""; } });

  // ── OpenRouter-style browser (Free/Paid groups + search) ──
  const browser = root.querySelector(".or-browser");
  const results = browser ? browser.querySelector(".or-results") : null;
  const searchEl = browser ? browser.querySelector(".or-search") : null;

  function markBrowser() {
    if (!results) return;
    results.querySelectorAll(".or-row").forEach((r) => r.classList.toggle("added", exists(r.dataset.id)));
  }
  function renderGroup(title, list) {
    if (!list.length) return "";
    const rows = list.map((m) =>
      '<button type="button" class="or-row" data-id="' + m.id.replace(/"/g, "&quot;") + '">' +
      '<span class="or-id"></span><span class="or-name"></span></button>').join("");
    return '<div class="or-group">' + title + ' <span class="hint">(' + list.length + ')</span></div>' + rows;
  }
  function applyFilter() {
    const q = (searchEl.value || "").toLowerCase();
    let groupVisible = {};
    results.querySelectorAll(".or-row").forEach((r) => {
      const hit = !q || r.dataset.id.toLowerCase().includes(q) || (r.dataset.name || "").toLowerCase().includes(q);
      r.style.display = hit ? "" : "none";
    });
    // Hide a group header if everything under it is filtered out.
    let rowsAfter = [];
    [...results.children].reverse().forEach((el) => {
      if (el.classList.contains("or-group")) {
        const anyVisible = rowsAfter.some((r) => r.style.display !== "none");
        el.style.display = anyVisible ? "" : "none";
        rowsAfter = [];
      } else { rowsAfter.push(el); }
    });
  }
  if (browser) {
    searchEl.addEventListener("input", (e) => { e.stopPropagation(); applyFilter(); });
    searchEl.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
  }

  root.querySelector(".model-refresh-btn").addEventListener("click", async () => {
    msg.textContent = "Fetching…"; msg.style.color = "";
    try {
      const r = await fetch(modelsUrl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: keyValue(), base_url: urlValue() }),
      });
      const d = await r.json();
      if (!d.ok) { msg.textContent = "✗ " + d.error; msg.style.color = "var(--red)"; return; }

      if (isBrowser) {
        results.innerHTML = renderGroup("Free", d.free || []) + renderGroup("Paid", d.paid || []);
        (d.free || []).concat(d.paid || []).forEach((m, i) => {
          const row = results.querySelectorAll(".or-row")[i];
          row.dataset.name = m.name || m.id;
          row.querySelector(".or-id").textContent = m.id;
          row.querySelector(".or-name").textContent = m.name && m.name !== m.id ? m.name : "";
          row.addEventListener("click", () => {
            if (exists(m.id)) remove(m.id); else add(m.id);
            markBrowser();
          });
        });
        markBrowser();
        applyFilter();
        browser.hidden = false;
        searchEl.focus();
        msg.textContent = "✓ " + (d.count || 0) + " models · " + (d.free || []).length + " free";
        msg.style.color = "var(--green)";
      } else {
        let added = 0, present = 0;
        (d.models || []).forEach((m) => { if (exists(m)) { present++; } else { ul.appendChild(makeLi(m)); added++; } });
        sync(true);
        msg.textContent = "✓ " + (d.models || []).length + " available · " + added + " added · " + present + " already in list";
        msg.style.color = "var(--green)";
      }
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; }
  });
}
document.querySelectorAll(".model-widget").forEach(initModelWidget);

// ── Provider tabs (Gemini / OpenRouter …) ────────────────────────────────────
(function () {
  const tabs = [...document.querySelectorAll(".provider-tab")];
  if (!tabs.length) return;
  tabs.forEach((tab) => tab.addEventListener("click", () => {
    const name = tab.dataset.providerTab;
    tabs.forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll("[data-provider-panel]").forEach((p) => {
      p.hidden = p.dataset.providerPanel !== name;
    });
  }));
})();

// ── Provider key tests (Gemini / OpenRouter) ─────────────────────────────────
document.querySelectorAll("[data-test-provider]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const provider = btn.dataset.testProvider;
    const msg = btn.parentElement.querySelector(".test-msg");
    const keyEl = document.querySelector("[name=" + provider + "_api_key]");
    const urlName = btn.dataset.urlField;
    const urlEl = urlName ? document.querySelector("[name=" + urlName + "]") : null;
    msg.textContent = "Testing…"; msg.style.color = "";
    try {
      const r = await fetch(btn.dataset.testUrl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: keyEl ? keyEl.value : "", base_url: urlEl ? urlEl.value : "" }),
      });
      const d = await r.json();
      msg.textContent = (d.ok ? "✓ " : "✗ ") + d.message;
      msg.style.color = d.ok ? "var(--green)" : "var(--red)";
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; }
  });
});

// ── Per-field API key save ───────────────────────────────────────────────────
// API keys aren't autosaved; each saves on its own button so a half-typed key
// is never written. The value stays in the field afterwards so Test / model
// browse keep working without a reload.
document.querySelectorAll("[data-secret]").forEach((inp) => {
  const wrap = inp.closest(".secret");
  if (!wrap) return;
  const btn = wrap.querySelector(".secret-save");
  const msg = wrap.querySelector(".secret-msg");
  inp.addEventListener("input", () => { btn.disabled = inp.value.trim() === ""; msg.textContent = ""; });
  btn.addEventListener("click", async () => {
    const value = inp.value.trim();
    if (!value) return;
    btn.disabled = true; msg.textContent = "Saving…"; msg.style.color = "";
    try {
      const r = await fetch("/api/secret", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ field: inp.name, value }),
      });
      const d = await r.json();
      msg.textContent = (d.ok ? "✓ " : "✗ ") + d.message;
      msg.style.color = d.ok ? "var(--green)" : "var(--red)";
      if (d.ok) { inp.placeholder = "••••••••"; } else { btn.disabled = false; }
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; btn.disabled = false; }
  });
});

// ── Path mapping table ───────────────────────────────────────────────────────
(function () {
  const table = document.getElementById("remap-table");
  if (!table) return;
  const body = document.getElementById("remap-body");
  const field = document.getElementById("remap-field");
  const current = {};
  try { (JSON.parse(table.dataset.current) || []).forEach((r) => { current[r.from] = r.to; }); } catch (e) { /* none */ }

  function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
  function sync(notify) {
    const rules = [];
    body.querySelectorAll("tr[data-arr]").forEach((tr) => {
      const arrPath = tr.dataset.arr, loc = tr.querySelector("input").value.trim();
      if (loc && loc !== arrPath) rules.push(arrPath + " => " + loc);
    });
    field.value = rules.join("\n");
    if (notify) field.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function row(arrPath) {
    const tr = document.createElement("tr");
    tr.dataset.arr = arrPath;
    tr.innerHTML = '<td class="mname">' + esc(arrPath) + '</td><td><input type="text"></td>';
    const inp = tr.querySelector("input");
    inp.value = current[arrPath] || arrPath;
    inp.addEventListener("input", () => sync(true));
    return tr;
  }
  function build(folders) {
    const all = [...folders];
    Object.keys(current).forEach((k) => { if (!all.includes(k)) all.push(k); });
    body.innerHTML = "";
    if (!all.length) { body.innerHTML = '<tr><td colspan="2" class="hint" style="padding:12px">No folders detected — is Sonarr/Radarr connected?</td></tr>'; return; }
    all.forEach((f) => body.appendChild(row(f)));
    sync(false);
  }
  async function detect() {
    const msg = document.getElementById("remap-msg");
    msg.textContent = "Detecting…"; msg.style.color = "";
    try {
      const r = await fetch("/api/arr/rootfolders", { method: "POST", headers: { "X-Requested-With": "fetch" } });
      const d = await r.json();
      build(d.folders || []);
      msg.textContent = "✓ " + (d.folders || []).length + " folder(s)";
      msg.style.color = "var(--green)";
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; }
  }
  document.getElementById("remap-detect").addEventListener("click", detect);
  detect();
})();

// ── Auto-save ────────────────────────────────────────────────────────────────
(function () {
  const form = document.querySelector("form[data-autosave]");
  if (!form) return;
  const status = document.getElementById("save-status");
  let timer = null, toastEl = null, toastTimer = null;
  function show(text, color) { if (status) { status.textContent = text; status.style.color = color || ""; } }
  function toast(msg, ok) {
    if (!toastEl) { toastEl = document.createElement("div"); toastEl.className = "toast"; document.body.appendChild(toastEl); }
    toastEl.textContent = msg;
    toastEl.classList.toggle("ok", ok);
    toastEl.classList.toggle("err", !ok);
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 1800);
  }
  async function save() {
    show("Saving…");
    try {
      const r = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      show("✓ All changes saved · " + new Date().toLocaleTimeString(), "var(--green)");
      toast("✓ Settings saved", true);
    } catch (e) { show("✗ Not saved: " + e, "var(--red)"); toast("✗ Not saved", false); }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(save, 600); }
  // Secrets (type=password) never ride the autosave — each API key has its own
  // Save button (see the secret-save block below), so a half-typed key is never
  // persisted. Skip them on both input and change.
  const isSecret = (el) => el && el.type === "password";
  form.addEventListener("input", (e) => { if (!isSecret(e.target)) schedule(); });
  form.addEventListener("change", (e) => { if (!isSecret(e.target)) schedule(); });
})();
