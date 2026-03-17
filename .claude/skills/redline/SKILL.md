---
name: redline
description: |
  Visual UI feedback tool — annotate elements in a running web app, then fix them from annotation files.
  Two modes: `/redline setup` guides installation of the Chrome extension or Tauri plugin.
  `/redline <path>` reads an annotation JSON file and fixes all annotated issues in the codebase.
  Use this skill whenever the user mentions: redline, UI annotations, visual feedback, annotate elements,
  fix annotations, paint feedback on UI, mark up the UI, visual code review of a running app,
  or wants to annotate and fix visual issues in their web app.
tools: Read, Glob, Grep, Bash, Edit, Write, Agent, AskUserQuestion
---

# Redline — Visual UI Annotation & Fix

Annotate elements in a running web app, then process those annotations to fix the code.

## Modes

### `/redline setup`

Guide the user to install the Redline annotation overlay for their environment.

#### Step 1: Detect environment

Check the project type:

- If the project has a `src-tauri/` directory → **Tauri app** → recommend the Tauri plugin
- Otherwise → **Web app in browser** → recommend the Chrome extension

Ask the user to confirm if unclear.

#### Step 2a: Chrome Extension (for web apps in Chrome)

Tell the user:

1. Install from the [Chrome Web Store](https://chromewebstore.google.com/detail/egdjiaglidnecgjbddcbdboffbhdlgdn), or clone from `https://github.com/twiced-technology-gmbh/redline-plugin-chrome` and load unpacked
2. Pin the extension icon for quick access
3. Press `Cmd+Option+Shift+A` (Mac) / `Ctrl+Alt+Shift+A` (Win/Linux) to toggle

#### Step 2b: Tauri Plugin (for Tauri desktop apps)

Guide the user through these changes:

1. **Add dependency** to `src-tauri/Cargo.toml`:
   ```toml
   tauri-plugin-redline = "0.1"
   ```
   Or for a local path (e.g., monorepo):
   ```toml
   tauri-plugin-redline = { path = "../path/to/tauri-plugin-redline" }
   ```

2. **Register the plugin** in `src-tauri/src/lib.rs` — must be on the Builder (before `.setup()`), not inside `setup()`, because the plugin uses `js_init_script`:
   ```rust
   let mut builder = tauri::Builder::default()
       .plugin(tauri_plugin_shell::init());

   if cfg!(debug_assertions) {
       builder = builder.plugin(tauri_plugin_redline::init());
   }

   builder.setup(|app| { /* ... */ })
   ```

3. **Add permission** to `src-tauri/capabilities/default.json`:
   ```json
   "redline:default"
   ```

4. Run `cargo build` to verify compilation.

#### Step 3: Create redline directory

```bash
mkdir -p .claude/redline
```

#### Step 4: Update .gitignore

Add this entry to `.gitignore` if not already present:
```
.claude/redline/
```

#### Step 5: Confirm to the user

Tell the user:
- Setup is complete
- How to use: press `Cmd+Option+Shift+A` (Mac) or `Ctrl+Alt+Shift+A` (Win/Linux) while the app is running
- They'll name the annotation session, click elements and type feedback, then press the hotkey again to finish
- The annotation file downloads automatically — the filename gets copied to clipboard
- Paste it to any coding agent with `/redline <filename>`

---

### `/redline <filename>`

Process an annotation file and fix the issues in the codebase.

#### Step 1: Read the annotation file

The argument is a filename (e.g. `home-2026-03-10-1430.json`). Search for it in this order:

1. `~/Downloads/<filename>`
2. If not found: `find ~ -maxdepth 2 -name "<filename>" -type f 2>/dev/null | head -1`
3. If still not found, try as an absolute or relative path

**Reading strategy**: The file contains large base64 screenshot strings at the end. Use Bash to extract the annotation data without screenshots first:

```bash
python3 -c "
import json,sys
d=json.load(sys.stdin)
ss={}
if 'screenshots' in d:
    ss['has_page']=bool(d['screenshots'].get('page'))
    ss['has_annotations']=bool(d['screenshots'].get('annotations'))
    d.pop('screenshots')
d.pop('screenshot',None)
d['_screenshot_availability']=ss
print(json.dumps(d, indent=2))
" < FILE_PATH
```

This gives you all annotation data with their `computedCss` included (needed for fixes) while keeping context manageable.

#### Step 2: Classify each annotation by confidence

Every annotation has a `type` field. The type determines how reliably the captured element data identifies the user's actual target:

**HIGH confidence — `select` type:**
The user clicked directly on a DOM element. The `selector`, `html`, `computedCss`, and `childHints` are **exact**. Proceed directly to source location (Step 3).

**MEDIUM confidence — `arrow`, `circle`, `box`, `text` types:**
The user drew a shape near an element. Element data comes from `document.elementFromPoint()` at the shape's center or endpoint. Usually correct, but may capture a parent wrapper instead of the intended child.

Check the `html` field:
- If it contains specific identifiers (`data-testid`, `id`, meaningful classes, text content) → treat as reliable
- If the `comment` refers to something specific (e.g., "change icon color") but the `html` is a generic wrapper (e.g., `<a class="card-link">`) → the target is likely a child element. Check `childHints` for the actual target (e.g., `<svg>` for an icon comment).

For **arrows**: The `to` point (arrow tip) determines the captured element. The user is pointing AT this element — focus on it, not the `from` point.

**LOW confidence — `freehand` type:**
The bounding box center of freehand strokes can land anywhere — often a parent container. If the `html` is a generic container (`<div>`, `<main>`, `<section>`, `<article>`) with 4+ childHints, the captured element is almost certainly NOT the actual target.

**→ For LOW confidence annotations, you MUST use the screenshot for disambiguation (Step 2b).**
**→ For MEDIUM confidence annotations where the comment doesn't match the captured element, also use the screenshot.**

#### Step 2b: Screenshot disambiguation (for LOW/ambiguous annotations)

The JSON includes a `screenshots` object with two base64 images:
- `screenshots.page` — the app content with the overlay hidden (JPEG)
- `screenshots.annotations` — the drawn shapes on transparent background (PNG)

**Extract and read both screenshots** using the Read tool on the original JSON file, or extract them:

```bash
python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
ss=d.get('screenshots',{})
if ss.get('page'):
    with open('/tmp/redline-page.jpg','wb') as f: f.write(base64.b64decode(ss['page'].split(',')[1]))
    print('Page screenshot: /tmp/redline-page.jpg')
if ss.get('annotations'):
    with open('/tmp/redline-annotations.png','wb') as f: f.write(base64.b64decode(ss['annotations'].split(',')[1]))
    print('Annotations screenshot: /tmp/redline-annotations.png')
" < FILE_PATH
```

Then Read both image files. You are a multimodal model — you can see images. Use them to:

1. **Look at the annotations screenshot** — see exactly where the user drew arrows, circles, boxes, freehand strokes, and text labels. Each annotation has a numbered label with the comment text.
2. **Look at the page screenshot** — see the actual UI state at the time of annotation.
3. **Mentally composite** — determine which UI element each drawn shape targets.
4. **Cross-reference** with the annotation data: the visual identification tells you the ACTUAL target; the `nearSelector`/`html`/`childHints` tell you WHERE in the DOM that target is (or its parent is).

**Example**: An arrow with comment "Change icon color to yellow" has `nearSelector` pointing to `<a href="/skills">` (a card wrapper). The screenshot shows the arrow tip pointing at the SVG icon inside that card. The `childHints` confirm `<svg>` is the first child. → Target is the SVG icon component inside the Skills card, not the card wrapper itself.

**Example**: A freehand with `nearSelector` pointing to `#main-content` (the entire page body). This is useless alone. The screenshot shows freehand strokes circling a specific table row. → Target is that table row, identifiable by cross-referencing the position with the page layout.

#### Step 3: Locate the source component

For each annotation, find the source file. Strategy depends on confidence:

**For HIGH confidence (`select`) and confirmed MEDIUM/LOW annotations:**

1. **Extract identifiers from `html`** (fastest path):
   - Look for `data-testid`, `id`, `aria-label`, or other unique attributes
   - Grep for that attribute value — this directly locates the source component
   - If uniquely identified, done

2. **Trace the selector path** (when `html` has no unique identifiers):
   - Break the selector into segments (split on ` > `)
   - Walk top-down through the component tree matching segments to JSX
   - `:nth-child(N)` — count children in the parent's JSX
   - Portal containers → follow `createPortal` to the rendering component
   - The final segment is the annotated element

3. **For draw annotations where the target is a CHILD of the captured element**:
   - Use the captured element's selector to find the parent component
   - Then locate the specific child element matching the comment (e.g., find the `<svg>` icon inside the card component)
   - The `childHints` array shows direct children — match the one relevant to the comment

4. **Verify with secondary signals**:
   - `childHints`: confirm children match
   - `text`: confirm rendered text
   - `tagName`: confirm HTML tag
   - `position`/`from`/`to`: consistent with layout position

**NEVER do this:**
- Grep for a class name, find a match, and assume it's the target without verifying against `html` or `selector`
- Ignore contradictions between selector and grep results — the selector is right

Group confirmed annotations by source file for parallel fixes.

#### Step 4: Apply fixes

For annotations that map to different files, dispatch parallel agents — one per file. Each agent receives the full annotation data and:

1. Reads the source file
2. Locates the element from Step 3
3. Interprets the comment in context:
   - **`comment`**: The user's feedback — this is the PRIMARY instruction
   - **`computedCss`**: Current styling — use to understand what needs to change (e.g., for "too much padding", read `padding-*` values)
   - **`html`**: Rendered outerHTML — shows attributes and structure
   - **`childHints`**: Direct children — helps navigate inner structure
4. Applies the minimal fix

**`computedCss` reference** — curated subset of ~40 properties:
- **Layout**: `display`, `position`, `width`, `height`, `min-*`, `max-*`, `padding-*`, `margin-*`, `gap`, `flex-*`, `align-*`, `justify-content`, `grid-*`, `top`/`right`/`bottom`/`left`, `z-index`, `overflow`
- **Visual**: `color`, `background-color`, `background`, `border`, `border-radius`, `box-shadow`, `opacity`
- **Typography**: `font-size`, `font-weight`, `font-family`, `line-height`, `text-align`, `text-decoration`, `letter-spacing`, `white-space`
- **Transform**: `transform`, `transition`

Default/empty values (`none`, `normal`, `auto`, `0px`, transparent) are omitted.

**For ambiguous comments** (e.g., "fix this", "wrong", "ugly"): flag in the summary rather than guessing. Include the selector, current styles, and what the screenshot shows so the user can clarify.

#### Step 5: Summary

```
Redline: processed <N> annotations from <view>

Fixed:
  - div.card > h2.title: reduced padding from 24px to 12px (src/components/Card.tsx:15)
  - button.submit: changed color from #333 to var(--primary) (src/styles/buttons.css:42)

Skipped (ambiguous):
  - nav > a.active: comment was "fix this" — please clarify what needs to change

Skipped (low confidence, no screenshot):
  - #main-content: freehand annotation "adjust spacing" — nearSelector hit the page container, could not determine target without screenshot

Files modified:
  - src/components/Card.tsx
  - src/styles/buttons.css
```
