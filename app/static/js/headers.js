// The Headers screen: repainting the rows a check just settled, and walking
// the Pages queue one document at a time with a clock running.
//
// The selection itself — ids and folder paths, as the server wants them — is
// serialised by tree.js (window.treeSelectionJSON), shared with Parsing.
(() => {
  "use strict";

  /** The extraction method currently chosen in the toolbar. */
  window.headerMethod = () => {
    const select = document.getElementById("header-method");
    return select ? select.value : "manual";
  };

  /** How many pages after the contents page the Pages method should parse. */
  window.headerPages = () => {
    const input = document.getElementById("header-pages");
    return input ? input.value : "";
  };

  // ---- the method picker ---------------------------------------------------

  // Manual runs from the button on each row, so the batch button has nothing
  // to do under it and stays off. The pages field only means something to
  // the Pages method and appears only under it.
  function syncMethod() {
    const method = window.headerMethod();
    const button = document.getElementById("btn-get-header");
    const pagesPicker = document.getElementById("pages-picker");

    if (button && !button.dataset.running) {
      button.disabled = method === "manual";
      button.title = method === "manual" ? "Pick a method other than Manual" : "";
    }
    if (pagesPicker) pagesPicker.hidden = method !== "pages";
  }

  document.addEventListener("change", (event) => {
    if (event.target.id === "header-method") syncMethod();
  });
  document.addEventListener("DOMContentLoaded", syncMethod);
  if (document.readyState !== "loading") syncMethod();

  // ---- repainting ----------------------------------------------------------

  const PILLS = {
    YES: '<span class="pill ok">✓ yes header</span>',
    NO: '<span class="pill total">— no header</span>',
  };

  function repaint(result) {
    let updates;
    try {
      updates = JSON.parse(result.dataset.updates || "{}");
    } catch (err) {
      return;
    }
    Object.entries(updates).forEach(([id, value]) => {
      const cell = document.querySelector(`.tree-node[data-id="${id}"] .has-header`);
      if (!cell) return; // row is in a folder that was never opened
      cell.dataset.value = value;
      cell.innerHTML = PILLS[value] || "";
    });
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  /** Keep a row honest after an extraction saves: button, count, and tick.
   *
   *  Storing headers is what takes a document out of the selection, so the box
   *  is disabled here too — otherwise the row stays tickable until a reload,
   *  and the next batch would quietly include a document that is already done.
   *
   *  `source` is any element carrying data-doc-id and data-header-count: the
   *  outline pop-up, or the status pill of a finished queue row.
   */
  function refreshRow(source) {
    const id = source.dataset.docId;
    const count = Number(source.dataset.headerCount || 0);
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // row is in a folder that was never opened

    const button = row.querySelector(".btn-outline");
    if (button) {
      button.classList.toggle("is-empty", count === 0);
      button.title = `${plural(count, "header")} stored`;
    }

    const cell = row.querySelector(".header-count");
    if (cell) {
      cell.dataset.count = String(count);
      cell.innerHTML = count
        ? `<span class="pill headers">${plural(count, "header")}</span>`
        : "";
    }

    const box = row.querySelector(":scope > .tree-row > .tree-check");
    if (box) {
      box.disabled = count > 0;
      box.title = count
        ? `${plural(count, "header")} already stored — use Manual to redo them`
        : "";
      if (box.disabled) box.checked = false;
    }
    if (window.refreshTreeCounts) window.refreshTreeCounts(row);
  }

  // ---- the Pages queue -----------------------------------------------------

  /** Seconds as m:ss — minutes are not capped, so 61 minutes reads 61:00. */
  function clock(ms) {
    const total = Math.floor(ms / 1000);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  /** One run at a time. A second Get Header while this is live is refused
   *  by the disabled button, so a single slot is enough. */
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
    const button = document.getElementById("btn-get-header");
    const pages = card.dataset.pages || "";

    const run = { stopped: false, current: null, started: performance.now() };
    activeRun = run;
    if (button) {
      button.dataset.running = "1";
      button.disabled = true;
    }

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
      state.textContent = "⚙ parsing and extracting";
      tick();

      try {
        await htmx.ajax("POST", row.dataset.runUrl, {
          target: row.querySelector(".gate-status"),
          swap: "innerHTML",
          values: { pages },
        });
      } catch (err) {
        row.querySelector(".gate-status").innerHTML =
          '<span class="pill fail">✗ failed</span><span class="gate-reason">request failed</span>';
      }

      row.classList.remove("is-running");
      run.current = null;
      done += 1;
      if (row.querySelector(".gate-status .pill.ok")) ok += 1;
      else failed += 1;
      paint();
    }

    clearInterval(interval);
    tick();
    state.textContent = run.stopped ? "⏹ stopped" : "✓ done";
    state.classList.toggle("ok", !run.stopped);
    stop.hidden = true;
    activeRun = null;

    if (button) {
      delete button.dataset.running;
      syncMethod();
    }
  }

  // A run in flight is abandoned when its card is replaced; the requests it
  // already sent complete on the server, but the loop stops asking for more.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "header-result" && activeRun) activeRun.stopped = true;
  });

  // ---- the manual paste box ------------------------------------------------

  /** Line and character counts for the paste, so a truncated copy is visible.
   *
   *  Lines are counted the way a person counts them: a box holding nothing is
   *  zero lines, not one.
   */
  function countPaste(input) {
    const panel = input.closest(".paste-panel");
    if (!panel) return;

    const stats = panel.querySelector("[data-paste-stats]");
    if (!stats) return;

    const text = input.value;
    const lines = text.length === 0 ? 0 : text.split("\n").length;
    const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

    stats.textContent = `${plural(lines, "line")} · ${plural(text.length, "char")}`;
  }

  document.addEventListener("input", (event) => {
    const input = event.target.closest("[data-paste-input]");
    if (input) countPaste(input);
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-paste-clear]")) return;

    const input = document.querySelector("[data-paste-input]");
    if (!input) return;
    input.value = "";
    countPaste(input);
    input.focus();
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;

    if (target.id === "header-result") {
      const result = target.querySelector("[data-updates]");
      if (result) repaint(result);
      const card = target.querySelector('[data-run="pages"]');
      if (card) runQueue(card);
      return;
    }

    if (target.classList && target.classList.contains("gate-status")) {
      const pill = target.querySelector("[data-header-count]");
      if (pill) refreshRow(pill);
      return;
    }

    if (target.id === "modal") {
      const dialog = target.querySelector("dialog[data-header-count]");
      if (dialog) refreshRow(dialog);

      // A failed extraction comes back with the paste still in it, so the
      // counts have to be recomputed rather than left at zero.
      const paste = target.querySelector("[data-paste-input]");
      if (paste) countPaste(paste);
    }
  });
})();
