// The Section screen: walking the queue one document at a time with a clock
// running, repainting the tree rows a run just settled, and keeping the run
// log on the right up to date.
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

  /** Seconds as a short figure: 4.2s under a minute, m:ss above. */
  function span(seconds) {
    return seconds < 60 ? `${seconds.toFixed(1)}s` : clock(seconds * 1000);
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
      // The same link the server renders, so a row a run just filled opens
      // its blocks without a reload.
      const href = cell.dataset.contentsUrl || "";
      cell.innerHTML =
        count && href
          ? `<a class="pill contents" data-viewer title="the blocks stored for this document" href="${href}">${plural(count, "block")}</a>`
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

  // ---- the run log -------------------------------------------------------------

  /** The box on the right. One line per event, stamped with the run clock;
   *  the counters above are the run so far. Lines from earlier runs stay
   *  until Clear, so a long session can be read back.
   */
  const log = (() => {
    const box = document.getElementById("section-log-box");
    const list = document.getElementById("section-log");
    const live = document.getElementById("section-log-live");
    const summary = document.getElementById("section-log-summary");
    const clear = document.getElementById("section-log-clear");
    const stat = (name) => box?.querySelector(`[data-stat="${name}"]`);
    const cells = {
      elapsed: stat("elapsed"),
      progress: stat("progress"),
      blocks: stat("blocks"),
      model: stat("model"),
    };

    if (!box || !list) {
      // The page has no log box; every call below is a no-op.
      const noop = () => {};
      return { begin: noop, line: noop, stats: noop, end: noop, elapsed: noop };
    }

    let started = 0;
    let running = false;

    const show = (cell, text) => {
      if (cell) cell.querySelector("dd").textContent = text;
    };
    const tone = (cell, name) => {
      if (!cell) return;
      cell.classList.remove("is-busy", "is-ok", "is-fail");
      if (name) cell.classList.add(name);
    };

    /** Append one line. `kind` is info, doc, ok, fail, or stop. */
    function line(kind, text, detail) {
      const empty = list.querySelector(".run-log-empty");
      if (empty) empty.remove();

      const item = document.createElement("li");
      item.className = `run-log-line is-${kind}`;
      const at = document.createElement("span");
      at.className = "run-log-time";
      at.textContent = running ? clock(performance.now() - started) : "—";
      const body = document.createElement("span");
      body.className = "run-log-text";
      body.textContent = text;
      item.append(at, body);
      if (detail) {
        const note = document.createElement("span");
        note.className = "run-log-detail";
        note.textContent = detail;
        item.append(note);
      }
      list.append(item);
      list.scrollTop = list.scrollHeight;
      if (clear) clear.disabled = false;
    }

    function begin(total) {
      if (list.querySelector(".run-log-line")) {
        const rule = document.createElement("li");
        rule.className = "run-log-rule";
        list.append(rule);
      }
      started = performance.now();
      running = true;
      box.classList.add("is-live");
      if (live) live.hidden = false;
      if (clear) clear.disabled = true;
      if (summary) summary.textContent = `Started ${new Date().toLocaleTimeString()}`;
      show(cells.elapsed, "0:00");
      show(cells.progress, `0 / ${total}`);
      show(cells.blocks, "0");
      show(cells.model, "0:00");
      tone(cells.progress, "is-busy");
      tone(cells.blocks, null);
      tone(cells.model, null);
    }

    function elapsed() {
      if (running) show(cells.elapsed, clock(performance.now() - started));
    }

    function stats({ done, total, blocks, modelSeconds, failed }) {
      show(cells.progress, `${done} / ${total}`);
      show(cells.blocks, blocks.toLocaleString());
      show(cells.model, clock(modelSeconds * 1000));
      tone(cells.blocks, blocks ? "is-ok" : null);
      tone(cells.progress, failed ? "is-fail" : "is-busy");
    }

    function end({ ok, failed, left, stopped, modelSeconds }) {
      const took = clock(performance.now() - started);
      const parts = [`✓ ${ok}`];
      if (failed) parts.push(`✗ ${failed}`);
      if (left) parts.push(`${left} left in queue`);
      line(
        stopped ? "stop" : "info",
        `${stopped ? "Stopped" : "Done"} · ${parts.join(" · ")}`,
        `${took} total · ${clock(modelSeconds * 1000)} in the models`,
      );
      if (summary) {
        summary.textContent = `${stopped ? "Stopped" : "Finished"} ${new Date().toLocaleTimeString()} · ${took}`;
      }
      tone(cells.progress, failed ? "is-fail" : "is-ok");
      elapsed();
      running = false;
      box.classList.remove("is-live");
      if (live) live.hidden = true;
      if (clear) clear.disabled = false;
    }

    clear?.addEventListener("click", () => {
      if (running) return;
      list.innerHTML =
        '<li class="run-log-empty">Start a Section run and each document will be logged here.</li>';
      show(cells.elapsed, "0:00");
      show(cells.progress, "0 / 0");
      show(cells.blocks, "0");
      show(cells.model, "0:00");
      tone(cells.progress, null);
      tone(cells.blocks, null);
      if (summary) summary.textContent = "No run yet.";
      clear.disabled = true;
    });

    return { begin, line, stats, end, elapsed };
  })();

  // ---- the queue ---------------------------------------------------------------

  /** One run at a time. A second Section while this is live is refused by
   *  the disabled button, so a single slot is enough. */
  let activeRun = null;

  /** Walk the queue: one POST per hidden row, top to bottom, the log on the
   *  right keeping time. htmx.ajax resolves once the reply is swapped in, so
   *  awaiting it in a loop is what makes the run sequential.
   */
  async function runQueue(card) {
    const rows = Array.from(card.querySelectorAll(".gate-row[data-run-url]"));
    const strip = card.querySelector("[data-run-strip]");
    if (!rows.length || !strip) return;

    const state = strip.querySelector("[data-run-state]");
    const stop = strip.querySelector("[data-run-stop]");

    const run = { stopped: false, currentStarted: 0, started: performance.now() };
    activeRun = run;
    setButton(true);

    const skipped = Array.from(card.querySelectorAll(".gate-skipped"));
    log.begin(rows.length);
    log.line(
      "info",
      `Run started · ${plural(rows.length, "document")} queued`,
      skipped.length ? `${plural(skipped.length, "document")} left out of the queue` : "",
    );
    for (const item of skipped) {
      log.line("stop", `↷ ${item.dataset.name}`, item.dataset.reason);
    }

    const interval = setInterval(() => log.elapsed(), 1000);

    stop.addEventListener("click", () => {
      run.stopped = true;
      stop.disabled = true;
      state.textContent = "⏹ stopping after this document";
      log.line("stop", "Stop requested · finishing the current document");
    });

    let done = 0;
    let ok = 0;
    let failed = 0;
    let blocks = 0;
    let modelSeconds = 0;
    const paint = () => {
      state.textContent = `⚙ ${done} / ${rows.length} · filing blocks · images through the model`;
      log.stats({ done, total: rows.length, blocks, modelSeconds, failed });
    };

    let left = 0;
    for (const row of rows) {
      const name = row.dataset.name || "document";

      if (run.stopped) {
        left += 1;
        continue;
      }

      run.currentStarted = performance.now();
      paint();
      log.line("doc", `▶ ${name}`);

      let requestFailed = false;
      try {
        await htmx.ajax("POST", row.dataset.runUrl, {
          source: row,
          target: row.querySelector(".gate-status"),
          swap: "innerHTML",
        });
      } catch (err) {
        requestFailed = true;
      }

      done += 1;
      // The server's own timing when the reply carries one — that is the
      // processing time; the browser's clock adds the transport on top.
      const waited = (performance.now() - run.currentStarted) / 1000;
      const reported = row.querySelector(".gate-status [data-seconds]");
      const took = reported ? Number(reported.dataset.seconds) || waited : waited;
      const result = row.querySelector(".gate-status [data-content-count]");
      if (result) {
        ok += 1;
        refreshRow(result);
        const stored = Number(result.dataset.contentCount || 0);
        const llm = Number(result.dataset.llmSeconds || 0);
        blocks += stored;
        modelSeconds += llm;
        const facts = [plural(stored, "block")];
        if (result.dataset.summary) facts.push(result.dataset.summary);
        log.line(
          "ok",
          `✓ ${name}`,
          `${facts.join(" · ")} · ${span(took)} (models ${span(llm)})`,
        );
      } else {
        failed += 1;
        const pill = row.querySelector(".gate-status [data-error]");
        const reason = requestFailed
          ? "request failed"
          : pill?.dataset.error || "failed";
        log.line("fail", `✗ ${name}`, `${reason} · ${span(took)}`);
      }
      paint();
    }

    clearInterval(interval);
    if (!run.abandoned) {
      const tally = failed ? `✓ ${ok} · ✗ ${failed}` : `✓ ${ok}`;
      state.textContent = run.stopped ? `⏹ stopped · ${tally}` : `✓ done · ${tally}`;
      state.classList.toggle("ok", !run.stopped);
      stop.hidden = true;
    }
    // An abandoned run's box already belongs to the run that replaced it.
    if (!run.abandoned) log.end({ ok, failed, left, stopped: run.stopped, modelSeconds });
    activeRun = null;
    setButton(false);
  }

  // A run in flight is abandoned when its card is replaced; the request it
  // already sent completes on the server, but the loop stops asking for more.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "section-result" && activeRun) {
      activeRun.stopped = true;
      activeRun.abandoned = true;
      log.line("stop", "Queue replaced · the document in flight finishes, the rest are dropped");
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    if (target.id !== "section-result") return;
    const card = target.querySelector('[data-run="sections"]');
    if (card) runQueue(card);
  });
})();
