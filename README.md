<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/bendwright-lockup-dark.svg">
    <img src="docs/img/bendwright-lockup-light.svg" width="320" alt="bendwright - The visual JSON editor nobody asked for.">
  </picture>
</p>

bendwright is a local, offline editor for [Archify](https://github.com/tt-a1i/archify)
workflow and architecture diagram JSON. You bring a diagram JSON that already renders in
Archify, and bendwright lets you move nodes, rewire connections, and fix labels directly, then hands
the JSON back for Archify to render. The JSON stays the source of truth. Extras that
Archify's format has no field for (custom colors, custom types, your own icons) live in
a small `<name>.bendwright.json` file next to your diagram, so the diagram JSON always
stays valid Archify.

![bendwright editing the sample workflow: a node selected, its fields in the inspector on the right](docs/img/screenshot-workflow.png)

**Bring your own Archify JSON.** This edits Archify's workflow and architecture IR, not
arbitrary JSON. Point it at a `.workflow.json` or `.architecture.json` that already works
with Archify, make your edits, and save.

## Why this exists

I kept asking an AI to make small changes to an Archify diagram. Move one node, reroute
one arrow, fix a label. Every round it regenerated the whole thing and handed back a
diagram worse than the one I started with. bendwright is the boring fix: open the JSON,
change exactly what you meant to change, and nothing else.

It is a single Python file with no third-party dependencies. It runs a small loopback
web server on `127.0.0.1` and opens the editor in your browser. Nothing leaves your
machine.

## Features

- **Workflow and architecture diagrams** - open either kind. The canvas is the live
  Archify render. Click anything and its fields open in the inspector on the right;
  with nothing selected, the inspector shows the diagram itself and **Browse** links to
  every node, edge, lane (or component, connection, boundary), card, and custom type.
- **Visual layout editing** - drag nodes; in a workflow they snap to the nearest lane and
  column, in an architecture diagram they snap to a 10 px grid.
- **Resize** - in an architecture diagram, select a component and drag the corner grip.
- **Nodes** - add, duplicate, and delete nodes. The inspector edits type, label,
  sublabel, tag, position (lane and column, or position and size), color, and icon.
  **Enter** applies your edits as one undo step; **Esc** throws them away.
- **New diagram** - start from scratch with **New**. The first **Save** asks where to
  write it.
- **Diagram title** - with nothing selected, edit the title and subtitle in the inspector.
- **Colors and line styles** - preset colors (blue, green, red, amber, purple, teal,
  gray) for nodes and edges, plus solid, dashed, or dotted lines. Readable in both the
  light and dark themes, and they keep working with Archify's data-flow animation.
- **Custom types** - define your own node types (for example "VM" or "File share") with
  a name, color, and icon in the **Custom types library** (the **⋯** menu). The name shows everywhere,
  including the exported page. Save a type to your library to reuse it in every diagram.
  Or pick **+ New custom type...** at the bottom of any Type list to make one on the
  spot and apply it to that node.
- **Icons** - a searchable picker (by name, alias, or category) with pictures of
  Archify's built-in logo catalog, a few extras bendwright ships for common
  infrastructure (Windows, Linux, Ubuntu, nginx, Apache, AWS, Azure, VM, web server,
  database), and your own PNGs. The catalog is read from your Archify install, so
  logos Archify adds later show up automatically.
- **Connections** - press **C** for Connect, click a source node, then a target; drag an
  endpoint to reroute. Press **V** to go back to Select. Click an edge to edit its label,
  line style, color, and Archify settings, or delete it.
- **Inline labels** - double-click a node or edge to jump straight to its label, or a
  lane header to rename the lane.
- **Quality profiles** - toggle between `standard` and `showcase`. Changes Archify
  rejects are reverted on the spot and the reason stays in a red chip in the header
  until you close it, so the file on disk is always renderable.
- **Explicit save** - edits live in a buffer and never touch disk until you press
  **Save**. Undo/redo, **Discard changes** (reload from disk, in the **⋯** menu), and an
  unsaved-changes warning are all included.
- **Lossless save** - key order, unknown fields, and the trailing newline are preserved.
  Files are written with 2-space indentation.
- **Cards** - the diagram's cards show under the drawing while you edit. **Cards** on
  the bottom tool bar hides them when they get in the way.
- **Preview** - **Preview** on the bottom tool bar opens the full rendered page in a new tab.
- **Export HTML** - from the **⋯** menu, one click saves the JSON and writes the rendered `.html` (via Archify) right next to it, always in sync. No CLI needed.
- **Native file picker** - open a diagram through your OS file dialog.
- **Auto-shutdown** - close the browser and the server (and its console window)
  shut down on their own a few seconds later.

## Setup

1. **Install the prerequisites.** Python 3.8 or newer, and (for validation and live
   preview) Node.js 18 or newer. bendwright itself has no third-party Python
   dependencies.

2. **Get Archify (the renderer).** The simplest setup for bendwright is to clone it
   next to bendwright:

   ```
   git clone https://github.com/tt-a1i/archify
   ```

   Archify also documents `npx skills add tt-a1i/archify -g`; if you install it that
   way, note the path to `archify.mjs` for step 4.

3. **Get bendwright.**

   ```
   git clone https://github.com/pcguywilson/bendwright
   ```

   Keep the two folders side by side, for example `code/archify` and `code/bendwright`.

4. **Point bendwright at Archify.** Any one of these:

   - Nothing to do if the `archify` folder sits next to the `bendwright` folder.
     bendwright finds `archify/bin/archify.mjs` on its own.
   - Set `ARCHIFY_HOME` to the Archify folder (the one whose `bin/` holds
     `archify.mjs`), or to the full path of `archify.mjs`.
   - Pass `--archify path/to/archify/bin/archify.mjs` when you launch.

Without Archify, bendwright still runs and saves with a structural check, but you
lose live preview and full validation.

## Running it

**Windows:** double-click `bendwright.bat`. Your browser opens; click **Open** to
pick a diagram. You can also drag a `*.workflow.json` or `*.architecture.json` file onto
the `.bat` to open it directly.

**Any platform:**

```
python bendwright.py                         # starts a new blank diagram
python bendwright.py path/to/diagram.workflow.json
python bendwright.py diagram.workflow.json --port 8770 --archify /path/to/archify.mjs
```

Two sample diagrams, `bendwright.example.workflow.json` and
`bendwright.example.architecture.json`, are included, so you can launch and click Open to
try them right away.

bendwright shuts its own server down a few seconds after you close the browser. If you
want it to stay running while you step away for a long time, launch with `--keep-alive`.

To get the rendered diagram, choose **Export HTML** from the **⋯** menu. bendwright saves
your JSON and writes `<name>.html` next to it, ready to open or share.

## Custom types, icons, and the Archify patch

**Where things live**

- `<name>.bendwright.json`, next to your diagram: this diagram's colors, custom types,
  and any of your own icons it uses. Written only when you **Save**. Keep it with the
  diagram if you move or share the JSON.
- `bendwright-data/types.json`, next to `bendwright.py`: your type library, shared
  across diagrams.
- `bendwright-data/icons/`, next to `bendwright.py`: your own icons. Drop a file in and
  choose **Refresh icons** from the **⋯** menu.
- `bendwright-data/` also holds the Archify patch record. It is git-ignored, so your
  icons and types never end up in a commit. To keep it elsewhere, set
  `BENDWRIGHT_DATA_DIR` to an absolute path.

**Your own icons**

- PNG only, 64 KB or smaller. 64 to 128 px square with a transparent background works
  best; the icon is drawn at about 16 px in the node's corner.
- The file name is the icon name: lowercase letters, numbers, `-`, or `_`
  (for example `vm-server.png`). Other names are skipped with a note.
- A name already used by Archify's catalog or bendwright's extras is skipped. Pick a
  different name.
- Icons a diagram uses are copied into its `.bendwright.json`, so the diagram still
  renders on another machine. Exported HTML always has the icons built in.

**The Archify patch**

So that a custom type's name (say "VM") also appears in Archify's click panel, search,
and in-drawing legend, bendwright applies a small patch to the Archify install it uses.
It changes four files (`workflow-compiler.mjs`, `render-architecture.mjs`, `cli.mjs`,
`template.html`) and keeps a backup of each (`*.bendwright-orig`).

- The patch does nothing unless bendwright is the one rendering. Running Archify on its
  own gives its normal output.
- bendwright checks the patch at every start and re-applies it after an Archify
  update. If an update changed Archify too much, it restores the originals, shows a
  note, and falls back to showing custom names in tooltips and the page legend only.
  Exports keep working either way.
- Turn it off: set `ARCHIFY_BENDWRIGHT_PATCH=0`.
- Undo it: `python bendwright.py --unpatch-archify`.

Exported HTML is always a single self-contained file: colors, icons, and names are
baked in, and it opens anywhere without bendwright or Archify.

## The IR format

A workflow diagram is a single JSON object: `lanes`, `nodes`, `edges`, and optional
`meta`, `cards`, `mainPath`, and `semanticChecks`. An architecture diagram has
`components` (with `pos` and `size`), `connections`, and optional `boundaries`, `cards`,
and `meta`. See `bendwright.example.workflow.json` and
`bendwright.example.architecture.json` for complete, valid examples, and the Archify
schemas for the full contract.

## Demos

All clips use the two sample diagrams that ship with bendwright.

**Inspect and edit** - click a node, change its label and sublabel in the inspector, and
press **Enter**. Archify re-renders right away; nothing touches disk until **Save**.

![Inspect and edit](gifs/inspect-and-edit.gif)

**Move a node** - drag it; it snaps to the nearest lane and column. **Undo** puts it back.

![Move a node](gifs/move-node.gif)

**Connect and reroute** - press **C**, click a source, then a target. Drag an endpoint
onto another node to reroute the connection.

![Connect and reroute](gifs/connect-and-reroute.gif)

**Style an edge** - click a connection, pick a line style and color, and rename it. Line
and color are saved beside the diagram, so the Archify JSON stays valid.

![Style an edge](gifs/style-edges.gif)

**Icons and custom types** - search the icon picker in the inspector and pick a logo; it
shows on the node right away. Then pick **+ New custom type...** in the Type list to name
a type and apply it on the spot.

![Icons and custom types](gifs/icons-and-types.gif)

**Architecture diagrams** - set a component's position in the inspector (its boundary
grows to fit), then drag another component's corner grip to resize it.

![Architecture diagrams](gifs/architecture.gif)

![Architecture diagram with a connection selected: line style, color, and Archify settings in the inspector](docs/img/screenshot-architecture.png)

**Safe by default** - an edit Archify rejects is put back right away and the reason stays
in the header until you close it. Your file is never left unrenderable.

![Safe by default](gifs/safe-by-default.gif)

**Edge styles at a glance** - every preset color as a solid, dashed, and dotted line, in
the dark and light themes. Rendered by bendwright.

![Edge styles](gifs/edge-styles.gif)

Duplicate and delete live in the node inspector, **New** starts a blank workflow or
architecture diagram, and cards are edited from **Browse > Cards** (toggle them on the canvas with **Cards**).

## License

[MIT](LICENSE)
