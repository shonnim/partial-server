// The Grouping screen: after a save, repaint the group pill on every tree
// row the reply names, then drop the selection so the next label starts
// clean. Rows in folders that were never opened get their pill from the
// server when the folder is expanded.
(() => {
  "use strict";

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escapeHtml = (text) => text.replace(/[&<>"']/g, (c) => ESCAPES[c]);

  function paintRow(id, groupId) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return;
    const cell = row.querySelector(".group-cell");
    if (!cell) return;
    cell.innerHTML = groupId
      ? `<span class="pill group" title="group id">🏷 ${escapeHtml(groupId)}</span>`
      : "";
  }

  function clearSelection() {
    document.querySelectorAll("#tree-root .tree-check").forEach((box) => {
      box.checked = false;
      box.indeterminate = false;
    });
    if (window.refreshTreeCounts) window.refreshTreeCounts();
  }

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "group-result") return;

    const card = event.detail.target.querySelector("[data-group-saved]");
    if (!card) return; // an error card: leave the ticks so the user can retry

    const groupId = card.dataset.groupId || "";
    const ids = (card.dataset.docIds || "").split(",").filter(Boolean);
    ids.forEach((id) => paintRow(id, groupId));

    // The folder pills count grouped documents; a reload makes them exact,
    // but the rows just painted are what the user is looking at.
    clearSelection();

    // Remember the label for the datalist so the next selection can pick it.
    const list = document.getElementById("known-groups");
    if (groupId && list && !Array.from(list.options).some((o) => o.value === groupId)) {
      const option = document.createElement("option");
      option.value = groupId;
      list.appendChild(option);
    }
  });
})();
