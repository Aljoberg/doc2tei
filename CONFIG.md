# Writing a doc2tei config

This is the reference for **config files** — the per-document Python modules that
tell `doc2tei` how to turn a source document into TEI XML. If you just want to run
the converter, jump to [Running it](#running-it). If you want to teach it about a
*new* document, read the whole thing.

> **Scope.** doc2tei converts parliamentary transcripts (Yugoslav/Slovene assembly
> sessions) into [TEI](https://tei-c.org/). There are two input backends — searchable
> **PDF** and **Word** (`.docx`) — and PDF is the actively maintained one. Word support
> is older and partially bit-rotted; see [Word mode](#word-mode-legacy).

---

## Table of contents

- [The big idea](#the-big-idea)
- [The pipeline](#the-pipeline)
- [Anatomy of a config module](#anatomy-of-a-config-module)
- [The chunk model](#the-chunk-model)
- [The `CONFIG` dict](#the-config-dict)
  - [Alignment groups](#alignment-groups)
  - [Rules](#rules)
  - [`run_immediate`](#run_immediate)
  - [Rule lifecycle](#rule-lifecycle)
- [Engine primitives](#engine-primitives)
  - [The stack](#the-stack)
  - [`push` / `pop` / `pop_to`](#push--pop--pop_to)
  - [`pop_and_push_to`](#pop_and_push_to)
  - [`tag_is_on_top` / `is_before_layout`](#tag_is_on_top--is_before_layout)
  - [`append`](#append)
  - [Spacing: `space_before`](#spacing)
- [Cosmetic annotations](#cosmetic-annotations)
- [Hooks: `on_pop` and `on_end`](#hooks-on_pop-and-on_end)
- [Writing `get_chunks` (the PDF extractor)](#writing-get_chunks-the-pdf-extractor)
- [Word mode (legacy)](#word-mode-legacy)
- [Finding the magic numbers](#finding-the-magic-numbers)
- [Worked example: the ZRIP config](#worked-example-the-zrip-config)
- [Gotchas](#gotchas)
- [Running it](#running-it)

---

## The big idea

A scanned parliamentary transcript has no machine-readable structure — it's just
text at positions on a page (PDF) or runs inside floating frames (DOCX). doc2tei
rebuilds the structure with two pieces working together:

1. **An extractor** (`get_chunks`) flattens the source into a linear stream of
   **chunks** — small pieces of text that each carry geometry (x, y), font info,
   and bold/italic flags.

2. **A rule engine** (`parse.py` + `engine.py`) feeds each chunk through *your*
   config. Your rules look at a chunk's position, font and text and decide what
   TEI element it belongs to — a speaker note, a paragraph segment, a footnote, a
   heading — by pushing and popping elements on a **stack** that mirrors the TEI
   tree being built.

Everything document-specific lives in the config. The engine is generic; the
config is where the "this PDF puts session headers at y≈607 and footnote markers
in 7pt type" knowledge goes. **Every new document needs its own config**, because
the coordinates, fonts and layout conventions differ.

Output is a single `<TEI>` tree:

```
TEI > text > body > div[type=debateSection] > ( head | time | note | u > seg | ... )
```

---

## The pipeline

`parse.py` is the entry point. For input file `INPUT`:

```
python parse.py INPUT -o out.xml
```

it does, in order:

1. `chunks = get_chunks(INPUT)` — calls **your config's** extractor, a generator
   of `Chunk`s.
2. Wires the config into the engine:
   - `engine.COSMETIC_ANNOTATIONS = COSMETIC_ANNOTATIONS`
   - `engine.on_pop = CONFIG.get("on_pop")`
3. For every chunk, calls `parse_text(chunk)` (with all stdout redirected into
   `meow.txt` — that file is the debug log).
4. `parse_text` chooses an [alignment group](#alignment-groups), then
   `match_rules` runs your rules against the chunk. The first matching rule fires;
   if none match, the chunk's text is appended into whatever element is currently
   open on the stack.
5. After the last chunk, the engine pops every still-open element back down to the
   root `<div>` and commits the tree.
6. If the config defines `on_end`, it's called.
7. The tree is serialized and written to `-o` (or printed).

```
INPUT ─▶ get_chunks() ─▶ Chunk stream ─▶ parse_text ─▶ match_rules ─▶ rule fires
                                                │                         │
                                                │                         ├─ action()        (mutate stack)
                                                │                         ├─ append_func()    (emit text)
                                                │                         └─ after_append()   (update state)
                                                │
                                                └─ no match ─▶ append(chunk) into open element
```

> **Required exports.** `parse.py` does
> `from config import CONFIG, COSMETIC_ANNOTATIONS, get_chunks, speaker_to_utterance`.
> So the module imported as `config` **must** define all four names — `CONFIG`,
> `COSMETIC_ANNOTATIONS`, `get_chunks`, and a symbol literally called
> `speaker_to_utterance` (even though `parse.py` reaches the actual hook through
> `CONFIG.get("on_pop")`). This is a hard import; if `speaker_to_utterance` is
> missing the program won't even start. See [Gotchas](#gotchas).

---

## Anatomy of a config module

The root `config.py` *is* the live config — currently the one for
*7. seja zbora republik in pokrajin* (ZRIP). Examples live under `examples/`:

| File | What it is |
| --- | --- |
| `config.py` (repo root) | The active PDF config (ZRIP). `parse.py` imports this. |
| `examples/zbor-republik-in-pokrajin/config_pdf.py` | Symlink to the canonical ZRIP PDF config. |
| `examples/zbor-republik-in-pokrajin/config_word.py` | The older **Word** version of the same document. |
| `examples/prosvetno-kulturno-vece/config.py` | A second PDF config, for a different transcript. Good "minimal" reference. |

A config module exports:

```python
CONFIG: PDFConfig                       # the rule tree (required)
COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations   # inline formatting rules (required)
def get_chunks(filename) -> Generator[Chunk]   # the extractor (required)
def speaker_to_utterance(popped): ...   # the on_pop hook (name required by parse.py)
# ...plus any helper functions your tests/actions use
```

The type definitions for all of this are in `type_decs.py`
(`PDFConfig`, `PDFRule`, `PDFCosmeticAnnotation`, and the Word equivalents).

---

## The chunk model

Defined in `engine.py`. Your `test`/`action`/`append_func` callables receive a
chunk and read its attributes to decide what to do. The three concrete types:

### `PDFChunk` — one run of same-font text on one line

| Attribute | Type | Meaning |
| --- | --- | --- |
| `x`, `y` | `float` | Position in **pdfplumber coordinates** (points; x from left, y **from the bottom** of the page). |
| `text` | `str` | The run's text. |
| `bold` | `bool` | `True` if `"bold"` appears in the font name. |
| `italic` | `bool` | `True` if `"italic"` appears in the font name. |
| `font_size` | `float` | Point size. Footnote markers are 7.0; body ≈ 9–10. |
| `page_num` | `int` | 0-based page index. |
| `space_before` | `bool` | `True` if whitespace separates this run from the previous one. Set by `get_chunks`; drives [spacing](#spacing). |
| `previous` | `PDFChunk \| None` | The chunk emitted just before this one (a linked list across the whole document). |
| `line_chunk` | `PDFLineChunk` | Back-reference to the line this run belongs to. |

### `PDFLineChunk` — a whole line, holding its runs

| Attribute | Meaning |
| --- | --- |
| `x`, `y` | Position of the line's first run. |
| `text` | Concatenated text of the whole line. |
| `bold`, `italic` | `True` only if **all** runs are bold/italic. |
| `runs` | `list[PDFChunk]` — the child runs, in order. |

You reach the line from a run via `chunk.line_chunk`. This is how you ask
questions like "is this the first run on its line?"
(`chunk is chunk.line_chunk.runs[0]`) or "is the whole line italic?"
(`chunk.line_chunk.italic`).

### `WordChunk` — one docx run

| Attribute | Meaning |
| --- | --- |
| `x`, `y`, `w`, `h` | The enclosing text frame's geometry (twips, from `w:framePr`). |
| `text`, `bold`, `italic` | Run text and formatting. |
| `space_before` | `bool` (default `True`) — whitespace separates this run from the previous one. Word runs are already whole, so the default suits them. |
| `run` | The underlying `python-docx` `Run` (e.g. `run.font.superscript`). |
| `paragraph` | The `Paragraph` (e.g. `paragraph.alignment`, `paragraph.runs`). |

### `Chunk` (protocol)

The minimal interface — `x`, `y`, `text`, `bold`, `italic`, `space_before` — that
both concrete types satisfy. Helpers that work on either backend type against this.

> **Coordinate convention.** PDF `x`/`y` are pdfplumber coordinates: x grows
> rightward from the page's left edge, **y grows upward from the bottom**. So
> "near the top of the page" means a *large* y (≈ 700+ on these documents), and a
> footnote at the bottom has a small y. Use the tools in
> [Finding the magic numbers](#finding-the-magic-numbers) to measure them.

---

## The `CONFIG` dict

```python
CONFIG: PDFConfig = {
    "mode": "pdf",                  # "pdf" | "word"
    "on_pop": speaker_to_utterance, # optional hook, runs on every pop
    "on_end": on_end,               # optional hook, runs once at the very end
    "alignments": { ... },          # the rule tree (required)
}
```

`mode` is informational/typing; the real branch in `parse.py` is on the chunk
type (`WordChunk` vs `PDFChunk`).

### Alignment groups

`alignments` maps a **group name** to a group of rules. Which group a chunk is
tested against depends on the backend:

- **PDF** — always the group named **`"any"`**. (PDF has no usable paragraph
  alignment, so there's just one bucket.)
- **Word** — `parse.py` computes the run frame's horizontal center and picks a
  group by magic ranges (twips):
  - center `4550–6560` → `"center"`
  - center `2500–3660` → `"left"`
  - center `7820–8460` → `"right"`
  - otherwise → `"_else"`

  Only the groups you define are consulted, so the ZRIP Word config uses just
  `"center"` and `"_else"`.

### Rules

A group is an **ordered dict** of `name -> rule`. Order matters: rules are tried
top to bottom and **the first match wins**. A rule is a `TypedDict`:

```python
"SPEAKER": {
    "test": lambda chunk: (0 < chunk.x < 55 or 271 < chunk.x < 293)
                          and leading_caps(chunk.text) >= 5,
    "action": pop_and_push_to("div", tag="note", type="speaker"),
    # "append_func": ...,   # optional
    # "after_append": ...,  # optional
    # "alignment": ...,     # Word only, optional
}
```

| Key | Required | Signature | Purpose |
| --- | --- | --- | --- |
| `test` | yes | `(chunk) -> bool`, **or** the literal `"_else"` | Decides if the rule fires. `"_else"` makes this the group's fallback (runs only when nothing else in the group matched). |
| `action` | no | `(chunk) -> Any` | Mutates the stack: pops/pushes the TEI elements this chunk should live in. |
| `append_func` | no | `(chunk) -> Any` | Emits the chunk's content. **If omitted, the engine calls `append(chunk)`** for you. Override it to suppress/transform the text or to control which cosmetic annotations apply. |
| `after_append` | no | `() -> Any` | Side-effect after appending (e.g. flipping a state flag). |
| `alignment` | no (Word) | `WD_PARAGRAPH_ALIGNMENT` | Word-only extra gate: the rule is skipped unless `chunk.paragraph.alignment` equals this. |

### `run_immediate`

A group may contain a key `"run_immediate"` whose value is a **plain callable**
(not a rule dict). It runs once for every chunk that lands in the group, *before*
any rule is tested. Use it to reset per-chunk state — for example the Word config
clears its `visited_time` latch when a paragraph stops being centered:

```python
"_else": {
    "run_immediate": lambda: globals().update({"visited_time": False}),
    "REFERENCE_ENTRY": { ... },
    ...
}
```

`match_rules` distinguishes rules from `run_immediate` by "is it callable?" — rule
values are dicts, `run_immediate` is a function.

> The PDF configs no longer need a `run_immediate` for spacing. Spacing is now
> driven per-chunk by [`space_before`](#spacing), so the old
> `setattr(engine, "is_first_run", True)` poke is gone.

### Rule lifecycle

When a rule fires (`do_rule_chores` in `parse.py`):

1. `action(chunk)` runs if present — this is where stack surgery happens.
2. `append_func(chunk)` runs if present; **otherwise** `append(chunk)` runs.
3. `after_append()` runs if present.

If **no** rule matched (and there was no `"_else"`), `parse_text` falls back to
`append(chunk)`. That's the default path: text that isn't the start of anything new
simply flows into the element currently open on the stack. This is how continuation
lines and trailing runs of a paragraph accrete into the `<seg>` that an earlier
chunk opened.

> Empty chunks (`text` is empty or `"\n"`) are skipped by `match_rules` and never
> reach a rule.

---

## Engine primitives

These are the verbs your `action` and `append_func` callables use. Import them
from `engine`:

```python
from engine import (
    make_chunk, pop_and_push_to, tag, tag_is_on_top,
    pop_to, push, append,
)
```

### The stack

The engine holds a `stack: list[StackEntry]`. The bottom entry is the permanent
root `<div type="debateSection">`. Each `StackEntry` wraps:

- `element` — the `ET.Element` being built,
- `children` — a *pending* list of `str | Element` not yet flushed into the
  element,
- `last_elem` — bookkeeping for where trailing text should attach,
- `cosmetic` — whether this is an inline cosmetic wrapper (see below).

A global `children` always points at the top entry's pending list. Text and child
elements accumulate there and are "committed" into real `.text`/`.tail` when the
entry is popped. **You almost never touch `StackEntry` directly** — you drive the
stack through the helpers.

### `push` / `pop` / `pop_to`

```python
push("note", type="speaker")                       # open <note type="speaker">
push("note", attribs={"xml:id": "#note3"}, place="foot", n="3")  # mixed attrs
push("hi", rend="italic")                          # open <hi rend="italic">
push(some_element)                                 # push a prebuilt ET.Element
push("emph", cosmetic=True)                        # open an inline cosmetic wrapper
```

- `push(tag, *, cosmetic=False, attribs={}, **rest)` opens a new element on top of
  the stack. Attributes come from both the `attribs` dict and loose keyword args
  (`type=...`, `n=...`, `place=...`). `cosmetic=True` marks it inline (see
  [Cosmetic annotations](#cosmetic-annotations)).
- `pop()` closes the top element, flushes its pending children into it, attaches
  it to its parent, and fires the `on_pop` hook. You rarely call this directly.
- `pop_to(*tags, invert=False)` pops until the top **structural** element's tag is
  one of `tags` (with `invert=True`, pops *while* it is). This is how you climb
  back out to a known anchor before opening something new:

  ```python
  pop_to("u", "div")   # climb out to the nearest <u> or <div>
  ```

  `tags` may be strings or `ET.Element`s (only the tag name is compared).

### `pop_and_push_to`

The most common action. It's a factory that returns an `action` closure:

```python
"action": pop_and_push_to("div", tag="head", type="session")
# returns: def action(chunk): if not on_top("head", type="session"):
#                                  pop_to("div"); push("head", type="session")
```

`pop_and_push_to(*pop_args, tag, chunked=True, **attribs)`:

- pops to `pop_args`, then pushes `tag` with `attribs`;
- if `chunked=True` (default) it **skips** the pop+push when `tag` (with those
  attribs) is *already* on top — so consecutive chunks that belong to the same
  element don't keep reopening it. This matters a lot for PDF, where one logical
  block is split across many run-chunks: you don't want ten `<note>`s, you want
  one that the later chunks append into.
- `chunked=False` forces the pop+push every time. The `SEG` rule uses this:

  ```python
  "action": pop_and_push_to("u", "div", tag="seg", chunked=False)
  ```

  Each indented line is its own paragraph, so a new `<seg>` should start every
  time — pop the previous `<seg>` (climbing to `<u>`/`<div>`) and open a fresh one.

### `tag_is_on_top` / `is_before_layout`

```python
tag_is_on_top("note", type="speaker")   # is the top *structural* element <note type="speaker">?
tag_is_on_top("time")                    # ...is it <time>?
```

`tag_is_on_top(tag, **attribs)` answers "what kind of block am I currently inside?"
It **ignores cosmetic wrappers** — it looks past `<emph>`/`<hi>` to the first
structural element — and checks both tag and the given attributes. This is the
backbone of stateful rules: e.g. CHAIRMAN fires if we're already in `<time>` (the
chairman line always follows the time) or already in a `<note type="chairman">`.

`is_before_layout(element)` is the cosmetic counterpart: it returns `True` if a
cosmetic wrapper matching `element` is currently open (i.e. sits above the last
structural element). The cosmetic machinery uses it to decide whether an
`<emph>`/`<hi>` is already open. You normally won't call it directly.

`tag(name, **attribs)` is a tiny helper that builds a bare `ET.Element` — used to
declare the identity of a cosmetic annotation (see below).

### `append`

```python
append(chunk)                                  # emit text, auto-apply all cosmetics
append(chunk, should_annotate=["REFERENCE"])   # only apply the REFERENCE annotation
append(chunk, should_annotate=[])              # plain text, no cosmetics
```

`append(*chunks, should_annotate=True)` is what actually puts text into the open
element. For each chunk it:

1. trims the text to one edge-stripped token; whitespace-only runs are dropped;
2. runs the [cosmetic annotations](#cosmetic-annotations) selected by
   `should_annotate` (`True` = all of them, or a list of names; an unknown name
   raises);
3. inserts a single separating space *before* the token when
   [`chunk.space_before`](#spacing) is true (and there's something to separate it
   from — see below);
4. appends the token.

`should_annotate` is how you stop double-formatting: a `GENERIC_NOTE` already wraps
its body in `<hi rend="italic">`, so it appends with `should_annotate=["REFERENCE"]`
(ZRIP) or `[]` (prosvetno) to keep the italic cosmetic from firing again inside it.

### Spacing

A searchable PDF has no real spaces or line breaks — the extractor reconstructs
them. Rather than baking reconstructed spaces into the text (and then fighting to
strip them back out), each chunk carries one boolean:

- **`chunk.space_before`** — `True` when this run is separated from the previous one
  by whitespace (a glyph gap, a real space glyph, or a line break). The extractor
  sets it (see [Writing `get_chunks`](#writing-get_chunks)); the engine turns it into
  at most one separating space.

The engine's contract makes the whole thing predictable:

- A run's own text is trimmed to a single token, so **no element ever ends in a
  stray space**. (That's why `pop()` no longer has to rstrip or shuffle spaces.)
- The separator is emitted **between** tokens only. At a **block start** (the
  enclosing element is still empty) it's withheld, so a fresh `<seg>`/`<note>` never
  begins with a leading space — this is what replaced the old `is_first_run` dance,
  and it's why a footnote body doesn't need an `lstrip` after the marker (it lands in
  an empty `<note>`).
- When a run opens an inline element (`<emph>`, `<hi>`, `<ref>`), the separator is
  placed **outside** it, in the nearest enclosing element that already has content —
  so you get `beseda <emph>poudarjeno</emph> naprej`, never `beseda<emph> ...`.

The upshot for config authors: you almost never touch spacing in the engine. If
words run together or gain stray spaces, fix where `get_chunks` decides
`space_before`, not the rules. The behaviour is pinned by `tests/test_spacing.py`.

---

## Cosmetic annotations

Cosmetic annotations are **inline formatting that can appear inside anything and
doesn't change layout** — bold, italic, footnote references. They're handled
separately from rules so you don't have to think about them in every rule: any
time you `append` text, the engine opens/closes the right inline wrapper around it.

`COSMETIC_ANNOTATIONS` is an ordered dict of `name -> annotation`:

```python
COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {
    "ITALIC": {
        "test": lambda chunk: chunk.italic,   # when does this formatting apply?
        "tag":  tag("emph"),                  # the element it wraps text in (also its identity)
    },
    "BOLD": {"test": lambda chunk: chunk.bold, "tag": tag("hi", rend="bold")},
    "REFERENCE": {
        "test": lambda chunk: (chunk.run.font.superscript
                               if isinstance(chunk, WordChunk)
                               else chunk.font_size == 7.0),
        "tag": tag("ref"),                    # identity (for matching/removal)
        "append_func": lambda chunk: push(    # custom open: target is dynamic
            "ref",
            target=f'#note{re.sub(r"[^a-zA-Z0-9]", "", chunk.text.strip())}',
        ),
    },
}
```

Each annotation has:

| Key | Purpose |
| --- | --- |
| `test` | `(chunk) -> bool` — does this chunk carry the formatting? |
| `tag` | A bare `ET.Element` (build it with `tag(...)`) used as the wrapper's **identity** — the engine matches against it to know whether the wrapper is already open and to close it. |
| `append_func` | *Optional.* Custom "open" logic when the simple `push(tag)` isn't enough (e.g. `REFERENCE` computes a `target` attribute from the chunk text). |

**How `append` applies them** (for `PDFChunk`): for each annotation whose name is
in `should_annotate`,

- if its wrapper is **not** already open and `test(chunk)` is true → open it
  (`push(tag, cosmetic=True)`, or call `append_func`);
- if its wrapper **is** open and `test(chunk)` is false → close it
  (`pop_to(tag, invert=True)`).

So a run of italic chunks opens one `<emph>` on the first and the engine keeps it
open until a non-italic chunk closes it. `cosmetic=True` is what makes
`tag_is_on_top` and `pop_to` look *past* these wrappers to the real structural
element. Spacing across a wrapper boundary just works: because the separator is
emitted *outside* the element being opened (see [Spacing](#spacing)), `od <emph>…`
never fuses into `od<emph>…`, and no wrapper is left holding a trailing space.

> **`REFERENCE` is the odd one out.** Its `append_func` pushes a `<ref>` with a
> computed `target`, and the matching `REFERENCE_ENTRY` *rule* opens the
> corresponding `<note place="foot" n="...">`. Bold/italic are the clean,
> representative cases to model new annotations on.

---

## Hooks: `on_pop` and `on_end`

### `on_pop(popped: StackEntry)`

Set via `CONFIG["on_pop"]`; runs **every time any element is popped**, receiving the
`StackEntry` that just closed. The configs use it to build utterances from speaker
notes — a neat trick that works *because* a pop is the first moment the full text of
a speaker note is known:

```python
def speaker_to_utterance(popped):
    if popped.element.tag == "note" and popped.element.attrib.get("type") == "speaker":
        text = "".join((i.text or "") if isinstance(i, ET.Element) else i
                       for i in popped.children)
        # strip "PREDSEDNIK" prefix and anything after "(", "," or ":"
        name = re.sub(r"^pre\S+\s+|\s*(?:\(|,|:).*$", "", text, flags=re.IGNORECASE)
        serialized = "#" + "".join(w.capitalize() for w in name.split())  # #PascalCase
        utterance_speaker_mapping.setdefault(serialized, text)
        push("u", who=serialized)   # open the <u> right after the speaker note closes
```

The speaker note closes when the next block (e.g. a `<seg>`) opens; at that point
the hook fires and opens `<u who="#Name">` so the body lands inside an utterance.
`utterance_speaker_mapping` accumulates `{"#PascalName": "full speaker line"}`.

### `on_end()`

Set via `CONFIG["on_end"]`; runs once after the whole document is parsed and the
tree is committed. ZRIP uses it to dump the speaker map for the
[listPerson tool](#running-it):

```python
def on_end():
    with open("out/speaker_utterance.json", "w", encoding="utf-8") as f:
        json.dump(utterance_speaker_mapping, f, ensure_ascii=False)
```

---

## Writing `get_chunks` (the PDF extractor)

`get_chunks(filename)` is a generator that yields the `Chunk` stream. It's part of
the config because reconstructing words, runs and lines from raw glyphs is
document-specific. The shape is the same in both PDF configs; tune the thresholds
and grouping to your document.

Steps (see `get_chunks` in `config.py`):

1. **Collect glyphs.** A `pdfminer` `PDFLayoutAnalyzer` subclass walks each page and
   records every `LTChar` as `{text, x0, x1, y0, fontname, size}`.
2. **Group glyphs into lines.** Walk the page's glyphs; start a new line when the
   y-drop between consecutive glyphs exceeds `line_treshold` (≈ 4.8), with a larger
   guard (`× 5`) for big jumps (column breaks).
3. **Group a line into runs.** Within a line, merge consecutive glyphs into a run
   while the **font name and size match** and the x-gap is small. A gap *inside* a
   run (x-gap over `threshold` ≈ 1.7) becomes a real space in the run's text; a gap
   *between* runs sets the next run's [`space_before`](#spacing) instead. PDFs
   usually have no literal space glyphs, so this is how spacing is inferred from gaps.
4. **Carry the line break.** A `pending_space` flag remembers that a line ran to its
   end, so the **first run of the next line** gets `space_before=True` — that's what
   keeps lines word-separated without baking a trailing space onto every line.
5. **Build chunks.** For each run, `make_chunk(text=..., x=..., y=..., font_name=...,
   size=..., previous=prev_run, page_num=..., space_before=...)` → a `PDFChunk`,
   linked to the previous one. Then build one `PDFLineChunk` for the line, and set
   `run.line_chunk` on each run so rules can navigate line ↔ run.
6. **Yield.** `yield from run_chunks`.

```python
prev_run = make_chunk(text=r["text"], x=r["x0"], y=r["y0"],
                      font_name=r["fontname"], size=r["size"],
                      previous=prev_run, page_num=page_num,
                      space_before=r["space_before"])
```

The two configs differ in detail — `prosvetno`'s document already has space glyphs
(so it derives `space_before` from whitespace at run edges rather than from gaps),
skips a header band (`y > 740`), and handles even/odd-page indentation quirks —
which is exactly the kind of per-document tuning that belongs here.

> Magic numbers (`threshold`, `line_treshold`, header bands, indentation ranges)
> are the heart of a PDF config and almost always need re-measuring per document.

---

## Word mode (legacy)

`config_word.py` is the original `.docx` path, kept as an example. The differences
from PDF mode:

- The extractor is `get_frames` (not `get_chunks`): it walks `doc.paragraphs`,
  keeps only paragraphs with a `w:framePr` frame, and yields a `WordChunk` per run.
- Rules live in `"center"` / `"left"` / `"right"` / `"_else"` groups, chosen by the
  frame's center point, and may use the Word-only `"alignment"` gate.
- Tests read docx-native properties: `chunk.run.font.superscript`,
  `chunk.paragraph.alignment`, `chunk.paragraph.paragraph_format.first_line_indent`.

> **Status.** Word mode is older than the current engine and not actively kept in
> sync (e.g. the Word branch of `append`'s cosmetic handling has a stale open
> condition, and `parse.py`'s hard import of `speaker_to_utterance` is PDF-shaped).
> Treat `config_word.py` as a reference for the *rule style*, not as a drop-in
> working config. **PDF is the supported backend.**

---

## Finding the magic numbers

Configs are full of coordinates and font sizes. Two tools help you measure them so
you're not guessing:

- **`tools/pdf_coords.html`** — open it in a browser (no install; needs internet
  once to pull PDF.js), drop your PDF in, and move/click on the page. It reads out
  the cursor's **pdfplumber** coordinates (`x0/x1`, `y0/y1`, `top/bottom`) and the Δ
  between clicked markers. These are the exact `x`/`y` your `PDFChunk` tests see.
  Everything stays local.
- **`tools/dump_text_coords.py`** — `python tools/dump_text_coords.py file.pdf
  [--page N] [--grep WORD]` dumps, per text chunk, the transformation-matrix
  translations (`cm[4]/cm[5]`, `tm[4]/tm[5]`) and font size that pypdf reports, to
  cross-check positions.

Workflow: find an element you want to classify (a session header, a footnote
marker), measure its x/y/size, and write a `test` that brackets those values with a
little tolerance. Page misalignment in scans means you'll often need ranges
(`48 < x < 64`) rather than exact values, and sometimes per-page-parity branches.

---

## Worked example: the ZRIP config

Reading `config.py` (the `"any"` group) top to bottom — remember, **first match
wins**, so order encodes precedence:

| Rule | Fires when… | Produces |
| --- | --- | --- |
| `HEADER` | the chunk is one of the first 3 elements on a fresh page (running header) | nothing (`append_func` returns `None` — drops it) |
| `SEJA_DECLARATION` | bold, at the title band (`605 < y < 610`) or already inside the session head | `<head type="session">` |
| `TIME` | italic, at the time band or following a session section | `<time>` |
| `CHAIRMAN` | starts with `PREDSEDUJE`, or follows `<time>`/is already in chairman; not italic; indented | `<note type="chairman">` |
| `SEJA_SECTION` | all-caps, centered, first body line of a fresh page | `<head type="sessionSection">` |
| `REFERENCE_ENTRY` | 7pt, a digit, first run on its line (a footnote definition's marker) | `<note place="foot" n="…">` |
| `GENERIC_NOTE` | at the note band and the **whole line** is italic | `<note><hi rend="italic">…` |
| `SPEAKER` | not indented and ≥ 5 leading capital letters | `<note type="speaker">` (→ `on_pop` opens `<u>`) |
| `SEG` | `is_seg(chunk)` — first run of a correctly-indented body line | a fresh `<seg>` inside the current `<u>`/`<div>` |

Things worth copying from it:

- **Order as precedence.** `REFERENCE_ENTRY` (7pt digit) is listed before
  `GENERIC_NOTE`/`SEG` so a footnote marker is claimed before a body rule sees it.
- **State via the stack, not globals.** `CHAIRMAN` and `TIME` test
  `tag_is_on_top(...)` to chain off the previous block instead of tracking a flag.
- **`chunked` discipline.** `REFERENCE_ENTRY` guards with
  `tag_is_on_top("note", place="foot", n=serialized)` so the multi-run footnote body
  opens exactly one `<note>`; `SEG` uses `chunked=False` to restart every paragraph.
- **Geometry helpers.** `is_seg`, `is_page_top`, `nth_previous`, `leading_caps`
  keep the `test` lambdas readable — push non-trivial logic into named functions.

`examples/prosvetno-kulturno-vece/config.py` is a cleaner, smaller second example
(no footnotes, simpler cosmetics) and a good template to start a new config from.

---

## Gotchas

- **`parse.py` hard-imports `speaker_to_utterance`.** Any module used as the root
  `config.py` must define that name, or the program crashes on import — even if your
  `on_pop` is something else or absent. (Define a stub if you don't need it.)
- **Footnote/multi-run blocks split across chunks.** One logical block becomes many
  run-chunks in PDF. Always guard pushes with `tag_is_on_top(...)` (or use
  `chunked=True`, the default) so you open one element, not one per run. Conversely,
  use `chunked=False` when each chunk genuinely *is* a new block (paragraphs/`SEG`).
- **Coordinates are per-document and per-page.** Scans are misaligned; expect to use
  ranges, page-number branches (`left = 48 if page_num <= 2 else 41`), and
  even/odd-page parity (`prosvetno`'s `is_seg`). Re-measure with the tools above.
- **y grows upward.** "Top of page" = large y. Footnotes sit at small y.
- **Don't double-format.** When a rule already opened an inline wrapper
  (`GENERIC_NOTE` → `<hi rend="italic">`), append with `should_annotate=[...]` to
  stop the cosmetic layer re-wrapping the same text.
- **Spacing is reconstructed.** If words run together or get extra spaces, the fix is
  in `get_chunks` — specifically where it decides each run's
  [`space_before`](#spacing) — not in your rules or the engine.
- **`out/` and `meow.txt` are gitignored.** `meow.txt` is the full debug log (all
  prints land there); read it when a chunk goes to the wrong element.
- **`on_end` writes a hardcoded path** (`out/speaker_utterance.json`). Create `out/`
  (or change the path) before running.

---

## Running it

```bash
# 1. Convert a document to TEI (root config.py is the active config)
python parse.py path/to/input.pdf -o out/out.xml

# 2. (ZRIP) on_end wrote out/speaker_utterance.json; turn it into a TEI <listPerson>
#    by querying Wikidata for each speaker
python make_list_person.py out/speaker_utterance.json -o out/listPerson.xml
```

To convert a *different* document, write a new config module exporting `CONFIG`,
`COSMETIC_ANNOTATIONS`, `get_chunks`, and `speaker_to_utterance`
([anatomy](#anatomy-of-a-config-module)), and point `parse.py`'s `from config
import ...` at it (today that means making it the importable `config` — e.g. via
the `config.py` at the repo root, mirrored into `examples/.../config_pdf.py` by
symlink).

Dependencies: `pdfminer.six` (PDF extraction), `python-docx` (Word mode),
`pypdf` (the coord-dump tool), `requests` (`make_list_person.py`).
