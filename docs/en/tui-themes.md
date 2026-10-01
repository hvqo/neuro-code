# TUI appearance themes

Open `/settings` → **Appearance**. Move with arrow keys to preview; Enter or clicking a choice applies and saves it. Esc / Back restores the theme used when opening the picker. Tab traverses choices and Back; the list scrolls. Previewing does not write preferences or submit drafts.

The composer has no extra title; the editor and Send button share a row, with no permanent shortcut hint. Enter and Send share the existing submission pipeline. Ctrl+J / F2 insert a newline and are documented in F1 / `/help`. Shift+Enter works only when Textual receives a distinct modified event; SS3 keypad Enter is never guessed to be Shift. Bounded multiline height and all V1A palette / V1B typography semantics remain unchanged. System uses adaptive surfaces when terminal RGB is reliable; terminal input capability is independent of theme.

There are 13 choices. `porcelain` is warm white; `graphite` retains the existing Obsidian storage identifier; `matrix` is Neuro Code's own green-on-black palette. First launch still defaults to Porcelain, and existing preferences remain valid.

`system` probes OSC 10/11 once before Textual starts consuming terminal input, with a 60 ms deadline. A valid foreground/background pair with sufficient contrast enables derived surfaces and secondary text for TrueColor/ANSI256 output; ANSI16, unsupported terminals, malformed replies, and low-contrast pairs fail soft to default colors, ANSI-white panel rules, dim secondary text, reverse selection, and ANSI-blue focus. The probe is optional and failure never prevents startup. System never uses `ansi_bright_black` as a broad fill and does not infer the operating system's light/dark setting. Inline Markdown code has no background fill, while fenced code and diffs retain separate styling. Actual contrast still depends on the terminal palette. Deterministic snapshots inject fixed dark, light, or unknown palettes and do not represent every real terminal; use the manual checklist in `tests/visual/README.md`. First-run provider setup uses the same theme registry with the visible unknown-palette fallback.

The themes below use upstream color values with independently designed mappings to Neuro Code's text, surfaces, focus and status roles. They do not port upstream layout or implementation. Some secondary text is brightened for small terminal text and readability; these are adaptations rather than pixel-identical reproductions.

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

## Independent Syntax Theme

Open `/settings` → Appearance → **Syntax Theme**. Select a choice for live Python
preview; Save remembers it, Esc/Back restores the opening choice. This refreshes
existing and streaming fenced code without changing prose, inline code, drafts,
cursor, code surface or shell geometry. A save failure is visible and keeps the
applied choice.

Auto / Default uses GitHub Dark on dark RGB code surfaces and Friendly Light on
light surfaces. Six explicit choices are GitHub Dark, One Dark, Monokai, Dracula,
Friendly Light and Solarized Light. They reuse installed Pygments styles and
lexers; Python, Rust, JSON, Shell and Diff are covered, unknown languages render
ordinary code. Token foregrounds are adapted when required for ≥4.5:1 RGB
contrast; explicit dark styles can therefore be used with Porcelain and light
styles with Graphite. The UI remains the only code-background owner.

The independent optional `syntax_theme` field defaults to Auto for old or invalid
configurations and survives other preference writes. System uses its existing
terminal palette; reliable RGB enables the same adaptation, while unknown/ANSI16
uses default foreground and limited ANSI syntax colors. Your saved choice stays
intact. Actual ANSI contrast needs real-terminal review. See [ADR 0187](adr/0187-independent-tui-syntax-theme-v2a.md)
and the syntax gallery/manual checklist in `tests/visual/README.md`.
