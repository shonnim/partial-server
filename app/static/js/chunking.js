// The Chunking screen: the live count of ticked documents in the side box,
// a button that is only live once a group is picked and something is ticked,
// a loading state while the run is in flight, and the chunk count repainted
// on every tree row the reply names.
//
// The selection itself — ids and folder paths — is serialised by tree.js
// (window.treeSelectionJSON), shared with the other tree pages. tree.js
// also paints the picked total into #tree-root-badges; this script reads
// it from there rather than counting again.
(() => {
  "use strict";

  const form = document.getElementById("chunk-form");
  const select = document.getElementById("chunk-group");
  const button = document.getElementById("btn-chunk");
  const counter = document.getElementById("chunk-selected");
  if (!form || !select || !button || !counter) return;

  let running = false;

  function pickedTotal() {
    const n = document.querySelector("#tree-root-badges .tree-picked-n");
    return n ? Number(n.textContent || 0) : 0;
  }

  /** The button is live only with a group picked and something ticked. */
  function sync() {
    const total = pickedTotal();
    counter.textContent = total.toLocaleString();
    const option = select.selectedOptions[0];
    const noStrategy = Boolean(option && option.dataset.unimplemented);
    button.disabled = running || !select.value || total === 0;
    button.title = !select.value
      ? "Pick a group id first"
      : noStrategy
        ? "No chunking strategy for this group id yet"
        : total === 0
          ? "Tick at least one document"
          : "";
  }

  /** Disabled and spinning while the request is in flight. */
  function setRunning(state) {
    running = state;
    if (state) {
      button.classList.add("is-loading");
      button.innerHTML = '<span class="btn-spinner"></span> Chunking…';
    } else {
      button.classList.remove("is-loading");
      button.textContent = button.dataset.label || "Chunking";
    }
    sync();
  }

  // ---- repainting the tree -----------------------------------------------------

  /** Keep a row honest after a run stored its chunks: count and link. */
  function paintRow(id, count) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // row is in a folder that was never opened
    const cell = row.querySelector(".chunk-count");
    if (!cell) return;
    cell.dataset.count = String(count);
    if (count > 0) {
      cell.innerHTML =
        `<a class="pill chunks" data-viewer title="the chunks stored for this document" ` +
        `href="${cell.dataset.chunksUrl}">${count} chunk${count === 1 ? "" : "s"}</a>`;
    } else {
      cell.innerHTML = "";
    }
  }

  // ---- events --------------------------------------------------------------------

  // tree.js repaints the picked total on every tick and every level load;
  // watching that number is simpler than repeating its rules here.
  const badges = document.getElementById("tree-root-badges");
  if (badges) {
    new MutationObserver(sync).observe(badges, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
  }
  select.addEventListener("change", sync);

  form.addEventListener("htmx:beforeRequest", () => setRunning(true));
  form.addEventListener("htmx:afterRequest", () => setRunning(false));

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "chunk-result") return;
    event.detail.target
      .querySelectorAll("[data-chunked]")
      .forEach((row) => paintRow(row.dataset.docId, Number(row.dataset.chunks || 0)));
  });

  sync();
})();
