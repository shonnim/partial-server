// The Fixing screen: Check and Fix share one result panel. Both buttons are
// disabled and spinning while a request is in flight, and a fix reply names
// the documents whose PDF was rewritten so their rows can be taken out of the
// selection in place.
//
// A check reply leaves the ticks alone on purpose — checking is how the user
// decides to fix the very same selection. A fix reply with failures keeps the
// remaining ticks too, so the same selection can be retried; the rewrite is
// idempotent, so the retry only redoes what is missing.
(() => {
  "use strict";

  // ---- the buttons, busy for as long as a request runs ----------------------

  const FORMS = {
    "check-form": { button: "btn-check", busy: "Checking…" },
    "fix-form": { button: "btn-fix", busy: "Fixing…" },
    "pages-form": { button: "btn-pages", busy: "Counting…" },
  };

  function setRunning(running, active) {
    Object.values(FORMS).forEach((form) => {
      const button = document.getElementById(form.button);
      if (!button) return;
      if (running) {
        button.disabled = true;
        if (form === active) {
          button.classList.add("is-loading");
          button.innerHTML = '<span class="btn-spinner"></span> ' + form.busy;
        }
      } else {
        button.disabled = false;
        button.classList.remove("is-loading");
        button.textContent = button.dataset.label || "Run";
      }
    });
  }

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const form = FORMS[event.detail.elt.id];
    if (form) setRunning(true, form);
  });

  // afterRequest rather than afterSwap, so a failed request — a network
  // error, a 500 — hands the buttons back too instead of leaving them spinning.
  document.body.addEventListener("htmx:afterRequest", (event) => {
    if (FORMS[event.detail.elt.id]) setRunning(false);
  });

  // ---- repainting the tree --------------------------------------------------

  /** Take one fixed row out of the selection, the way the server renders it. */
  function disableRow(id) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return null; // inside a folder that was never opened
    const box = row.querySelector(".tree-check");
    if (box) {
      box.checked = false;
      box.indeterminate = false;
      box.disabled = true;
      box.title = "Already rewritten into one page per sheet";
    }
    const cell = row.querySelector(".fixed-cell");
    if (cell) {
      cell.innerHTML =
        '<span class="pill fixed" title="Already rewritten into one page per sheet">✂ fixed</span>';
    }
    return row;
  }

  function clearSelection() {
    document.querySelectorAll("#tree-root .tree-check").forEach((box) => {
      box.checked = false;
      box.indeterminate = false;
    });
  }

  /** Repaint one row's page pill after a pages check corrected its count. */
  function updatePagesPill(id, pages) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // inside a folder that was never opened
    const cell = row.querySelector(".pages-cell");
    if (cell) cell.textContent = `${pages} p`;
  }

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "fix-result") return;

    // A pages-check reply names the rows whose stored count was corrected;
    // their page pills repaint in place and the ticks stay as they are.
    const pagesCard = event.detail.target.querySelector("[data-page-updates]");
    if (pagesCard) {
      (pagesCard.dataset.pageUpdates || "")
        .split(",")
        .filter(Boolean)
        .forEach((pair) => {
          const [id, pages] = pair.split(":");
          if (id && pages) updatePagesPill(id, pages);
        });
      return;
    }

    // Only a fix reply carries data-doc-ids; a check reply (or an error card)
    // changes nothing in the tree.
    const card = event.detail.target.querySelector("[data-doc-ids]");
    if (!card) return;

    // Rows fixed in full go out of the selection even when others failed.
    // The folder pills count fixed documents; a reload makes them exact, but
    // the rows just disabled are what the user is looking at.
    const ids = (card.dataset.docIds || "").split(",").filter(Boolean);
    const rows = ids.map(disableRow).filter(Boolean);

    if (card.hasAttribute("data-fixed")) clearSelection();

    if (window.refreshTreeCounts) {
      rows.forEach((row) => window.refreshTreeCounts(row));
      if (!rows.length) window.refreshTreeCounts();
    }
  });
})();
