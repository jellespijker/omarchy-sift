# ADR-0010: Translations, demo mode, and a theme-safe, overflow-proof panel

Status: Accepted

## Context
A design critique with real renders (31 stock themes, demo data, a throwaway render harness) found that the panel failed its users in ways
no code review would show:
- Secondary text used the theme's `muted` role, which fails 4.5:1 contrast in all 31 themes against the tinted surfaces it sits on (as low as
  1.19:1), and all small text was 10 px.
- The header status line ran under the scan and refresh buttons; the tab bar and the frequency selector were fixed-width rows that could not
  survive longer translations; chips and rows had no maximum width.
- The review row's action buttons moved with the number of tag chips; the same hint repeated on every row; tag ideas and classifier internals
  competed with the review queue; the demo banner outweighed the task.
The panel also had to be translated (Dutch, French, German, Spanish, Chinese, Arabic) and demonstrable without real data.

## Decision
**Structure.** `Model.qml` (data and CLI calls), `Content.qml` (the view) and `Panel.qml` (the bar host). The same view runs in the bar popup and in
`tools/render.py`, which renders any theme, language and tab to PNG in a throwaway window and logs the computed contrast per theme.

**Colour.** No text uses the theme's `muted`. Secondary text, accent text and urgent text are computed from the theme's own foreground until
they reach 4.5:1 on the tinted fill they sit on; accent is used for graphics (3:1) such as underlines and borders. Minimum text size is 11 px.
Measured in all 31 themes after the change: secondary text >= 4.50, primary text >= 4.79, urgent and accent text >= 4.51, accent graphics >= 3.14.

**Layout.** Tabs and the frequency selector are equal-width segmented controls that elide; chips never exceed the panel; review rows have a
fixed action column with a tag line that wraps; tag ideas moved to the Tags tab behind a one-line disclosure; settings are grouped
(Scanning, Classifier, General) with details collapsed; the four tabs share one scroll area so the popup stays a sensible height.

**Translations.** A flat JSON catalog per language in `sift/i18n/`, English as the fallback. The language follows the system (`LC_ALL`,
`LC_MESSAGES`, `LANGUAGE`, `LANG`) unless the user picks one (`sift config set language`, or Settings). The CLI and the panel share the catalogs:
the panel loads them through `sift i18n`, and the CLI translates the messages that surface in the panel (validation errors, safety refusals). Strings
are number-neutral ("Files: {n}") so no language needs plural forms. Arabic mirrors the whole layout (`LayoutMirroring`). Tag names and
descriptions were already any-script (ADR-0008). Tests enforce complete catalogs, identical placeholders, verbatim code spans, no invisible bidi
control characters, and short labels.

**Demo mode.** `sift demo on|off|reset`, a switch in Settings, and `SIFT_DEMO=1`. The real program runs against a sandbox (its own config, state and
synthetic files carrying real tags, plus an offline classifier), so every tab and every action works while nothing real is read or changed. Commands that
would change the system (setup, schedule, keyring) are refused while it is on.

## Consequences
- The panel is legible in every stock theme and survives about 40% longer text; both are checked by rendering, not assumed.
- English strings are the source of truth: changing one means updating five-plus translations (the catalog tests fail until they match).
- Translations were drafted by a language model and reviewed mechanically; native-speaker review is still recommended before release.
- Demo mode adds a second code path through paths and config, kept small by using the real commands on sandbox data.

## Alternatives rejected
- **Qt's `qsTr` and .ts files:** Quickshell does not load translators, and the CLI would still need its own catalog.
- **Using the theme's `muted` and hoping:** measured to fail in every theme.
- **Fixing overflow with smaller fonts:** worsens the legibility problem it was meant to solve.
