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

  /** Whether the Force toggle is on: stored documents are selectable again. */
  window.headerForce = () => {
    const box = document.getElementById("force-get-header");
    return Boolean(box && box.checked);
  };

  /** The model chosen in the toolbar's LLM picker. */
  window.headerLLM = () => {
    const select = document.getElementById("header-llm");
    return select ? select.value : "";
  };

  /** The page range the Pages method sends to the parser, as form values. */
  window.headerPageRange = () => {
    const value = (id) => {
      const input = document.getElementById(id);
      return input ? input.value : "";
    };
    return {
      start_page: value("header-start-page"),
      end_page: value("header-end-page"),
    };
  };

  // ---- the method picker ---------------------------------------------------

  // Manual runs from the button on each row, so the batch button has nothing
  // to do under it and stays off. The page range fields only mean something
  // to the Pages method and appear only under it.
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

  // ---- the Force toggle ------------------------------------------------------

  // Ticking Force hands the opted-out rows back to the selection: every
  // disabled box in the tree — stored documents, and the folders that went
  // with them — is enabled, remembering on the element the disabled state it
  // returns to when the toggle is cleared. Rows that load or finish while
  // the toggle is on are swept the same way, so no box stays disabled under
  // Force.

  /** Enable every disabled box in `scope`; returns the boxes released now. */
  function releaseBoxes(scope) {
    const released = [];
    scope.querySelectorAll(".tree-check:disabled").forEach((box) => {
      box.dataset.forceReleased = "1";
      box.dataset.heldTitle = box.title;
      box.disabled = false;
      box.title = "";
      released.push(box);
    });
    return released;
  }

  /** Put every released box back the way the tree had it. */
  function restoreBoxes() {
    const fileNodes = [];
    document.querySelectorAll(".tree-check[data-force-released]").forEach((box) => {
      box.disabled = true;
      box.checked = false;
      box.indeterminate = false;
      box.title = box.dataset.heldTitle || "";
      delete box.dataset.forceReleased;
      delete box.dataset.heldTitle;
      const node = box.closest(".tree-node");
      if (node && node.classList.contains("is-file")) fileNodes.push(node);
    });
    return fileNodes;
  }

  document.addEventListener("change", (event) => {
    if (event.target.id !== "force-get-header") return;
    const root = document.getElementById("tree-root");
    if (!root || !window.refreshTreeCounts) return;

    if (event.target.checked) {
      releaseBoxes(root);
      window.refreshTreeCounts();
    } else {
      // Folder states are re-derived per restored document rather than
      // trusted from the stash: extraction may have stored headers for more
      // documents while the toggle was on, which can take a folder that was
      // selectable before out of the selection now.
      restoreBoxes().forEach((node) => window.refreshTreeCounts(node));
      window.refreshTreeCounts();
    }
  });

  // ---- repainting ----------------------------------------------------------

  const PILLS = {
    YES: '<span class="pill ok" title="has a contents page">✓</span>',
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
      const held = count
        ? `${plural(count, "header")} already stored — tick Force get header or use Manual to redo them`
        : "";
      if (count && window.headerForce()) {
        // Under Force the row stays selectable; what it keeps is the disabled
        // state to fall back to when the toggle is cleared. The tick still
        // comes off — the document just finished, and leaving it ticked would
        // feed it straight into the next batch.
        box.dataset.forceReleased = "1";
        box.dataset.heldTitle = held;
        box.checked = false;
      } else {
        delete box.dataset.forceReleased;
        delete box.dataset.heldTitle;
        box.disabled = count > 0;
        box.title = held;
        if (box.disabled) box.checked = false;
      }
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
    // The pick made when Get Header was pressed, pinned onto the card: the
    // whole run uses it even if the toolbar changes mid-queue. Under the
    // Group method no range is sent — the server reads each document's own
    // group strategy instead.
    const range = card.dataset.useGroup
      ? {
          use_group: "true",
          ...(card.dataset.llm ? { llm: card.dataset.llm } : {}),
        }
      : {
          start_page: card.dataset.startPage || "",
          end_page: card.dataset.endPage || "",
          ...(card.dataset.llm ? { llm: card.dataset.llm } : {}),
        };

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
          values: range,
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

  // ---- the outline pop-up's index editor -----------------------------------

  /** The edited printed page and start/end page indexes, for the Save button.
   *
   *  Keyed by header id; an emptied box becomes null, which the server stores
   *  as NULL. Read from the open dialog — there is only ever one.
   */
  window.headerIndexEdits = () => {
    const edits = {};
    document.querySelectorAll("#modal dialog [data-header-id]").forEach((row) => {
      const read = (selector) => {
        const input = row.querySelector(selector);
        const value = input ? input.value.trim() : "";
        return value === "" ? null : Number(value);
      };
      edits[row.dataset.headerId] = {
        page: read("[data-page-start]"),
        start: read("[data-idx-start]"),
        end: read("[data-idx-end]"),
      };
    });
    return JSON.stringify(edits);
  };

  // ---- the transfer pop-up's two editors ------------------------------------

  /** One pane's rows as the server wants them, keyed by `keyAttr`'s value.
   *
   *  Each pane of the Transfer pop-up edits and saves on its own: the bucket
   *  pane's rows carry data-toc-seq, the Stored pane's data-header-id, and a
   *  Save serialises only its own pane.
   */
  function paneEdits(pane, keyAttr) {
    const edits = {};
    pane.querySelectorAll(`[${keyAttr}]`).forEach((row) => {
      const number = (selector) => {
        const input = row.querySelector(selector);
        const value = input ? input.value.trim() : "";
        return value === "" ? null : Number(value);
      };
      const title = row.querySelector("[data-edit-title]");
      edits[row.getAttribute(keyAttr)] = {
        title: title ? title.value : null,
        page: number("[data-page-start]"),
        start: number("[data-idx-start]"),
        end: number("[data-idx-end]"),
      };
    });
    return JSON.stringify(edits);
  }

  window.transferTocEdits = () => {
    const pane = document.querySelector('#modal [data-pane="toc"]');
    return pane ? paneEdits(pane, "data-toc-seq") : "{}";
  };

  window.transferStoredEdits = () => {
    const pane = document.querySelector('#modal [data-pane="stored"]');
    return pane ? paneEdits(pane, "data-header-id") : "{}";
  };

  document.addEventListener("click", (event) => {
    const pane = event.target.closest(".transfer-pane");
    if (!pane) return;

    if (event.target.closest("[data-edit-pane]")) {
      pane.classList.add("is-editing");
      const first = pane.querySelector("[data-edit-title]");
      if (first) first.focus();
    } else if (event.target.closest("[data-cancel-pane]")) {
      pane.classList.remove("is-editing");
      // Back out whatever was typed: the boxes return to the rendered values.
      pane.querySelectorAll(".idx-input, .title-input").forEach((input) => {
        input.value = input.defaultValue;
      });
    }
  });

  document.addEventListener("click", (event) => {
    const dialog = event.target.closest("dialog");
    if (!dialog) return;

    if (event.target.closest("[data-edit-indexes]")) {
      dialog.classList.add("is-editing");
      const first = dialog.querySelector("[data-page-start]");
      if (first) first.focus();
    } else if (event.target.closest("[data-cancel-indexes]")) {
      dialog.classList.remove("is-editing");
      // Back out whatever was typed: the boxes return to the stored values.
      dialog.querySelectorAll(".idx-input").forEach((input) => {
        input.value = input.defaultValue;
      });
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;

    // A level that loads while Force is on arrives with its stored rows
    // disabled; sweep them into the selection like everything already on
    // screen. tree.js's own handler has run first (it registered first) and
    // passed a checked folder's tick down to the selectable rows — the rows
    // released here take the same tick, so a folder checked under Force
    // keeps meaning "everything beneath it".
    if (window.headerForce()) {
      const list = target.classList.contains("tree-children")
        ? target
        : target.closest(".tree-children");
      if (list) {
        releaseBoxes(list).forEach((box) => {
          const node = box.closest(".tree-node");
          const parent =
            node && node.parentElement && node.parentElement.closest(".tree-node");
          const parentBox =
            parent && parent.querySelector(":scope > .tree-row > .tree-check");
          if (parentBox && parentBox.checked && !parentBox.indeterminate) {
            box.checked = true;
          }
        });
        if (window.refreshTreeCounts) {
          window.refreshTreeCounts(list.closest(".tree-node"));
        }
        return;
      }
    }

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
