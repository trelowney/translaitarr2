// Drag-to-reorder Gemini model list with a per-model batch size, plus add and
// fetch-from-API. Used by the setup wizard and Settings. No-ops if absent.
(function () {
  const MODELS_URL = "/api/gemini/models";
  const DEFAULT_BATCH = 150;
  const ul = document.getElementById("model-list");
  if (!ul) return;
  const modelsField = document.getElementById(ul.dataset.target);
  const batchField = document.getElementById(ul.dataset.batchTarget);
  let dragEl = null;

  function sync(notify) {
    const items = [...ul.querySelectorAll("li")];
    modelsField.value = JSON.stringify(items.map((li) => li.dataset.model));
    const batches = {};
    items.forEach((li) => { batches[li.dataset.model] = parseInt(li.querySelector(".mbatch").value, 10) || DEFAULT_BATCH; });
    batchField.value = JSON.stringify(batches);
    // Let an auto-saving form know something changed (not on the initial sync).
    if (notify) modelsField.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wire(li) {
    li.draggable = true;
    li.addEventListener("dragstart", () => { dragEl = li; li.classList.add("drag"); });
    li.addEventListener("dragend", () => { li.classList.remove("drag"); sync(true); });
    li.querySelector(".x").addEventListener("click", () => { li.remove(); sync(true); });
    li.querySelector(".mbatch").addEventListener("input", () => sync(true));
  }
  function exists(name) {
    return [...ul.querySelectorAll("li")].some((li) => li.dataset.model === name);
  }
  function makeLi(name, batch) {
    const li = document.createElement("li");
    li.dataset.model = name;
    li.innerHTML = '<span class="grip">⠿</span><span class="mname"></span>' +
      '<input type="number" class="mbatch" min="20" step="10" title="batch size (cues per request)">' +
      '<button type="button" class="x" title="Remove">×</button>';
    li.querySelector(".mname").textContent = name;
    li.querySelector(".mbatch").value = batch || DEFAULT_BATCH;
    wire(li);
    return li;
  }
  function add(name, batch) {
    name = (name || "").trim();
    if (name && !exists(name)) { ul.appendChild(makeLi(name, batch)); sync(true); }
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
  sync();

  const input = document.getElementById("model-input");
  document.getElementById("model-add-btn").addEventListener("click", () => { add(input.value); input.value = ""; });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); add(input.value); input.value = ""; }
  });

  document.getElementById("model-refresh-btn").addEventListener("click", async () => {
    const msg = document.getElementById("model-msg");
    const keyEl = document.querySelector("[name=gemini_api_key]");
    msg.textContent = "Fetching…"; msg.style.color = "";
    try {
      const r = await fetch(MODELS_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: keyEl ? keyEl.value : "" }),
      });
      const d = await r.json();
      if (!d.ok) { msg.textContent = "✗ " + d.error; msg.style.color = "var(--red)"; return; }
      let added = 0, present = 0;
      d.models.forEach((m) => {
        if (exists(m)) { present++; } else { ul.appendChild(makeLi(m, DEFAULT_BATCH)); added++; }
      });
      sync(true);
      msg.textContent = "✓ " + d.models.length + " available · " + added + " added · " + present + " already in list";
      msg.style.color = "var(--green)";
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; }
  });
})();

// ── Auto-save: persist Settings on every change (no Save button) ──────────────
(function () {
  const form = document.querySelector("form[data-autosave]");
  if (!form) return;
  const status = document.getElementById("save-status");
  let timer = null;

  function show(text, color) {
    if (status) { status.textContent = text; status.style.color = color || ""; }
  }
  async function save() {
    show("Saving…");
    try {
      const r = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const t = new Date().toLocaleTimeString();
      show("✓ All changes saved · " + t, "var(--green)");
    } catch (e) {
      show("✗ Not saved: " + e, "var(--red)");
    }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(save, 600); }

  form.addEventListener("input", schedule);
  form.addEventListener("change", schedule);
})();

