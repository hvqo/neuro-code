# TUI appearance themes

Open `/settings` → **Appearance**. Move with arrow keys to preview; Enter or clicking a choice applies and saves it. Esc / Back restores the theme used when opening the picker. Tab traverses choices and Back; the list scrolls. Previewing does not write preferences or submit drafts.

The composer has no extra title; a newline hint and Send action sit below the editor. Enter and Send share the existing submission pipeline. Ctrl+J or F2 inserts a newline; alternatively click Newline, or focus that button with Tab and press Enter. Alt+Enter / Shift+Enter work only when the terminal forwards distinct key events; neither is universally supported. The composer fills the available terminal width with bounded multiline height. Graphite and Porcelain use restrained theme surfaces for the composer and historical user messages. System leaves broad areas on the terminal's default background and uses border glyphs, dim text, and selection semantics for hierarchy. The composer has a fine top rule that uses the accent when focused; user messages retain a left rule. Placeholder text, hints, cursor and selection colors improve visibility.

There are 13 choices. `porcelain` is warm white; `graphite` retains the existing Obsidian storage identifier; `matrix` is Neuro Code's own green-on-black palette. First launch still defaults to Porcelain, and existing preferences remain valid.

`system` passes through the terminal's default foreground/background and a small ANSI palette using Textual's `textual-ansi` rendering mode. It does not query OSC, infer RGB values, or read the operating system's light/dark setting. Broad surfaces use the default background; the theme never assumes `ansi_bright_black` is dark. Default foreground, dim text, border glyphs, reverse selection, and limited ANSI state colors provide hierarchy. Inline Markdown code has no background fill, while fenced code and diffs retain separate styling. Actual contrast still depends on the terminal palette. Textual's deterministic System snapshots do not represent every real terminal palette; use the manual checklist in `tests/visual/README.md`. First-run provider setup uses the same theme registry.

The themes below use upstream color values with independently designed mappings to Neuro Code's text, surfaces, focus, syntax and status roles. They do not port upstream layout or implementation. Some secondary text is brightened for small terminal text and readability; these are adaptations rather than pixel-identical reproductions.

| Identifier | Reference variant | Palette source |
| --- | --- | --- |
| `tokyonight` | Night | [tokyonight](https://github.com/folke/tokyonight.nvim) |
| `everforest` | Dark / medium | [everforest](https://github.com/sainnhe/everforest) |
| `ayu` | Dark | [ayu](https://github.com/ayu-theme/ayu-colors) |
| `catppuccin` | Mocha | [catppuccin](https://github.com/catppuccin/palette) |
| `catppuccin-macchiato` | Macchiato | [catppuccin-macchiato](https://github.com/catppuccin/palette) |
| `gruvbox` | Dark | [gruvbox](https://github.com/morhetz/gruvbox) |
| `kanagawa` | Wave | [kanagawa](https://github.com/rebelot/kanagawa.nvim) |
| `nord` | Nord | [nord](https://github.com/nordtheme/nord) |
| `one-dark` | Atom One Dark | [one-dark](https://github.com/Th3Whit3Wolf/one-nvim) |

Preferences use the `theme` field in the existing atomic JSON store. Missing or invalid values fall back to `porcelain`. A save failure keeps the applied appearance and reports an error. Theme switches refresh CSS, Rich Markdown, syntax and existing message widgets while preserving drafts, cursor position and conversation content.

Graphite and Porcelain use a neutral reading hierarchy: primary prose, quieter secondary and muted text, restrained canvas/panel/selected surfaces, and a visible normal border and stronger focus border. Their ordinary text and meaningful UI boundaries have contrast regression guards. One accent marks links and interaction; success, warning, and error are reserved for state. Markdown headings and Tool Activity use the conversation's neutral hierarchy. Code syntax retains its independent palette (including syntax colors), without changing message content. The other saved theme identifiers remain compatible; V1A's measured RGB guards cover Graphite and Porcelain. See [ADR 0184](adr/0184-tui-color-system-and-contrast-v1a.md).
