// The Summarizing screen: the live count of ticked documents in the side
// box, a button that is live once something is ticked, and a queue walked
// one document per request — every header is a model call, so the browser
// drives the batch rather than one long HTTP request.
//
// The selection itself — ids and folder paths — is serialised by tree.js
// (window.treeSelectionJSON), shared with the other tree pages.
(() => {
  "use strict";

  const form = document.getElementById("summarize-form");
  const button = document.getElementById("btn-summarize");
  const counter = document.getElementById("summarize-selected");
  if (!form || !button || !counter) return;

  let running = false;

  function pickedTotal() {
    const n = document.querySelector("#tree-root-badges .tree-picked-n");
    return n ? Number(n.textContent || 0) : 0;
  }

  /** The button is live only while something is ticked and no queue runs. */
  function sync() {
    const total = pickedTotal();
    counter.textContent = total.toLocaleString();
    button.disabled = running || total === 0;
    button.title = total === 0 ? "Tick at least one document or folder" : "";
  }

  function setRunning(state) {
    running = state;
    if (state) {
      button.classList.add("is-loading");
      button.innerHTML = '<span class="btn-spinner"></span> Summarizing…';
    } else {
      button.classList.remove("is-loading");
      button.textContent = button.dataset.label || "Summarize";
    }
    sync();
  }

  const badges = document.getElementById("tree-root-badges");
  if (badges) {
    new MutationObserver(sync).observe(badges, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
  }
  sync();

  // ---- repainting the tree -------------------------------------------------------

  /** Keep a row honest after a run: the "📝 done/total" badge. */
  function paintRow(id, done, total, isDone) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // row is in a folder that was never opened
    const cell = row.querySelector(".summary-count");
    if (!cell || !total) return;
    cell.innerHTML =
      `<a class="pill ${isDone ? "ok" : "total"}" data-viewer ` +
      `title="summarized / level 1-2 headers" ` +
      `href="${cell.dataset.summariesUrl}">📝 ${done}/${total}</a>`;
  }

  // ---- the queue -----------------------------------------------------------------

  /** One run at a time; the disabled button refuses a second. */
  let activeRun = null;

  async function runQueue(card) {
    const rows = Array.from(card.querySelectorAll(".gate-row[data-run-url]"));
    const strip = card.querySelector("[data-run-strip]");
    if (!rows.length || !strip) return;

    const state = strip.querySelector("[data-run-state]");
    const stop = strip.querySelector("[data-run-stop]");
    const force = card.dataset.force === "1";

    const run = { stopped: false };
    activeRun = run;
    setRunning(true);

    stop.addEventListener("click", () => {
      run.stopped = true;
      stop.disabled = true;
      state.textContent = "⏹ stopping after this document";
    });

    let done = 0;
    let ok = 0;
    let failed = 0;

    for (const row of rows) {
      if (run.stopped) {
        const cell = row.querySelector(".gate-status");
        if (cell) cell.innerHTML = '<span class="pill total">⏹ not run</span>';
        continue;
      }

      state.textContent = `⚙ ${done} / ${rows.length} · headers through the model`;
      const cell = row.querySelector(".gate-status");
      if (cell) cell.innerHTML = '<span class="pill total">⚙ running…</span>';

      try {
        await htmx.ajax("POST", row.dataset.runUrl, {
          source: row,
          target: cell,
          swap: "innerHTML",
          values: force ? { force: "1" } : {},
        });
      } catch (err) {
        /* read the cell below either way */
      }

      done += 1;
      const result = row.querySelector(".gate-status [data-sum-result]");
      if (result && !Number(result.dataset.failed || 0)) {
        ok += 1;
      } else {
        failed += 1;
        if (!result && cell) {
          cell.innerHTML = '<span class="pill fail">✗ request failed</span>';
        }
      }
      if (result) {
        paintRow(
          result.dataset.docId,
          Number(result.dataset.doneTotal || 0),
          Number(result.dataset.total || 0),
          result.dataset.done === "1",
        );
      }
    }

    if (!run.abandoned) {
      const tally = failed ? `✓ ${ok} · ✗ ${failed}` : `✓ ${ok}`;
      state.textContent = run.stopped ? `⏹ stopped · ${tally}` : `✓ done · ${tally}`;
      state.classList.toggle("ok", !run.stopped && !failed);
      stop.hidden = true;
    }
    activeRun = null;
    setRunning(false);
  }

  // A run in flight is abandoned when its card is replaced; the request it
  // already sent completes on the server, but the loop stops asking for more.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "summarize-result" && activeRun) {
      activeRun.stopped = true;
      activeRun.abandoned = true;
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "summarize-result") return;
    const card = event.detail.target.querySelector('[data-run="summarizing"]');
    if (card) runQueue(card);
  });
})();
