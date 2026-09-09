// The Section screen: walking the queue one document at a time with a clock
// running, and repainting the tree rows a run just settled.
//
// The selection itself — ids and folder paths, as the server wants them — is
// serialised by tree.js (window.treeSelectionJSON), shared with Parsing and
// Headers.
(() => {
  "use strict";

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  /** Seconds as m:ss — minutes are not capped, so 61 minutes reads 61:00. */
  function clock(ms) {
    const total = Math.floor(ms / 1000);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  // ---- the Section button -----------------------------------------------------

  /** Disabled and spinning while a queue is live; back to itself after. */
  function setButton(running) {
    const button = document.getElementById("btn-section");
    if (!button) return;
    if (running) {
      button.dataset.running = "1";
      button.disabled = true;
      button.classList.add("is-loading");
      button.innerHTML = '<span class="btn-spinner"></span> Sectioning…';
    } else {
      delete button.dataset.running;
      button.disabled = false;
      button.classList.remove("is-loading");
      button.textContent = button.dataset.label || "Section";
    }
  }

  // ---- repainting the tree -----------------------------------------------------

  /** Keep a row honest after a run stored its blocks: count and tick.
   *
   *  Storing blocks is what takes a document out of the selection, so the
   *  box is disabled here too — otherwise the row stays tickable until a
   *  reload, and the next batch would quietly redo a document that is done.
   */
  function refreshRow(pill) {
    const id = pill.dataset.docId;
    const count = Number(pill.dataset.contentCount || 0);
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // row is in a folder that was never opened

    const cell = row.querySelector(".content-count");
    if (cell) {
      cell.dataset.count = String(count);
      cell.innerHTML = count
        ? `<span class="pill contents">${plural(count, "block")}</span>`
        : "";
    }

    const box = row.querySelector(":scope > .tree-row > .tree-check");
    if (box) {
      box.disabled = count > 0;
      box.title = count ? `${plural(count, "block")} already stored` : "";
      if (box.disabled) {
        box.checked = false;
        box.indeterminate = false;
      }
    }
    if (window.refreshTreeCounts) window.refreshTreeCounts(row);
  }

  // ---- the queue ---------------------------------------------------------------

  /** One run at a time. A second Section while this is live is refused by
   *  the disabled button, so a single slot is enough. */
  let activeRun = null;

  /** Walk the queue: one POST per row, top to bottom, with the strip keeping
   *  time. htmx.ajax resolves once the reply is swapped in, so awaiting it in
   *  a loop is what makes the run sequential.
   */
  async function runQueue(card) {
    const rows = Array.from(card.querySelectorAll(".gate-row[data-run-url]"));
    const strip = card.querySelector("[data-run-strip]");
    if (!rows.length || !strip) return;

    const timer = strip.querySelector("[data-run-timer]");
    const state = strip.querySelector("[data-run-state]");
    const progress = strip.querySelector("[data-run-progress]");
    const tally = strip.querySelector("[data-run-tally]");
    const stop = strip.querySelector("[data-run-stop]");

    const run = { stopped: false, current: null, currentStarted: 0, started: performance.now() };
    activeRun = run;
    setButton(true);

    const tick = () => {
      const now = performance.now();
      timer.textContent = clock(now - run.started);
      if (run.current) {
        const rowTimer = run.current.querySelector("[data-row-timer]");
        if (rowTimer) rowTimer.textContent = clock(now - run.currentStarted);
      }
    };
    const interval = setInterval(tick, 1000);

    stop.addEventListener("click", () => {
      run.stopped = true;
      stop.disabled = true;
      state.textContent = "⏹ stopping after this document";
    });

    let done = 0;
    let ok = 0;
    let failed = 0;
    const paint = () => {
      progress.textContent = `${done} / ${rows.length}`;
      tally.textContent = failed ? `✓ ${ok} · ✗ ${failed}` : ok ? `✓ ${ok}` : "";
    };

    for (const row of rows) {
      if (run.stopped) {
        row.querySelector(".gate-status").innerHTML =
          '<span class="pill total">— left in queue</span>';
        continue;
      }

      run.current = row;
      run.currentStarted = performance.now();
      row.classList.add("is-running");
      row.querySelector(".gate-status").innerHTML =
        '<span class="pill total is-live">⏱ <span data-row-timer>0:00</span></span>';
      state.textContent = "⚙ filing blocks · tables through the model";
      tick();

      try {
        await htmx.ajax("POST", row.dataset.runUrl, {
          source: row,
          target: row.querySelector(".gate-status"),
          swap: "innerHTML",
        });
      } catch (err) {
        row.querySelector(".gate-status").innerHTML =
          '<span class="pill fail">✗ failed</span><span class="gate-reason">request failed</span>';
      }

      row.classList.remove("is-running");
      run.current = null;
      done += 1;
      const result = row.querySelector(".gate-status [data-content-count]");
      if (result) {
        ok += 1;
        refreshRow(result);
      } else {
        failed += 1;
      }
      paint();
    }

    clearInterval(interval);
    tick();
    if (!run.abandoned) {
      state.textContent = run.stopped ? "⏹ stopped" : "✓ done";
      state.classList.toggle("ok", !run.stopped);
      stop.hidden = true;
    }
    activeRun = null;
    setButton(false);
  }

  // A run in flight is abandoned when its card is replaced; the request it
  // already sent completes on the server, but the loop stops asking for more.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "section-result" && activeRun) {
      activeRun.stopped = true;
      activeRun.abandoned = true;
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    if (target.id !== "section-result") return;
    const card = target.querySelector('[data-run="sections"]');
    if (card) runQueue(card);
  });
})();
