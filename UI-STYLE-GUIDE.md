# UI Style Guide — build the UI exactly like this

Instructions for Claude Code. This document describes a complete admin-panel design
system (originally from `partial-server`). Follow it exactly: the token values, the
markup skeleton, and the component recipes below are the spec, not inspiration.
When something is not covered here, derive it from the existing tokens and the
design principles — never invent a new color, radius, or font size.

## Design principles (read first)

1. **Soft, low-contrast surfaces; the data carries the screen.** Cool off-whites
   instead of pure white on grey. Ink is softened from near-black so long lists
   read calmer. Legibility of content always beats visual polish.
2. **Server-rendered HTML + HTMX. No build step.** No npm, no bundler, no SPA
   framework. Interactivity = HTMX first, a small plain `<script>` second.
   Routes answering HTMX requests return HTML *fragments* kept in
   `templates/<feature>/_partials/`.
3. **One accent, many tints.** The indigo accent (`--accent`) is the default.
   Every distinct *kind* of action or object gets its own tint variable
   (`--tint-*`), and buttons/pills derive all their states from that one tint
   via `color-mix()`. Adding a variant is two lines, never a new palette.
4. **Exactly one filled button per screen/dialog** (`.btn-primary`) — the single
   action the screen exists to perform. Everything else is an outline button.
5. **Empty means silent.** An element with nothing to say renders nothing
   (`.foo:empty { display: none; }`) rather than showing a zero or reserving
   space. Undecided reads as quiet, not as a negative answer.
6. **Dense, readable tables over dashboards.** Tabular numerals
   (`font-variant-numeric: tabular-nums`) on every number, sticky table headers,
   hover tint on rows.
7. **Comments explain intent.** CSS comments say *why* a rule exists, in prose.
   Keep that habit.

## Stack assumptions

- Jinja2 (or equivalent) server templates, all extending one `base.html`.
- One stylesheet: `static/css/app.css`. Plain CSS, custom properties, no
  preprocessor. Modern CSS is fine and expected: `color-mix()`, `:has()`,
  `scrollbar-gutter`, native `<dialog>`.
- `htmx.min.js` vendored into `static/js/`, loaded with `defer`.
- Small page-specific scripts as separate files in `static/js/`, also `defer`.

---

## 1. Design tokens

Put this `:root` block at the top of `app.css` verbatim (rename the `--tint-*`
slots to this project's action kinds, keeping the same values where the meaning
matches):

```css
:root {
  /* Surfaces — cool off-whites rather than pure white on grey. */
  --bg: #f4f6fc;
  --panel: #ffffff;
  --panel-soft: #fafbff;
  --sidebar-top: #2b3155;
  --sidebar-bottom: #232845;

  /* Ink — softened from near-black so long lists read calmer. */
  --ink: #2b3050;
  --ink-soft: #6b7391;
  --ink-faint: #9aa1bb;
  --on-dark: #c5cbe4;
  --on-dark-soft: #7b83a6;

  /* Lines and accents. */
  --line: #ebedf7;
  --line-strong: #dfe3f1;
  --accent: #5b6ef5;
  --accent-soft: #eef1fe;
  --accent-tint: #f6f8ff;

  --ok: #2f9367;
  --ok-soft: #eaf7f0;
  --fail: #cf5a5a;
  --fail-soft: #fdf0f0;

  /* Button tints — one per kind of action. A colour variant only swaps
     --btn-tint; everything else in .btn is derived from it. */
  --tint-a: #5b6ef5;   /* indigo  (was: pdf) */
  --tint-b: #b0741d;   /* amber   (was: json) */
  --tint-c: #2f9367;   /* green   (was: md) */
  --tint-d: #8a5cd6;   /* violet  (was: manual — also the .btn-primary tint) */
  --tint-e: #1f8a8a;   /* teal    (was: outline) */
  --tint-f: #c026d3;   /* magenta (was: ai) */
  --tint-g: #0e7490;   /* cyan    (was: transfer) */

  /* Nesting depth tints: level 1 strongest, each step loses saturation so
     nesting reads as distance from the trunk. */
  --lvl-1: #4657d8;
  --lvl-2: #8a5cd6;
  --lvl-3: #b0741d;
  --lvl-4: #2f9367;
  --lvl-5: #8b93b5;

  /* One mono stack; nothing fetched — no webfonts, unavailable faces fall through. */
  --mono: "JetBrains Mono", "Cascadia Code", "Fira Code", "IBM Plex Mono",
          "SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  --radius: 16px;       /* cards, big containers */
  --radius-sm: 10px;    /* inputs, buttons, inner blocks */
  --shadow: 0 1px 2px rgba(43, 48, 80, .04), 0 10px 30px rgba(43, 48, 80, .06);
  --shadow-lift: 0 2px 6px rgba(43, 48, 80, .06), 0 18px 44px rgba(43, 48, 80, .10);
  --ease: .16s cubic-bezier(.4, 0, .2, 1);
}
```

