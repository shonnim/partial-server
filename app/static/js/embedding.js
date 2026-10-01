// The Embedding screen: walking the queue one document at a time with a
// clock running, repainting the embed badge on the tree rows a run just
// settled, and keeping the run log on the right up to date.
//
// The selection itself — ids and folder paths, as the server wants them —
// is serialised by tree.js (window.treeSelectionJSON), shared with the
// other tree pages.
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

  // ---- the Embed button ---------------------------------------------------------

  const button = document.getElementById("btn-embed");
  const counter = document.getElementById("embed-selected");

  /** Disabled while nothing is ticked and while a queue is live. */
  let running = false;

  function pickedTotal() {
    const n = document.querySelector("#tree-root-badges .tree-picked-n");
    return n ? Number(n.textContent || 0) : 0;
  }

  function sync() {
    if (!button) return;
    const total = pickedTotal();
    if (counter) counter.textContent = total.toLocaleString();
    button.disabled = running || total === 0;
    button.title = total === 0 ? "Tick at least one document or folder" : "";
  }

  function setRunning(state) {
    running = state;
    if (!button) return;
    if (state) {
      button.classList.add("is-loading");
      button.innerHTML = '<span class="btn-spinner"></span> Embedding…';
    } else {
      button.classList.remove("is-loading");
      button.textContent = button.dataset.label || "Embed";
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

  /** Keep a row honest after a run: the embed badge, and the box.
   *
   *  A fully embedded document leaves the selection, so its box is disabled
   *  here too — otherwise it stays tickable until a reload, and the next
   *  batch would quietly queue a document that is done. Deleting vectors
   *  hands the box back the same way.
   */
  function paintRow(id, embedded, total, failed, done) {
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // row is in a folder that was never opened
    const cell = row.querySelector(".embed-count");
    if (cell) {
      cell.dataset.total = String(total);
      if (total > 0) {
        const tone = failed > 0 ? "fail" : embedded >= total ? "ok" : "total";
        const title = `embedded / total chunks${failed ? ` · ${failed} failed` : ""}`;
        cell.innerHTML = `<span class="pill ${tone}" title="${title}">🧲 ${embedded}/${total}</span>`;
      } else {
        cell.innerHTML = "";
      }
    }

    const box = row.querySelector(":scope > .tree-row > .tree-check");
    if (box) {
      box.disabled = Boolean(done);
      box.title = done ? "already embedded" : "";
      if (box.disabled) {
        box.checked = false;
        box.indeterminate = false;
      }
    }
    if (window.refreshTreeCounts) window.refreshTreeCounts(row);
  }

  // ---- the run log ---------------------------------------------------------------

  const log = (() => {
    const box = document.getElementById("embed-log-box");
    const list = document.getElementById("embed-log");
    const live = document.getElementById("embed-log-live");
    const summary = document.getElementById("embed-log-summary");
    const clear = document.getElementById("embed-log-clear");
    const stat = (name) => box?.querySelector(`[data-stat="${name}"]`);
    const cells = {
      elapsed: stat("elapsed"),
      progress: stat("progress"),
      chunks: stat("chunks"),
      tokens: stat("tokens"),
    };

    if (!box || !list) {
      const noop = () => {};
      return { begin: noop, line: noop, stats: noop, end: noop, elapsed: noop };
    }

    let started = 0;
    let liveNow = false;

    const show = (cell, text) => {
      if (cell) cell.querySelector("dd").textContent = text;
    };
    const tone = (cell, name) => {
      if (!cell) return;
      cell.classList.remove("is-busy", "is-ok", "is-fail");
      if (name) cell.classList.add(name);
    };

    function line(kind, text, detail) {
      const empty = list.querySelector(".run-log-empty");
      if (empty) empty.remove();

      const item = document.createElement("li");
      item.className = `run-log-line is-${kind}`;
      const at = document.createElement("span");
      at.className = "run-log-time";
      at.textContent = liveNow ? clock(performance.now() - started) : "—";
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
      liveNow = true;
      box.classList.add("is-live");
      if (live) live.hidden = false;
      if (clear) clear.disabled = true;
      if (summary) summary.textContent = `Started ${new Date().toLocaleTimeString()}`;
      show(cells.elapsed, "0:00");
      show(cells.progress, `0 / ${total}`);
      show(cells.chunks, "0");
      show(cells.tokens, "0");
      tone(cells.progress, "is-busy");
      tone(cells.chunks, null);
    }

    function elapsed() {
      if (liveNow) show(cells.elapsed, clock(performance.now() - started));
    }

    function stats({ done, total, chunks, tokens, failed }) {
      show(cells.progress, `${done} / ${total}`);
      show(cells.chunks, chunks.toLocaleString());
      show(cells.tokens, tokens.toLocaleString());
      tone(cells.chunks, chunks ? "is-ok" : null);
      tone(cells.progress, failed ? "is-fail" : "is-busy");
    }

    function end({ ok, failed, left, stopped }) {
      const took = clock(performance.now() - started);
      const parts = [`✓ ${ok}`];
      if (failed) parts.push(`✗ ${failed}`);
      if (left) parts.push(`${left} left in queue`);
      line(stopped ? "stop" : "info", `${stopped ? "Stopped" : "Done"} · ${parts.join(" · ")}`, `${took} total`);
      if (summary) {
        summary.textContent = `${stopped ? "Stopped" : "Finished"} ${new Date().toLocaleTimeString()} · ${took}`;
      }
      tone(cells.progress, failed ? "is-fail" : "is-ok");
      elapsed();
      liveNow = false;
      box.classList.remove("is-live");
      if (live) live.hidden = true;
      if (clear) clear.disabled = false;
    }

    clear?.addEventListener("click", () => {
      if (liveNow) return;
      list.innerHTML =
        '<li class="run-log-empty">Start an Embed run and each document will be logged here.</li>';
      show(cells.elapsed, "0:00");
      show(cells.progress, "0 / 0");
      show(cells.chunks, "0");
      show(cells.tokens, "0");
      tone(cells.progress, null);
      tone(cells.chunks, null);
      if (summary) summary.textContent = "No run yet.";
      clear.disabled = true;
    });

    return { begin, line, stats, end, elapsed };
  })();

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

    const run = { stopped: false, started: performance.now() };
    activeRun = run;
    setRunning(true);

    const skipped = Array.from(card.querySelectorAll(".gate-skipped"));
    log.begin(rows.length);
    log.line(
      "info",
      `Run started · ${plural(rows.length, "document")} queued${force ? " · force" : ""}`,
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
    let chunks = 0;
    let tokens = 0;
    const paint = () => {
      state.textContent = `⚙ ${done} / ${rows.length} · chunks through the embedder`;
      log.stats({ done, total: rows.length, chunks, tokens, failed });
    };

    let left = 0;
    for (const row of rows) {
      const name = row.dataset.name || "document";

      if (run.stopped) {
        left += 1;
        continue;
      }

      const startedAt = performance.now();
      paint();
      log.line("doc", `▶ ${name}`, plural(Number(row.dataset.chunks || 0), "chunk"));

      let requestFailed = false;
      try {
        await htmx.ajax("POST", row.dataset.runUrl, {
          source: row,
          target: row.querySelector(".gate-status"),
          swap: "innerHTML",
          values: force ? { force: "1" } : {},
        });
      } catch (err) {
        requestFailed = true;
      }

      done += 1;
      const waited = (performance.now() - startedAt) / 1000;
      const result = row.querySelector(".gate-status [data-embed-result]");
      if (result) {
        const took = Number(result.dataset.seconds) || waited;
        const stored = Number(result.dataset.embedded || 0);
        const bad = Number(result.dataset.failed || 0);
        chunks += stored;
        tokens += Number(result.dataset.tokens || 0);
        if (bad) failed += 1;
        else ok += 1;
        paintRow(
          result.dataset.docId,
          Number(result.dataset.embeddedTotal || 0),
          Number(result.dataset.total || 0),
          bad,
          result.dataset.done === "1",
        );
        refreshOpenPanel(result.dataset.docId);
        const facts = [plural(stored, "chunk")];
        if (bad) facts.push(`${bad} failed`);
        if (result.dataset.summary) facts.push(result.dataset.summary);
        log.line(bad ? "fail" : "ok", `${bad ? "✗" : "✓"} ${name}`, `${facts.join(" · ")} · ${span(took)}`);
      } else {
        failed += 1;
        const pill = row.querySelector(".gate-status [data-error]");
        const reason = requestFailed ? "request failed" : pill?.dataset.error || "failed";
        log.line("fail", `✗ ${name}`, `${reason} · ${span(waited)}`);
      }
      paint();
    }

    clearInterval(interval);
    if (!run.abandoned) {
      const tally = failed ? `✓ ${ok} · ✗ ${failed}` : `✓ ${ok}`;
      state.textContent = run.stopped ? `⏹ stopped · ${tally}` : `✓ done · ${tally}`;
      state.classList.toggle("ok", !run.stopped && !failed);
      stop.hidden = true;
      log.end({ ok, failed, left, stopped: run.stopped });
    }
    activeRun = null;
    setRunning(false);
  }

  /** The center panel follows the queue: when the document it shows just
   *  finished, ask for it again so the statuses on screen are the stored
   *  ones. */
  function refreshOpenPanel(docId) {
    const open = document.querySelector(`#embed-panel [data-panel-doc="${docId}"]`);
    if (open && window.htmx) {
      htmx.ajax("GET", `/embedding/${docId}/panel`, { target: "#embed-panel", swap: "innerHTML" });
    }
  }

  // A run in flight is abandoned when its card is replaced; the request it
  // already sent completes on the server, but the loop stops asking for more.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "embed-result" && activeRun) {
      activeRun.stopped = true;
      activeRun.abandoned = true;
      log.line("stop", "Queue replaced · the document in flight finishes, the rest are dropped");
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    if (target.id === "embed-result") {
      const card = target.querySelector('[data-run="embedding"]');
      if (card) runQueue(card);
      return;
    }
    // A panel action (embed pending, delete vectors) settled: mirror the
    // panel's new numbers onto the tree badge.
    if (target.id === "embed-panel") {
      const panel = target.querySelector("[data-panel-doc]");
      if (panel) {
        paintRow(
          panel.dataset.panelDoc,
          Number(panel.dataset.embeddedTotal || 0),
          Number(panel.dataset.total || 0),
          Number(panel.dataset.failed || 0),
          panel.dataset.done === "1",
        );
      }
    }
  });
})();
