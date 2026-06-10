// Drag-to-reorder Gemini model list, with add + fetch-from-API. Used by the
// setup wizard and the Settings page. No-ops if the model list isn't present.
(function () {
  const MODELS_URL = "/api/gemini/models";
  const ul = document.getElementById("model-list");
  if (!ul) return;
  const hidden = document.getElementById(ul.dataset.target);
  let dragEl = null;

  function sync() {
    hidden.value = JSON.stringify([...ul.querySelectorAll("li")].map((li) => li.dataset.model));
  }
  function wire(li) {
    li.draggable = true;
    li.addEventListener("dragstart", () => { dragEl = li; li.classList.add("drag"); });
    li.addEventListener("dragend", () => { li.classList.remove("drag"); sync(); });
    li.querySelector(".x").addEventListener("click", () => { li.remove(); sync(); });
  }
  function makeLi(name) {
    const li = document.createElement("li");
    li.dataset.model = name;
    li.innerHTML = '<span class="grip">⠿</span><span class="mname"></span>' +
                   '<button type="button" class="x" title="Remove">×</button>';
    li.querySelector(".mname").textContent = name;
    wire(li);
    return li;
  }
  function exists(name) {
    return [...ul.querySelectorAll("li")].some((li) => li.dataset.model === name);
  }
  function add(name) {
    name = (name || "").trim();
    if (name && !exists(name)) { ul.appendChild(makeLi(name)); sync(); }
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
      let added = 0;
      d.models.forEach((m) => { if (!exists(m)) { ul.appendChild(makeLi(m)); added++; } });
      sync();
      msg.textContent = "✓ " + d.models.length + " available" + (added ? ", " + added + " added" : "");
      msg.style.color = "var(--green)";
    } catch (e) { msg.textContent = "✗ " + e; msg.style.color = "var(--red)"; }
  });
})();
