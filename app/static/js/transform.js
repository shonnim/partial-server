// The Transform screen: the Migrate button is disabled and spinning while the
// request is in flight, and the reply names the documents migrated in full so
// their rows can be taken out of the selection in place.
//
// A reply with failures keeps the remaining ticks, so the user can retry the
// same selection — the copies are idempotent, so the retry only redoes what
// is missing.
(() => {
  "use strict";

  // ---- the button, busy for as long as the request runs --------------------

  function setRunning(running) {
    const button = document.getElementById("btn-migrate");
    if (!button) return;
    if (running) {
      button.disabled = true;
      button.classList.add("is-loading");
      button.innerHTML = '<span class="btn-spinner"></span> Migrating…';
    } else {
      button.disabled = false;
      button.classList.remove("is-loading");
      button.textContent = button.dataset.label || "Migrate selection";
    }
  }

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.elt.id === "migrate-form") setRunning(true);
  });

  // afterRequest rather than afterSwap, so a failed request — a network
  // error, a 500 — hands the button back too instead of leaving it spinning.
  document.body.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.elt.id === "migrate-form") setRunning(false);
  });

  // ---- repainting the tree --------------------------------------------------

  /** Take one migrated row out of the selection, the way the server renders it. */
  function disableRow(id) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return null; // inside a folder that was never opened
    const box = row.querySelector(".tree-check");
    if (box) {
      box.checked = false;
      box.indeterminate = false;
      box.disabled = true;
      box.title = "Already migrated to the RAG files bucket";
    }
    const cell = row.querySelector(".migrated-cell");
    if (cell) {
      cell.innerHTML =
        '<span class="pill migrated" title="Already migrated to the RAG files bucket">📦 migrated</span>';
    }
    return row;
  }

  function clearSelection() {
    document.querySelectorAll("#tree-root .tree-check").forEach((box) => {
      box.checked = false;
      box.indeterminate = false;
    });
  }

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "migrate-result") return;

    const card = event.detail.target.querySelector("[data-doc-ids]");
    if (!card) return; // an error card: leave the ticks so the user can retry

    // Rows migrated in full go out of the selection even when others failed.
    // The folder pills count migrated documents; a reload makes them exact,
    // but the rows just disabled are what the user is looking at.
    const ids = (card.dataset.docIds || "").split(",").filter(Boolean);
    const rows = ids.map(disableRow).filter(Boolean);

    if (card.hasAttribute("data-migrated")) clearSelection();

    if (window.refreshTreeCounts) {
      rows.forEach((row) => window.refreshTreeCounts(row));
      if (!rows.length) window.refreshTreeCounts();
    }
  });
})();
