// The pop-up viewer, shared by the documents list and the parsing tree. It
// carries whatever the route returns — the PDF, the parsed JSON, the markdown.
//
// A plain click on a [data-viewer] link opens it in a <dialog>; ctrl/cmd/shift
// and middle clicks fall through to the href so the standalone page still opens
// in a new tab. Requires a <div id="modal"> on the page.
(() => {
  "use strict";

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-viewer]");
    if (!link) return;
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;

    event.preventDefault();
    htmx.ajax("GET", link.href, { target: "#modal", swap: "innerHTML" });
  });

  // htmx delivers the viewer as a closed <dialog>; open it on arrival and throw
  // it away on close so the iframe stops holding on to the PDF.
  document.body.addEventListener("htmx:afterSwap", (event) => {
    const container = event.detail.target;
    if (container.id !== "modal") return;

    const dialog = container.querySelector("dialog");
    if (!dialog) return;

    // Clear on close, but only while this dialog is still the one on screen.
    // close() fires its event asynchronously, so a quick close-then-open can
    // land the old dialog's event after the new one has been swapped in —
    // without this guard that stale event blanks the pop-up that just opened.
    dialog.addEventListener("close", () => {
      if (dialog.parentElement === container) container.innerHTML = "";
    });
    dialog.addEventListener("click", (e) => {
      // A click landing on the dialog itself is a click on the backdrop.
      if (e.target === dialog || e.target.closest("[data-close-modal]")) dialog.close();
    });
    dialog.showModal();
  });
})();
