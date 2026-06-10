// Settings page behaviour: draggable Gemini model list (per-model batch + daily
// limit), path-mapping table, and auto-save. Each block no-ops if its markup
// isn't present.

// ── Model list ───────────────────────────────────────────────────────────────
(function () {
  const MODELS_URL = "/api/gemini/models";
  const DEFAULT_BATCH = 150, DEFAULT_LIMIT = 18;
  const ul = document.getElementById("model-list");
  if (!ul) return;
  const modelsField = document.getElementById(ul.dataset.target);
  const batchField = document.getElementById(ul.dataset.batchTarget);
  const limitField = document.getElementById(ul.dataset.limitTarget);
  let dragEl = null;

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
    li.querySelector(".x").addEventListener("click", () => { li.remove(); sync(true); });
    // The number fields stay editable/selectable: dragging them must not move the card.
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
  function makeLi(name, batch, limit) {
    const li = document.createElement("li");
    li.dataset.model = name;
    li.innerHTML = '<span class="grip">⠿</span><span class="mname"></span>' +
      '<input type="number" class="mbatch" min="20" step="10" title="batch size (cues per request)">' +
      '<input type="number" class="mlimit" min="1" title="daily request limit">' +
      '<button type="button" class="x" title="Remove">×</button>';
    li.querySelector(".mname").textContent = name;
    li.querySelector(".mbatch").value = batch || DEFAULT_BATCH;
    li.querySelector(".mlimit").value = limit || DEFAULT_LIMIT;
    wire(li);
    return li;
  }
  function add(name) {
    name = (name || "").trim();
    if (name && !exists(name)) { ul.appendChild(makeLi(name, DEFAULT_BATCH, DEFAULT_LIMIT)); sync(true); }
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

  const input = document.getElementById("model-input");
  document.getElementById("model-add-btn").addEventListener("click", () => { add(input.value); input.value = ""; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(input.value); input.value = ""; } });

  document.getElementById("model-refresh-btn").addEventListener("click", async () => {
    const msg = document.getElementById("model-msg");
    const keyEl = document.querySelector("[name=gemini_api_key]");
    msg.textContent = "Fetching…"; msg.style.color = "";
    try {
      const r = await fetch(MODELS_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: keyEl ? keyEl.value : "" }),
      });
      const d = await r.json();
      if (!d.ok) { msg.textContent = "✗ " + d.error; msg.style.color = "var(--red)"; return; }
      let added = 0, present = 0;
      d.models.forEach((m) => { if (exists(m)) { present++; } else { ul.appendChild(makeLi(m, DEFAULT_BATCH, DEFAULT_LIMIT)); added++; } });
      sync(true);
      msg.textContent = "✓ " + d.models.length + " available · " + added + " added · " + present + " already in list";
      msg.style.color = "var(--green)";
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; }
  });
})();

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
  let timer = null;
  function show(text, color) { if (status) { status.textContent = text; status.style.color = color || ""; } }
  async function save() {
    show("Saving…");
    try {
      const r = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      show("✓ All changes saved · " + new Date().toLocaleTimeString(), "var(--green)");
    } catch (e) { show("✗ Not saved: " + e, "var(--red)"); }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(save, 600); }
  form.addEventListener("input", schedule);
  form.addEventListener("change", schedule);
})();
