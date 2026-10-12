// Uploads from the parsing tree's "+" buttons.
//
// The pop-up itself is plain HTMX — the form posts and the per-file outcomes
// land in #upload-result. This script's only job is the follow-up: once an
// upload has stored at least one document, re-fetch that folder's level so
// the new rows (or the new subfolder) appear without a page reload.
(() => {
  "use strict";

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "upload-result") return;

    const done = event.detail.target.querySelector("[data-upload-done]");
    if (!done || !Number(done.dataset.uploaded)) return;

    // Paths hold Korean and brackets, so the node is found by comparison
    // rather than baked into a CSS selector.
    const path = done.dataset.path;
    let node = null;
    let list = document.getElementById("tree-root");
    if (path) {
      node = Array.from(document.querySelectorAll(".tree-node[data-path]"))
        .find((n) => n.dataset.path === path);
      list = node ? node.querySelector(":scope > .tree-children") : null;
    }
    if (!list) return;

    htmx.ajax("GET", done.dataset.nodesUrl, { target: list, swap: "innerHTML" })
      .then(() => {
        if (!node) return;
        // Leave the refreshed folder open so the new rows are visible.
        list.hidden = false;
        node.classList.add("is-open");
        const label = node.querySelector(":scope > .tree-row > .tree-label");
        if (label) label.setAttribute("aria-expanded", "true");
      });
  });
})();