## 2. Base rules

```css
* { box-sizing: border-box; }
[hidden] { display: none !important; }

/* Reserve the scrollbar's lane so the layout never shifts sideways when
   content grows past one viewport. */
html { scrollbar-gutter: stable; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.55 "Pretendard", -apple-system, "Segoe UI", "Malgun Gothic", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

a { color: inherit; text-decoration: none; }
::selection { background: var(--accent-soft); }

:where(button, a, input, select):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 6px;
}
```

Typography scale in use (don't add sizes outside it):
- Page h1: 20px / 650 weight / -.2px letter-spacing
- Card values & timers: 20–22px / 680 / -.3 to -.4px / tabular-nums
- Body & tree names: 13–14px
- Table cells, buttons, meta text: 12.5–13px
- Pills, mono chips, hints: 11–12px
- Overline labels (TH, section labels): 10–11px / 700 / +.8–1.3px letter-spacing / UPPERCASE / `--ink-soft`

Weights: 400 body, 500–550 names, 600 emphasis, 650–680 headings/values, 700 overlines.

## 3. Page shell (base.html)

Every page extends one base template with this exact structure — a dark gradient
sidebar, a white topbar, content on the tinted background:

```html
<body>
  <div class="shell">                      <!-- display:flex; min-height:100vh -->
    <aside class="sidebar">                <!-- sticky, 248px, dark gradient -->
      <div class="brand">
        <span class="brand-mark">📚</span> <!-- emoji as logo, filter:saturate(.9) -->
        <span class="brand-text">
          <strong>appname</strong>
          <small>one-line role</small>     <!-- 10px uppercase, letter-spaced -->
        </span>
      </div>
      <nav class="nav">
        <p class="nav-label">Workspace</p> <!-- 10px/700 uppercase section label -->
        <a class="nav-item is-active" href="...">
          <span class="nav-icon">📄</span><span class="nav-text">Documents</span>
        </a>
        <!-- ... one .nav-item per page, emoji icons, no icon font -->
      </nav>
      <div class="sidebar-foot"><span class="dot"></span> connected</div>
    </aside>

    <main class="main">
      <header class="topbar">
        <div>
          <h1>{% block heading %}{% endblock %}</h1>
          <p class="subheading">{% block subheading %}{% endblock %}</p>
        </div>
        <div class="topbar-actions">{% block actions %}{% endblock %}</div>
      </header>
      <div class="content">{% block content %}{% endblock %}</div>
    </main>
  </div>
</body>
```

Shell CSS essentials:

```css
.shell { display: flex; min-height: 100vh; }

.sidebar {
  position: sticky; top: 0;
  display: flex; flex-direction: column;
  flex: 0 0 248px; height: 100vh;
  padding: 22px 14px 18px;
  background: linear-gradient(180deg, var(--sidebar-top), var(--sidebar-bottom));
  color: var(--on-dark);
}
.nav-item {
  display: flex; align-items: center; gap: 11px;
  padding: 10px 12px; border-radius: 11px;
  color: var(--on-dark); font-size: 13.5px; font-weight: 500;
  transition: background var(--ease), color var(--ease), transform var(--ease);
}
.nav-item:hover { background: rgba(255,255,255,.07); color: #fff; }
.nav-item.is-active {
  background: linear-gradient(135deg, #6274f7, #5566ea);
  color: #fff;
  box-shadow: 0 8px 20px rgba(85,102,234,.32);
}
.sidebar-foot .dot {   /* green status dot with a soft halo */
  display: inline-block; width: 7px; height: 7px; margin-right: 7px;
  border-radius: 50%; background: #4fd39a;
  box-shadow: 0 0 0 3px rgba(79,211,154,.16);
}

.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar {
  display: flex; align-items: flex-end; justify-content: space-between; gap: 18px;
  padding: 24px 30px 20px;
  background: var(--panel); border-bottom: 1px solid var(--line);
}
.topbar h1 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: -.2px; }
.subheading { margin: 5px 0 0; color: var(--ink-soft); font-size: 13px; }
.content { padding: 24px 30px 44px; }
```

Responsive (one breakpoint, 1100px): sidebar collapses to a 70px icon rail
(`.brand-text, .nav-text, .nav-label, .sidebar-foot { display:none }`,
`.nav-item { justify-content:center }`), content padding tightens, two-column
layouts stack to one column.

## 4. Components

### Card

```css
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}
```

Everything sits in cards. Cards never nest visually — when a widget inside a
card already draws its own edge, strip the inner chrome rather than stacking
borders.

### Stat tiles

Grid `repeat(auto-fit, minmax(200px, 1fr))`, gap 16px. Each `.stat` is a card
with a 44px rounded icon square (`background: var(--accent-soft)`, emoji icon),
a 22px/680 value and a 12.5px `--ink-soft` label. Hover: `--shadow-lift` +
`translateY(-1px)`.

### Toolbar

A card-padded flex row (`gap: 11px; padding: 14px 16px`) holding search, selects,
a `.toolbar-spacer { flex:1 }`, and a `.toolbar-count` ("N documents" with the
number in `<strong>`). Inputs:

```css
input[type="search"], select {
  height: 36px; padding: 0 12px;
  border: 1px solid var(--line-strong); border-radius: var(--radius-sm);
  background: var(--panel-soft); color: var(--ink); font: inherit; font-size: 13px;
}
input:focus, select:focus {
  outline: none; background: var(--panel);
  border-color: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);   /* the signature focus ring */
}
```

That focus treatment — accent border + 4px soft ring, background lifts to white —
applies to every text input in the app (swap the tint for themed dialogs).

An HTMX loading spinner lives in the toolbar: a 14px circle border spinner,
`visibility: hidden` until `.htmx-request .spinner { visibility: visible; }`.

### Table

```css
table.docs { width: 100%; border-collapse: collapse; background: var(--panel); font-size: 13px; }
table.docs th {
  position: sticky; top: 0; z-index: 1;
  padding: 12px 16px;
  background: var(--panel-soft); border-bottom: 1px solid var(--line);
  color: var(--ink-soft);
  font-size: 10.5px; font-weight: 700; letter-spacing: .8px;
  text-align: left; text-transform: uppercase; white-space: nowrap;
}
table.docs td { padding: 13px 16px; border-bottom: 1px solid var(--line); vertical-align: middle; }
table.docs tbody tr:hover { background: var(--accent-tint); }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
```

Wrap in `.table-wrap { overflow-x: auto; }`. IDs/hashes/paths render as mono
chips: `.id-chip` — `padding: 3px 8px; border-radius: 7px; background: #f1f3fb;
color: var(--ink-soft); font-family: var(--mono); font-size: 11.5px`. Long names
truncate with ellipsis at a fixed max-width, never wrap.

A `.pager` bar closes the card: top border, `--panel` background, counts left
("x–y of **N**"), prev/next buttons right, 12.5px.

### Buttons

The core pattern — one tint variable drives every state via `color-mix()`:

```css
.btn {
  --btn-tint: var(--accent);
  display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 12px;
  border: 1px solid var(--line-strong); border-radius: var(--radius-sm);
  background: var(--panel); color: var(--ink);
  font: inherit; font-size: 12.5px; font-weight: 600; letter-spacing: .2px;
  white-space: nowrap; cursor: pointer;
  transition: border-color var(--ease), background var(--ease),
              color var(--ease), box-shadow var(--ease);
}
/* Neutral button reveals its tint only on hover. */
.btn:hover {
  border-color: color-mix(in srgb, var(--btn-tint) 34%, #fff);
  background: color-mix(in srgb, var(--btn-tint) 8%, #fff);
  color: var(--btn-tint);
  box-shadow: 0 2px 8px color-mix(in srgb, var(--btn-tint) 18%, transparent);
}
.btn[disabled] {
  border-color: var(--line); background: var(--panel-soft);
  color: var(--ink-faint); box-shadow: none; cursor: not-allowed; pointer-events: none;
}
.btn.ghost { border-color: transparent; background: transparent; }

/* The ONE filled button a screen/dialog gets. */
.btn-primary {
  --btn-tint: var(--tint-d);
  height: 34px; padding: 0 16px;
  border-color: transparent; background: var(--btn-tint); color: #fff;
  box-shadow: 0 2px 10px color-mix(in srgb, var(--btn-tint) 32%, transparent);
}
.btn-primary:hover { background: color-mix(in srgb, var(--btn-tint) 88%, #000); }

/* Tinted outline variants wear their tint at rest — that's what tells one
   action kind from another at a glance in a dense row. One class per kind: */
.btn-a { --btn-tint: var(--tint-a); }   /* ...etc for each kind */
.btn-a, .btn-b, .btn-c /* , ... */ {
  border-color: color-mix(in srgb, var(--btn-tint) 26%, #fff);
  background: color-mix(in srgb, var(--btn-tint) 7%, #fff);
  color: var(--btn-tint);
}
.btn-a:hover /* , ... */ {
  border-color: color-mix(in srgb, var(--btn-tint) 52%, #fff);
  background: color-mix(in srgb, var(--btn-tint) 14%, #fff);
}
```

A tinted button whose target is empty gets `.is-empty` (grey border, faint ink)
so untouched items don't look finished. Small in-row buttons shrink to
`height: 26px; padding: 0 9px; font-size: 11.5px`.

### Pills (status badges)

```css
.pill {
  display: inline-flex; align-items: center; justify-content: center; gap: 4px;
  min-width: 34px; padding: 3px 9px; border-radius: 8px;
  font-size: 11.5px; font-weight: 600;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.pill.ok    { background: var(--ok-soft);   color: var(--ok); }
.pill.fail  { background: var(--fail-soft); color: var(--fail); }
.pill.total { background: #f1f3fb;          color: var(--ink-soft); }
```

Themed pills reuse a tint: `background: color-mix(in srgb, var(--tint-x) 11–14%, #fff);
color: var(--tint-x)`. A pill that counts things related to a tinted button wears
the *same* tint as that button, so count and action read as one thing. A clickable
pill brightens its background on hover. And always: `.some-count:empty { display: none; }`.

### Modal (native `<dialog>`)

```css
dialog.modal {
  width: min(1400px, 96vw); height: 92vh; padding: 0;
  border: 1px solid var(--line); border-radius: 18px;
  background: var(--panel); color: var(--ink);
  box-shadow: 0 30px 80px rgba(30, 35, 66, .30);
  overflow: hidden;
}
dialog.modal[open] { display: flex; flex-direction: column; }
dialog.modal::backdrop { background: rgba(30, 35, 66, .46); backdrop-filter: blur(3px); }
```

Head bar: `.modal-head` — flex row, `padding: 12px 16px`, bottom border,
`--panel-soft` background, ellipsized 13.5px/650 title, actions on the right.
Narrower size variants just override width/height
(`dialog.modal-narrow { width: min(880px, 94vw); height: min(760px, 88vh); }`).
The body gets `flex: 1; min-height: 0; overflow-y: auto`. Modals are injected
into an empty `<div id="modal">` by HTMX and opened by a small script.

### PDF button + pop-up viewer (required on every file row)

Every row that represents a PDF file gets a small tinted **PDF** button. Clicking
it opens the document in a pop-up viewer (a native `<dialog>` holding an
`<iframe>` with the browser's PDF viewer). Ctrl/cmd/shift/middle click still
opens the standalone viewer page in a new tab — the button is a real link.

**1. The button on a file row** (indigo tint, small in-row size):

```html
<a class="btn tree-view btn-pdf" data-viewer
   href="{{ url_for('document_viewer', document_id=file.id) }}">PDF</a>
```

```css
.btn-pdf { --btn-tint: var(--tint-a); }           /* indigo */
.tree-view { height: 26px; padding: 0 9px; font-size: 11.5px; }
```

**2. One empty mount point per page** that can open the viewer, plus the script:

```html
<div id="modal"></div>
<script src="{{ static_url('js/viewer-modal.js') }}" defer></script>
```

**3. The viewer route serves two shapes from one URL** — a dialog fragment for
HTMX, a full page otherwise, so the viewer stays deep-linkable and survives a
ctrl-click into a new tab:

```python
@router.get("/{document_id}/viewer", response_class=HTMLResponse, name="document_viewer")
def document_viewer(request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    template = (
        "documents/_partials/viewer_modal.html"
        if request.headers.get("hx-request")
        else "documents/viewer.html"
    )
    return templates.TemplateResponse(request, template, {"doc": doc})
```

**4. The dialog partial** (`_partials/viewer_modal.html`) — the PDF and a slim
bar to close it, nothing else, so the document gets the whole dialog:

```html
<dialog class="modal" aria-label="{{ doc.filename }}">
  <header class="modal-head">
    <strong class="modal-title" title="{{ doc.filename }}">{{ doc.filename }}</strong>
    <button type="button" class="btn" data-close-modal>Close</button>
  </header>
  <iframe class="modal-pdf"
          src="{{ url_for('document_file', document_id=doc.id) }}#zoom=100"
          title="{{ doc.filename }}"></iframe>
</dialog>
```

```css
.modal-pdf { display: block; flex: 1; width: 100%; min-height: 0; border: 0; }
```

**5. The bytes endpoint** streams the PDF through the server (never a presigned
blob-store URL, so the object store stays unreachable from the browser), with
`Content-Disposition: inline` and the filename RFC 5987-encoded — a raw UTF-8
(e.g. Korean) filename is not valid latin-1 and the header would fail to send:

```python
headers = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(doc.filename)}"}
return StreamingResponse(obj.chunks, media_type="application/pdf", headers=headers)
```

**6. `static/js/viewer-modal.js`** — one shared, delegated script; copy verbatim:

```js
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
```

Behavior you get for free and must preserve: backdrop click closes, Esc closes
(native `<dialog>`), the dialog is removed from the DOM on close so the iframe
releases the PDF, and the same `[data-viewer]` mechanism works for any other
pop-up content (parsed JSON, markdown) — not just PDFs.

### Two-column work layout

Pages that pair a big browser (tree/table) with run controls:

```css
.work-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 18px; align-items: start;
}
.side-panel { position: sticky; top: 20px; display: flex; flex-direction: column; gap: 16px; }
@media (max-width: 1100px) { .work-layout { grid-template-columns: 1fr; } .side-panel { position: static; } }
```

Side boxes are cards with `padding: 14px 16px`, a 12.5px/700 title row, and an
optional faint `.side-note`.

### Tree browser

Folder/file tree inside a card: `.tree-head` strip (soft background, bottom
border, label + count pills right), rows 34px min-height with 10px radius and
`--accent-tint` hover, children indented by `padding-left: 26px`. The folder
*name* is the open/close button (no separate arrow); open state swaps an
emoji icon pair via `.is-open`. Checkbox per row (`accent-color: var(--accent)`);
disabled checkboxes drop to `.4` opacity and soften the row's name — settled, not
missing. Badges and small buttons sit right.

### Live / running states

- **Live ring:** an auto-refreshing box gets
  `border-color: color-mix(in srgb, var(--ok) 38%, #fff); box-shadow: 0 0 0 4px var(--ok-soft), var(--shadow);`
  plus a pulsing 8px `.live-dot` (keyframe ring expanding from the dot).
- **Run strip:** a soft inner panel with a big tabular-nums timer
  (18px/680) as the dominant element, progress text in `--ink-soft`.
- **Queue rows:** one flex row per item, top border, name ellipsized left,
  status pills right; the running row gets `background: var(--accent-soft)`.
- **Value bump:** when a number changes, add a class that plays
  `scale(1) → 1.22 → 1` over .6s.
- **Service state:** `.is-up` green soft panel / `.is-down` red soft panel, 9px
  `currentColor` dot, pulsing when up.

### Monospace output panes

Extracted/raw content is judged in a plain mono block:

```css
.output-text {
  flex: 1 1 auto; margin: 0; min-height: 0; padding: 18px 22px 26px;
  overflow: auto; overscroll-behavior: contain;
  background: var(--panel); color: var(--ink);
  font-family: var(--mono); font-size: 12.5px;
  font-variant-ligatures: none; line-height: 1.7; tab-size: 2;
  white-space: pre;            /* structure (JSON): never wrap */
}
.output-text.is-prose {        /* prose (Markdown): wraps, looser measure */
  padding: 20px 28px 30px 40px; font-size: 13px; line-height: 1.85;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
```

Slim custom scrollbars on these panes: 12px, `--panel-soft` track, `#ccd2e8`
thumb with 3px track-colored border and 999px radius, `#b3bcdd` on hover.

Inline error banner above an output pane: `background: var(--fail-soft);
color: var(--fail); font-size: 12.5px; font-weight: 600; padding: 10px 18px`.

### Nested outline (hierarchical lists)

Depth is carried three ways at once — indent (18px per level), a 3px colored
rail (`--lvl-1`…`--lvl-5` via a `--lvl-tint` custom property), and type weight
(lvl-1: 14px/680 → lvl-4/5: 12.5px `--ink-soft`) — so a mis-nested item is
visible without reading it. Rows hover `--accent-tint`. A row with broken data
gets a solid red ground (`#ef4444`, hover `#dc2626`) with white ink — reserved
for "this exact row is what the edit mode exists to fix".

### Empty states

`.empty { padding: 56px 20px; color: var(--ink-soft); text-align: center; }`
with a 32px emoji above one sentence. Placeholder pages: same, bigger
(62px padding, 44px emoji, short h2 + one `--ink-soft` paragraph, max-width 470px).

## 5. HTMX + JS conventions

- Full pages extend `base.html`; HTMX endpoints return fragments from
  `templates/<feature>/_partials/`.
- Dialogs: a button `hx-get`s the dialog partial into `<div id="modal">`,
  a deferred script calls `showModal()`. Close buttons are plain `.btn ghost`.
- Loading: HTMX's `.htmx-request` toggles spinner visibility; a busy primary
  button gets `.is-loading[disabled]` — lighter fill (`color-mix 62% accent + #fff`)
  plus a 12px white border-spinner inside the button.
- Edit modes toggle a state class on a container (`.is-editing`) and the CSS
  swaps read-only pills for inputs — no DOM rebuilding.
- Prefer `:has()` for state that follows the DOM (e.g. revealing a dependent
  field while an option is selected) so the UI is right even if the script is
  stale.
- Scripts are small, per-feature files (`tree.js`, `viewer-modal.js`, …),
  loaded with `defer` at the end of the page block that needs them.
- Icons are emoji everywhere (nav, buttons, placeholders, brand). No icon font,
  no SVG sprite sheet.

## 6. Checklist for every new screen

- [ ] Extends `base.html`; sets title (`Page · appname`), emoji heading, one-line subheading.
- [ ] Content sits in `.card`s on the `--bg` canvas; paddings from the shell (24/30px).
- [ ] Any list is paginated/virtualized server-side; long strings ellipsize.
- [ ] Numbers use tabular-nums; counts hide when empty.
- [ ] At most one `.btn-primary`; other actions are tinted outline buttons, one tint per action kind.
- [ ] All interactive states derive from tokens via `color-mix()` — no new hex values outside `:root`.
- [ ] Focus-visible ring on everything; inputs use the accent focus ring.
- [ ] Works at <1100px: sidebar rail, columns stack.
- [ ] Partial updates via HTMX fragments; no inline oversized scripts.
- [ ] Every file row has a `data-viewer` PDF button opening the pop-up viewer; the page has `<div id="modal">` and loads `viewer-modal.js`.
