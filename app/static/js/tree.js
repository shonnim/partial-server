// Parsing tree: expand/collapse, checkbox propagation, and the picked counts.
//
// Selection runs both ways. Checking a folder checks everything under it;
// checking the last unchecked sibling checks the parent, and unchecking any
// sibling knocks the parent back to indeterminate. Because levels load lazily,
// a folder checked before its children exist passes that state down when they
// arrive.
(() => {
  "use strict";

  const nodeOf = (el) => el.closest(".tree-node");
  const childListOf = (node) => node.querySelector(":scope > .tree-children");
  const boxOf = (node) => node.querySelector(":scope > .tree-row > .tree-check");
  const badgesOf = (node) => node.querySelector(":scope > .tree-row > .tree-badges");
  const isFile = (node) => node.classList.contains("is-file");

  /** Immediate child nodes — not the whole subtree. */
  function directNodes(node) {
    const list = childListOf(node);
    if (!list) return [];
    return Array.from(list.children).filter((li) => li.classList.contains("tree-node"));
  }

  const directBoxes = (node) => directNodes(node).map(boxOf).filter(Boolean);

  function setSubtree(node, checked) {
    const list = childListOf(node);
    if (!list) return;
    list.querySelectorAll(".tree-check").forEach((box) => {
      box.checked = checked;
      box.indeterminate = false;
    });
  }

  /** Derive one folder's state from the children currently rendered under it. */
  function refresh(node) {
    const boxes = directBoxes(node);
    const box = boxOf(node);
    if (!box || boxes.length === 0) return; // not expanded yet — leave as set

    const checked = boxes.filter((b) => b.checked).length;
    const partial = boxes.some((b) => b.indeterminate) ||
      (checked > 0 && checked < boxes.length);

    box.checked = !partial && checked === boxes.length;
    box.indeterminate = partial;
  }

  function refreshAncestors(node) {
    let parent = node.parentElement && node.parentElement.closest(".tree-node");
    while (parent) {
      refresh(parent);
      parent = parent.parentElement && parent.parentElement.closest(".tree-node");
    }
  }

  // ---- picked counts -------------------------------------------------------

  /** How many documents beneath this node are selected.
   *
   *  A fully-checked folder counts its whole subtree from the server-supplied
   *  data-total, so the number is right even when the folder was never opened.
   *  Indeterminate implies the children are loaded, so that case can recurse.
   */
  function pickedCount(node) {
    const box = boxOf(node);
    if (!box) return 0;
    if (isFile(node)) return box.checked ? 1 : 0;
    if (box.indeterminate) {
      return directNodes(node).reduce((n, child) => n + pickedCount(child), 0);
    }
    return box.checked ? Number(node.dataset.total || 0) : 0;
  }

  function paint(badges, count) {
    if (!badges) return;
    const pill = badges.querySelector(".tree-picked");
    if (!pill) return;
    pill.hidden = count === 0;
    pill.querySelector(".tree-picked-n").textContent = count;
  }

  function updateCounts() {
    const root = document.getElementById("tree-root");
    if (!root) return;

    document.querySelectorAll(".tree-node:not(.is-file)").forEach((node) => {
      paint(badgesOf(node), pickedCount(node));
    });

    const total = Array.from(root.children)
      .filter((li) => li.classList.contains("tree-node"))
      .reduce((n, li) => n + pickedCount(li), 0);

    paint(document.getElementById("tree-root-badges"), total);

    const label = document.getElementById("tree-selection");
    if (label) {
      label.textContent = total === 0
        ? "Nothing selected"
        : `${total} file${total === 1 ? "" : "s"} selected`;
    }
  }

  // ---- events --------------------------------------------------------------

  document.addEventListener("change", (event) => {
    const box = event.target.closest(".tree-check");
    if (!box) return;

    const node = nodeOf(box);
    box.indeterminate = false;
    setSubtree(node, box.checked);
    refreshAncestors(node);
    updateCounts();
  });

  // Clicking a folder's name opens it. htmx loads the level on the first
  // click; every click after that just shows or hides what is already there.
  document.addEventListener("click", (event) => {
    const label = event.target.closest(".tree-label");
    if (!label) return;

    const node = nodeOf(label);
    const list = childListOf(node);
    if (!list) return;

    list.hidden = !list.hidden;
    node.classList.toggle("is-open", !list.hidden);
    label.setAttribute("aria-expanded", String(!list.hidden));
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;

    // A level just loaded: inherit the parent's tick, then re-derive upwards
    // so a fully-checked folder lights up its own ancestors.
    const list = target.classList.contains("tree-children")
      ? target
      : target.closest(".tree-children");
    if (!list) return;

    const node = list.closest(".tree-node");
    if (node) {
      const box = boxOf(node);
      if (box && box.checked) setSubtree(node, true);
      else refresh(node);
      refreshAncestors(node);
    }
    updateCounts();
  });
})();
