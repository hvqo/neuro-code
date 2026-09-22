# TUI appearance themes

Open `/settings` → **Appearance**. Move with arrow keys to preview; Enter or clicking a choice applies and saves it. Esc / Back restores the theme used when opening the picker. Tab traverses choices and Back; the list scrolls. Previewing does not write preferences or submit drafts.

The composer has no extra title; a newline hint and Send action sit below the editor. Enter and Send share the existing submission pipeline. Ctrl+J or F2 inserts a newline; alternatively click Newline, or focus that button with Tab and press Enter. Alt+Enter / Shift+Enter work only when the terminal forwards distinct key events; neither is universally supported. The composer fills the available terminal width with bounded multiline height. Dedicated theme fills separate the composer and historical user messages from ordinary panels. The composer has a fine top rule that uses the accent when focused; user messages have an accent rule on the left. Placeholder text, hints, cursor and selection colors improve visibility. The system theme retains terminal defaults and uses foreground-colored rules to distinguish regions.

There are 13 choices. `porcelain` is warm white; `graphite` retains the existing Obsidian storage identifier; `matrix` is Neuro Code's own green-on-black palette. First launch still defaults to Porcelain, and existing preferences remain valid.

`system` passes through the terminal's default foreground/background and ANSI colors using Textual's `textual-ansi` rendering mode. It does not query OSC, infer RGB values, or read the operating system's light/dark setting. Appearance follows terminal configuration; reverse video and markers distinguish focused/selected rows. First-run provider setup uses the same theme registry.

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

Text uses independently mapped semantic colors per theme: blue headings and links, cyan inline code and tool activity, orange numbers and decorators, violet keywords, green strings and success states, and warm yellow warnings. Prose retains its neutral foreground. Light themes use darker text accents; dark themes use gentler bright colors. Matrix retains a green emphasis and System uses ANSI colors. Code, Markdown, and diffs share theme mappings without changing message content.
