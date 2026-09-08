// The Parsing screen: pushing the parse queue through the service, and
// keeping the service status box current.
//
// A run is two loops over the same rows. The submit loop uploads one document
// at a time — a 202 for one is what lets the next one go, so the service is
// never asked to take two uploads at once but its own queue fills up without
// waiting for parses to finish. The poll loop asks after every task in
// flight on a clock; the poll that finds a task completed is the one whose
// reply carries the stored output. The run is over when the last submission
// has been made and nothing is left in flight.
(() => {
  "use strict";

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** Seconds as m:ss — minutes are not capped, so 61 minutes reads 61:00. */
  function clock(ms) {
    const total = Math.floor(ms / 1000);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  // ---- the Parse docs button ------------------------------------------------

  /** Disabled and spinning while a queue is live; back to itself after. */
  function setParseButton(running) {
    const button = document.getElementById("btn-parse");
    if (!button) return;
    if (running) {
      button.dataset.running = "1";
      button.disabled = true;
      button.classList.add("is-loading");
      button.innerHTML = '<span class="btn-spinner"></span> Parsing…';
    } else {
      delete button.dataset.running;
      button.disabled = false;
      button.classList.remove("is-loading");
      button.textContent = button.dataset.label || "Parse docs";
    }
  }

  // ---- repainting the tree ---------------------------------------------------

  /** Bring a tree row up to date after its parse finished.
   *
   *  A document that has just been through the VLM engine is out of the
   *  selection from now on, so its box is disabled here too — otherwise it
   *  stays tickable until a reload and the next batch would quietly parse it
   *  again. The backend pill and the JSON / MD buttons follow.
   */
  function refreshRow(pill) {
    const id = pill.dataset.docId;
    const row = document.querySelector(`.tree-node[data-id="${id}"]`);
    if (!row) return; // row is in a folder that was never opened

    row.dataset.backend = "vlm-engine";

    const backend = row.querySelector(".parse-backend");
    if (backend) backend.innerHTML = '<span class="pill backend vlm">vlm-engine</span>';

    const links = row.querySelector(".parse-links");
    if (links && pill.dataset.jsonUrl && pill.dataset.mdUrl) {
      links.innerHTML =
        `<a class="btn tree-view btn-json" data-viewer href="${pill.dataset.jsonUrl}">JSON</a>` +
        `<a class="btn tree-view btn-md" data-viewer href="${pill.dataset.mdUrl}">MD</a>`;
    }

    const box = row.querySelector(":scope > .tree-row > .tree-check");
    if (box) {
      box.checked = false;
      box.indeterminate = false;
      box.disabled = true;
      box.title = "Already parsed with vlm-engine";
    }
    if (window.refreshTreeCounts) window.refreshTreeCounts(row);
  }

  // ---- the queue -------------------------------------------------------------

  /** One run at a time. A second Parse docs while this is live is refused by
   *  the disabled button, so a single slot is enough. */
  let activeRun = null;

  const cellOf = (row) => row.querySelector(".gate-status");
  const isLive = (row) => Boolean(cellOf(row).querySelector("[data-task-id]"));

  /** Upload one row. Resolves once the service has answered — with a task
   *  still in flight, or with a failure the cell now shows. */
  async function submitRow(row) {
    const cell = cellOf(row);
    row.dataset.started = String(performance.now());
    row.classList.add("is-running");
    cell.innerHTML =
      '<span class="pill total is-live">⬆ uploading · <span data-row-timer>0:00</span></span>';

    try {
      await htmx.ajax("POST", row.dataset.submitUrl, {
        source: row,
        target: cell,
        swap: "innerHTML",
      });
    } catch (err) {
      cell.innerHTML =
        '<span class="pill fail" data-done>✗ failed</span><span class="gate-reason">request failed</span>';
    }
  }

  /** Ask after one in-flight row. The reply replaces the cell: still in
   *  flight, stored, or failed. A poll that never reached the server leaves
   *  the cell as it was and is simply tried again on the next tick. */
  async function pollRow(row) {
    const cell = cellOf(row);
    const live = cell.querySelector("[data-task-id]");
    if (!live) return;

    try {
      await htmx.ajax("POST", row.dataset.pollUrl, {
        source: row,
        target: cell,
        swap: "innerHTML",
        values: { task_id: live.dataset.taskId },
      });
    } catch (err) {
      // retried on the next tick
    }
  }

  async function runQueue(card) {
    const rows = Array.from(card.querySelectorAll(".gate-row[data-submit-url]"));
    const strip = card.querySelector("[data-run-strip]");
    if (!rows.length || !strip) return;

    const timer = strip.querySelector("[data-run-timer]");
    const state = strip.querySelector("[data-run-state]");
    const progress = strip.querySelector("[data-run-progress]");
    const tally = strip.querySelector("[data-run-tally]");
    const stop = strip.querySelector("[data-run-stop]");
    const pollMs = Number(card.dataset.pollSeconds || 10) * 1000;

    const run = {
      stopped: false,
      abandoned: false,
      submitting: true,
      started: performance.now(),
      inFlight: new Set(),
    };
    activeRun = run;
    setParseButton(true);

    let sent = 0;
    let done = 0;
    let ok = 0;
    let failed = 0;

    const paint = () => {
      progress.textContent = `${done} / ${rows.length}`;
      tally.textContent = failed ? `✓ ${ok} · ✗ ${failed}` : ok ? `✓ ${ok}` : "";

      const flying = run.inFlight.size;
      if (run.stopped) {
        state.textContent = flying
          ? `⏹ stopping · ${plural(flying, "document")} still parsing`
          : "⏹ stopping";
      } else if (run.submitting) {
        state.textContent = `⬆ sending ${Math.min(sent + 1, rows.length)} of ${rows.length}` +
          (flying ? ` · ⚙ ${flying} parsing` : "");
      } else {
        state.textContent = `⚙ ${plural(flying, "document")} parsing`;
      }
    };

    const tick = () => {
      const now = performance.now();
      timer.textContent = clock(now - run.started);
      run.inFlight.forEach((row) => {
        const rowTimer = row.querySelector("[data-row-timer]");
        if (rowTimer) rowTimer.textContent = clock(now - Number(row.dataset.started));
      });
    };
    const interval = setInterval(tick, 1000);

    stop.addEventListener("click", () => {
      run.stopped = true;
      stop.disabled = true;
      paint();
    });

    /** A row whose cell has stopped asking: count it and repaint its tree row. */
    const settle = (row) => {
      run.inFlight.delete(row);
      row.classList.remove("is-running");
      done += 1;
      const result = cellOf(row).querySelector("[data-done]");
      if (result && result.classList.contains("ok")) {
        ok += 1;
        refreshRow(result);
      } else {
        failed += 1;
      }
    };

    // Upload one at a time. A submission that comes back already settled — a
    // missing PDF, a rejected task — is counted on the spot; one that comes
    // back in flight is left for the poll loop.
    const submitAll = async () => {
      for (const row of rows) {
        if (run.abandoned) return;
        if (run.stopped) {
          cellOf(row).innerHTML = '<span class="pill total">— left in queue</span>';
          continue;
        }
        run.inFlight.add(row);
        paint();
        await submitRow(row);
        if (run.abandoned) return;
        sent += 1;
        if (!isLive(row)) settle(row);
        paint();
      }
    };

    // Ask after everything in flight, in turn, every tick. A row still
    // uploading has no task id yet and is skipped until it does.
    const pollAll = async () => {
      for (;;) {
        await sleep(pollMs);
        if (run.abandoned) return;
        if (!run.submitting && run.inFlight.size === 0) return;

        for (const row of Array.from(run.inFlight)) {
          if (run.abandoned) return;
          if (!isLive(row)) continue;
          await pollRow(row);
          if (run.abandoned) return;
          if (!isLive(row)) settle(row);
          paint();
        }
      }
    };

    paint();
    tick();
    const polling = pollAll();
    await submitAll();
    run.submitting = false;
    paint();
    await polling;

    clearInterval(interval);
    tick();
    if (!run.abandoned) {
      state.textContent = run.stopped ? "⏹ stopped" : "✓ done";
      state.classList.toggle("ok", !run.stopped);
      stop.hidden = true;
    }
    activeRun = null;
    setParseButton(false);
  }

  // A run in flight is abandoned when its card is replaced. The tasks it
  // submitted keep running on the service and are picked up again by the next
  // Parse docs over the same documents.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "parse-result" && activeRun) activeRun.abandoned = true;
  });

  // ---- the service status box -----------------------------------------------

  let autoTimer = null;
  const previous = {};

  function healthUrl() {
    const button = document.getElementById("btn-check-now");
    return button ? button.getAttribute("hx-get") : null;
  }

  // The button is named as the source so its own spinner is the indicator;
  // with no source htmx marks the body as busy, which lights every spinner
  // on the page.
  function checkHealth() {
    const button = document.getElementById("btn-check-now");
    const url = healthUrl();
    if (!url || !button) return;
    htmx.ajax("GET", url, { source: button, target: "#service-status", swap: "innerHTML" });
  }

  function setAuto(on) {
    const box = document.getElementById("service-box");
    const button = document.getElementById("btn-auto-check");
    const badge = document.getElementById("live-badge");
    if (!box || !button) return;

    if (autoTimer) {
      clearInterval(autoTimer);
      autoTimer = null;
    }
    if (on) {
      const seconds = Number(box.dataset.healthSeconds || 5);
      checkHealth();
      autoTimer = setInterval(checkHealth, seconds * 1000);
    }

    box.classList.toggle("is-live", on);
    button.setAttribute("aria-pressed", String(on));
    button.textContent = on ? "■ Auto check" : "▶ Auto check";
    if (badge) badge.hidden = !on;
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("#btn-auto-check")) setAuto(autoTimer === null);
  });

  /** Stamp the check time and flash whatever moved since the last check. */
  function settleHealth(target) {
    const stamp = target.querySelector("[data-checked]");
    if (stamp) stamp.textContent = new Date().toLocaleTimeString();

    let changed = false;
    target.querySelectorAll("[data-stat]").forEach((el) => {
      const key = el.dataset.stat;
      const value = el.dataset.value;
      if (key in previous && previous[key] !== value) {
        el.classList.add("bump");
        el.addEventListener("animationend", () => el.classList.remove("bump"), { once: true });
        changed = true;
      }
      previous[key] = value;
    });

    const box = document.getElementById("service-box");
    if (box && box.classList.contains("is-live")) {
      box.classList.remove("did-check");
      void box.offsetWidth; // restart the animation
      box.classList.add("did-check");
      if (changed) box.classList.add("did-change");
      setTimeout(() => box.classList.remove("did-change"), 900);
    }
  }

  // ---- events ----------------------------------------------------------------

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;

    if (target.id === "parse-result") {
      const card = target.querySelector('[data-run="parse"]');
      if (card) runQueue(card);
      return;
    }

    if (target.id === "service-status") settleHealth(target);
  });

  // Leaving the page while Auto check is on has nothing to clean up: the
  // interval dies with the document. But the button must not stay pressed
  // across a back-forward cache restore with no timer behind it.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && autoTimer === null) setAuto(false);
  });
})();
