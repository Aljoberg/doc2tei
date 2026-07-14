# Writing a doc2tei config

Okay, so you want to parse a document. How doc2tei works is that you provide it a _configuration_ for your document, and it parses the document according to it.

The configuration is (if I simplify) a list of conditions that emit a specific tag.
For example, a `<head>` tag might need the source text to be bold, all caps, and centered.

doc2tei supports two backends, a **searchable PDF** file, or a **Word** (`.docx`) file as the input.

> Searchable PDF files are the main way of parsing documents, due to them usually being the original source.
> Word files are also _technically_ supported, but not actively maintained, since they require an extra step
> (they need to be converted from a searchable PDF file) and another OCR pass.

You can also bring your own format, since doc2tei uses a standardized _chunk_ system that is independent of PDFs or Word files.
You'll have to tweak some things though, like making your own chunk dataclass if there's additional attributes you might need.

## The idea

A searchable document has no machine-readable structure - it's just
text at positions on a page (PDF) or runs inside floating frames (DOCX).
doc2tei parses this in two parts:

1. **An extractor** (`get_chunks`) converts the source text to a stream of
   _chunks_ - small pieces of text that have certain attributes, such as their position, font information, and bold/italic info.

2. **A rule engine** (`parse.py` & `engine.py`) feeds each chunk through _your_
   config. Your rules check the attributes of a chunk, such as their x & y position, font and text and decide what
   to do with it - usually emit a TEI element by pushing and popping elements on a stack that mirrors the TEI
   tree being built.

Everything that involves the document lives in the config. It's what determines something like
"session headers in this PDF are between y 600 and 610, and footnotes' font size is 7."

Because PDF files differ **a lot** (in their coordinates, fonts and layout), rules usually need to be _very_ specific.
This means that **almost every new document needs its own config**, though similar documents can be parsed with the same config, assuming it's written well.

doc2tei's output is a `<TEI>` tree:

```
TEI > text > body > div[type=debateSection] > ( head | time | note | u > seg | ... )
```

---

<details>

<summary>If you're curious how the parser works</summary>

`parse.py` is the entry point. It takes an _input_ and prints the output to the _output_ file, or stdin. For input file `INPUT`:

```
python3 parse.py INPUT -o out.xml
```

it:

1. Gets the chunks by calling `config.get_chunks(INPUT)` so it can loop over them
2. Sets `COSMETIC_ANNOTATIONS` & `on_pop` on the engine
3. For every chunk, calls `parse_text(chunk)`
4. `parse_text` matches the general position of the chunk (only for Word files), then
   `match_rules` runs your rules against the chunk.
   If none match, the chunk's text is appended into whatever element is currently
   open on the stack.
5. After the last chunk, the engine pops every still-open element back down to the
   root `<div>` and commits the tree.
6. Calls `on_end(result)` if the config defines it (zero-argument legacy hooks
   are still supported).
7. The tree is stringified and written to the output file (or printed).

```
INPUT ─▶ get_chunks() ─▶ Chunk stream ─▶ parse_text ─▶ match_rules ─▶ rule fires
                                                │                         │
                                                │                         ├─ action()        (mutate stack)
                                                │                         ├─ append_func()    (emit text)
                                                │                         └─ after_append()   (update state)
                                                │
                                                └─ no match ─▶ append(chunk) into open element
```

</details>

## The config module

The config is defined in `config.py`. It exports (at least):

- `CONFIG` - a WordConfig or a PDFConfig, containing the rule tree
- `COSMETIC_ANNOTATIONS` - inline formatting rules
- `get_chunks(filename) -> Generator[Chunk]` - the extractor

All of these will be explained later.

The config path is not global anymore. Select it explicitly with
`--config path/to/config.py`, or pass it to `parse_document(..., config=...)`.
Old rule dictionaries and zero-argument hooks remain supported.

### Shared extractors, explicit policy

Extraction still belongs to the document config, because PDFs disagree about
spaces, line order, fonts and OCR. The shared classes remove the mechanical
pdfminer/pdfplumber loop without choosing those policies for you:

```python
from doc2tei import CharacterPDFExtractor, PageRange, WordPDFExtractor

# Character stream with real space glyphs.
get_chunks = CharacterPDFExtractor(
    pages=PageRange(0, None),
    line_tolerance=4.0,
    literal_spaces="preserve",  # or "break" / "ignore"
    line_filter=lambda line, page: line.y <= 740,
)

# OCR where reconstructing words and lines works better.
get_chunks = WordPDFExtractor(
    pages=PageRange(4, 114),       # zero-based, end-exclusive
    x_tolerance=1.7,
    y_tolerance=3.0,
    line_tolerance=3.2,
    word_gap=0.6,
    join_line_end_hyphens=True,
    page_enricher=enrich_page,
    line_enricher=enrich_line,
)
```

Both accept document callbacks for filtering, stopping, and metadata
enrichment. `PDFChunk.page_context.metadata`, `PDFChunk.metadata`, and
`PDFLineChunk.metadata` let rules consume those decisions without adding
one-off attributes to core chunk classes.

### Rules and selectors

Legacy ordered dictionaries are valid. For larger configs, named `Rule`
objects and composable selectors are available:

```python
from doc2tei import AllOf, Attribute, Between, LineStart, Metadata, Text, rule, rule_group

body_start = AllOf(
    LineStart(),
    Attribute("font_size", Between(8.8, 10.6, inclusive=True)),
    Metadata("front_matter", False, source="line"),
)

CONFIG = {
    "mode": "pdf",
    "debug": False,
    "alignments": {
        "any": rule_group(
            rule("SESSION", AllOf(LineStart(), Text(pattern=r"^\d+\. seja$", source="line")), action=open_session),
            rule("SEG", body_start, action=open_segment),
        )
    },
}
```

Rule order is still precedence. These helpers package common tests; they do
not introduce parliamentary concepts into the parser.

### Lifecycle and result hooks

`on_start(result)` runs after engine reset, `on_pop(entry)` runs whenever an
element closes, and `on_end(result)` runs after the tree is committed.
`result` contains `root`, `diagnostics`, and a free-form `data` dictionary.
Configs should put auxiliary output in `result.data`; the CLI writes it only
when `--data-output` is requested.

`SpeakerUtteranceHook` is an optional configurable debate helper. Installing
it is a config decision, not parser behavior:

```python
from doc2tei import SpeakerUtteranceHook

speakers = SpeakerUtteranceHook(identifier=my_speaker_id)
CONFIG.update(on_start=speakers.reset, on_pop=speakers, on_end=speakers.export)
```

The type definitions for all of this are in `type_decs.py`
(`PDFConfig`, `PDFRule`, `PDFCosmeticAnnotation`, and the Word equivalents).

---

## The chunk model

Every action / function in the config receives a _chunk_. It then does something with it (performs checks, pushes, pops, whatever).
There's three types:

### `PDFChunk` - some text with the same font on one line

| Attribute       | Type               | Meaning                                                                                                            |
| --------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `x` & `y`       | `float`            | Position of the chunk. With the default extractor, it's in **pdfplumber coordinates** (x from left, y from bottom) |
| `text`          | `str`              | The text. Bet you didn't expect that, huh                                                                          |
| `bold`          | `bool`             | `True` if `"bold"` appears in the font name                                                                        |
| `italic`        | `bool`             | `True` if `"italic"` appears in the font name                                                                      |
| `font_name`     | `str`              | Source font name                                                                                                   |
| `font_size`     | `float`            | Font size. Footnote markers are _usually_ 7.0; body is 9-10                                                        |
| `page_num`      | `int`              | 0-based page index                                                                                                 |
| `space_before`  | `bool`             | `True` if whitespace separates this run from the previous one. Set by `get_chunks`, see [spacing](#spacing)        |
| `previous`      | `PDFChunk \| None` | The chunk emitted just before this one (a linked list across the whole document)                                   |
| `line_chunk`    | `PDFLineChunk`     | Reference to the line this run belongs to                                                                          |
| `is_line_start` | `bool`             | Whether this is the first run in its line                                                                          |
| `line_text`     | `str`              | Shortcut for the complete line text                                                                                |
| `page_context`  | `PDFPageContext`   | Page number, dimensions, and config-provided metadata                                                              |
| `metadata`      | `dict`             | Config-provided run metadata                                                                                       |

### `PDFLineChunk` - a line of text, containing `PDFChunk`s

| Attribute        | Meaning                                                            |
| ---------------- | ------------------------------------------------------------------ |
| `x` & `y`        | Position of the line's first run                                   |
| `text`           | Concatenated text of the whole line                                |
| `bold`, `italic` | `True` if **all** runs are bold/italic                             |
| `runs`           | `list[PDFChunk]` - the child chunks (called _runs_ here), in order |

The line chunk is accessed by `.line_chunk` on a chunk.
This makes things like checking if the chunk is the first on its line
(`chunk is chunk.line_chunk.runs[0]`) or if the whole line is italic
(`chunk.line_chunk.italic`) possible.

### `WordChunk` - one docx run

| Attribute                | Meaning                                                                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `x`, `y`, `w`, `h`       | The enclosing text frame's geometry (twips, from `w:framePr`)                                                                         |
| `text`, `bold`, `italic` | Run text and formatting                                                                                                               |
| `space_before`           | `bool` (default `True`) - whitespace separates this run from the previous one. Word runs are already whole, so the default suits them |
| `run`                    | The underlying `python-docx`'s `Run` (so you can check things like `run.font.superscript`)                                            |
| `paragraph`              | The `Paragraph` (for things like `paragraph.alignment`, `paragraph.runs`)                                                             |

### `Chunk` protocol

This is a minimal interface with `x`, `y`, `text`, `bold`, `italic` & `space_before` that makes both backends work (and be typed correctly)

---

## The actual config

The primary config is defined by the `CONFIG` dict in `config.py`.
It has a few properties:

```python
CONFIG: PDFConfig = {
    "mode": "pdf",                  # the mode, either "pdf" or "word". Mostly for type checking
    "on_pop": speaker_to_utterance, # optional, runs on every pop
    "on_end": on_end,               # optional, runs once at the very end
    "alignments": { ... },          # the rule tree (required)
}
```

### Alignment groups

The `alignments` prop matches the general alignment of the chunk.
It's mostly legacy, and only (maybe) useful for Word files.
It works separate for each backend:

- **PDF** - always the group named **`"any"`**. PDF has no usable paragraph
  alignment, so there's just one bucket.
- **Word** - `parse.py` computes the run frame's horizontal center and picks a
  group by magic ranges (bad, this is what I meant by bad Word support):
  - center `4550–6560` → `"center"`
  - center `2500–3660` → `"left"`
  - center `7820–8460` → `"right"`
  - otherwise → `"_else"`

  Only the groups you define are consulted, so the ZRIP Word config uses just
  `"center"` and `"_else"`.

### Rules

An alignment group is an **ordered dict** of `{"NAME": rule}`.
Order matters, since the rules are tried top to bottom and the first match wins.
A rule is a `TypedDict`:

```python
"SPEAKER": {
    "test": lambda chunk: (
                0 < chunk.x < 55 or 271 < chunk.x < 293
            ) and leading_caps(chunk.text) >= 5,
    "action": pop_and_push_to("div", tag="note", type="speaker"),
    # "append_func": ...,   # optional
    # "after_append": ...,  # optional
    # "alignment": ...,     # Word only, optional
}
```

> The dict's keys are insignificant, they're only used for readability.

| Key            | Required  | Signature                                       | Purpose                                                                                                                                                                        |
| -------------- | --------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `test`         | yes       | `(chunk) -> bool`, **or** the literal `"_else"` | Decides if the rule fires. `"_else"` makes this the group's fallback (runs only when nothing else in the group matched)                                                        |
| `action`       | no        | `(chunk) -> Any`                                | Decides what to do, usually pops and appends a TEI tag                                                                                                                         |
| `append_func`  | no        | `(chunk) -> Any`                                | Emits the chunk's content. **If omitted, the engine calls `append(chunk)`** for you. Override it to suppress/transform the text or to control which cosmetic annotations apply |
| `after_append` | no        | `() -> Any`                                     | Side-effect after appending (e.g. flipping a state flag)                                                                                                                       |
| `alignment`    | no (Word) | `WD_PARAGRAPH_ALIGNMENT`                        | Word-only: the rule is skipped unless `chunk.paragraph.alignment` equals this                                                                                                  |

### `run_immediate`

A group may contain a key `"run_immediate"` whose value is a **plain callable**
(not a rule dict). It runs once for every chunk that lands in the group, _before_
any rule is tested. It's useful for resetting pre-chunk state - for example the Word config
clears its `visited_time` variable when a paragraph stops being centered:

```python
"_else": {
    "run_immediate": lambda: globals().update({"visited_time": False}),
    "REFERENCE_ENTRY": { ... },
    ...
}
```

### Rule lifecycle

When a rule matches:

1. `action(chunk)` runs if present
2. `append_func(chunk)` runs if present. If not, `append(chunk)` runs.
3. `after_append()` runs if present

If **no** rule matched (and there was no `"_else"`), `parse_text` falls back to
`append(chunk)`. That's what happens with plain text that isn't the start of anything new, it
simply flows into the element currently open on the stack.
This is how text is appended after a rule (like opening a `<seg>`) is matched.

> Empty chunks (`text` is empty or just a newline) are skipped.

---

## Engine primitives & helpers

This is what `test`, `action` and `append_func` would probably call. They're a set of helper functions:

```python
from engine import (
    make_chunk, pop_and_push_to, tag, tag_is_on_top,
    pop_to, push, append
)
```

### The stack

The engine holds a `stack: list[StackEntry]`. The bottom entry is the permanent
root `<div type="debateSection">`. Each `StackEntry` wraps:

- `element` - the `ET.Element` being built,
- `children` - a _pending_ list of `str | Element` not yet flushed into the
  element,
- `last_elem` - a reference for where trailing text should attach,
- `cosmetic` - whether this is an inline cosmetic wrapper (see below).

A global `children` always points at the top entry's `children` prop. Text and child
elements are appended there and are "committed" into `.text`/`.tail` when the
entry is popped. **You almost never touch `StackEntry` directly** - you drive the
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
  [Cosmetic annotations](#cosmetic-annotations))
- `pop()` closes the top element, flushes its pending children into it, attaches
  it to its parent, and fires the `on_pop` hook. You rarely call this directly.
- `pop_to(*tags, invert=False)` pops until the top **structural** element's tag is
  one of `tags` (with `invert=True`, pops _while_ it is). This is how you climb
  back out to a known anchor before opening something new:

  ```python
  pop_to("u", "div")   # climb out to the nearest <u> or <div>
  ```

  `tags` may be strings or `ET.Element`s (only the tag name is compared).

### `pop_and_push_to`

This is the action you should need most of the time. It's a factory that returns a function that pops and pushes a tag.

```python
"action": pop_and_push_to("div", tag="head", type="session")
# returns:
# def action(chunk):
#     if not on_top("head", type="session"):
#         pop_to("div")
#         push("head", type="session")
```

`pop_and_push_to(*pop_args, tag, chunked=True, **attribs)`:

- pops to `pop_args`, then pushes `tag` with `attribs`
- if `chunked=True` (default) it **skips** the pop+push when `tag` (with those
  attribs) is _already_ on top - so consecutive chunks that belong to the same
  element don't keep reopening it. This matters a lot for PDF, where one logical
  block is split across many run-chunks: you don't want ten `<note>`s, you want
  one that the later chunks append into.
- `chunked=False` forces the pop+push every time. `SEG` uses this:

  ```python
  "action": pop_and_push_to("u", "div", tag="seg", chunked=False)
  ```

  Each indented line is its own paragraph, so a new `<seg>` should start every
  time - pop the previous `<seg>` (climbing to `<u>`/`<div>`) and open a fresh one.

### `tag_is_on_top` / `is_before_layout`

```python
tag_is_on_top("note", type="speaker")   # is the top *structural* element <note type="speaker">?
tag_is_on_top("time")                    # ...is it <time>?
```

`tag_is_on_top(tag, **attribs)` tells you if you're inside the `tag` element.
It **ignores cosmetic wrappers** like `<emph>`/`<hi>` and skips to the first
structural element, then checks both tag and the given attributes. This is the
backbone of stateful rules: e.g. CHAIRMAN fires if we're already in `<time>` (the
chairman line always follows the time) or already in a `<note type="chairman">`.

`is_before_layout(element)` is the cosmetic counterpart: it returns `True` if a
cosmetic wrapper matching `element` is currently open (sits above the last
structural element). The cosmetic annotations system uses it to decide whether an
`<emph>`/`<hi>` is already open. You normally won't call it directly.

`tag(name, **attribs)` is a tiny helper that builds a bare `ET.Element`, used to
declare the identity of a cosmetic annotation (see below).

### `append`

```python
append(chunk)                                  # emit text, auto-apply all cosmetics
append(chunk, should_annotate=["REFERENCE"])   # only apply the REFERENCE annotation
append(chunk, should_annotate=[])              # plain text, no cosmetics
```

`append(*chunks, should_annotate=True)` is what actually puts text into the open
element. For each chunk it:

1. trims the text
2. runs the [cosmetic annotations](#cosmetic-annotations) selected by
   `should_annotate` (`True` = all of them, or a list of names. An unknown name
   raises)
3. inserts a single separating space _before_ the token when
   [`chunk.space_before`](#spacing) is true (and there's something to separate it
   from — see below)
4. appends the token

`should_annotate` is how you stop double-formatting: a `GENERIC_NOTE` already wraps
its body in `<hi rend="italic">`, so it appends with `should_annotate=["REFERENCE"]`
(ZRIP) or `[]` (prosvetno) to keep the italic cosmetic from firing again inside it.

### Spacing

A searchable PDF has no real spaces or line breaks - the extractor reconstructs
them. Rather than baking reconstructed spaces into the text (and then fighting to
strip them back out), each chunk carries one boolean:

- **`chunk.space_before`** - `True` when this run is separated from the previous one
  by whitespace (a glyph gap, a deljaj, or a line break). The extractor
  sets it (see [Writing `get_chunks`](#writing-get_chunks)) - the engine turns it into
  at most one separating space.

The engine makes the whole thing predictable:

- A run's own text is trimmed to a single token, so **no element ever ends in a
  stray space**.
- The separator is emitted **between** tokens only. At a **block start** (the
  enclosing element is still empty) it's withheld, so a fresh `<seg>`/`<note>` never
  begins with a leading space.
- When a run opens an inline element (`<emph>`, `<hi>`, `<ref>`), the separator is
  placed **outside** it, in the nearest enclosing element that already has content —
  so you get `beseda <emph>poudarjeno</emph> naprej`, never `beseda<emph> ...`.

If this behavior is not what you want or is bugged, fix where `get_chunks` decides `space_before`.
You can usually just leave it as-is though.

---

## Cosmetic annotations

Cosmetic annotations are **inline formatting that can appear inside anything and
doesn't change layout**, such as bold, italic and footnote references. They're handled
separately from rules so you don't have to think about them in every rule: any
time you `append` text, the engine opens/closes the right inline wrapper around it.

`COSMETIC_ANNOTATIONS` in `config.py` is an ordered dict of `{"NAME": annotation}`:

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
        "append_func": lambda chunk: push(    # custom open, since the tag is dynamic (we need `chunk`)
            "ref",
            target=f'#note{re.sub(r"[^a-zA-Z0-9]", "", chunk.text.strip())}',
        ),
    },
}
```

Note that this time, the dict's key names actually matter because they get matched in `should_annotate`.

Each annotation has:

| Key           | Purpose                                                                                                                                                                                                             |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test`        | `(chunk) -> bool` - whether the chunk has the formatting                                                                                                                                                            |
| `tag`         | A bare `ET.Element` (build it with `tag(...)`) used as the wrapper's **identity** - the engine matches against it to know whether the wrapper is already open and to close it.                                      |
| `append_func` | _Optional_ custom logic for opening the tag if the simple `push(tag)` isn't enough (e.g. `REFERENCE` computes a `target` attribute from the chunk text). I should probably rename this to "push_func" or something. |

**How `append` applies them** (for `PDFChunk`): for each annotation whose name is
in `should_annotate`,

- if its wrapper is **not** already open and `test(chunk)` is true, open it
  (`push(tag, cosmetic=True)`, or call `append_func`)
- if its wrapper **is** open and `test(chunk)` is false, close it
  (`pop_to(tag, invert=True)`).

So a run of italic chunks opens one `<emph>` on the first and the engine keeps it
open until a non-italic chunk closes it. `cosmetic=True` is what makes
`tag_is_on_top` and `pop_to` look _past_ these wrappers to the real structural
element. Spacing across a wrapper boundary just works: because the separator is
emitted _outside_ the element being opened (see [Spacing](#spacing)), `od <emph>…`
never fuses into `od<emph>…`, and no wrapper is left holding a trailing space.

---

## Hooks: `on_pop` and `on_end`

### `on_pop(popped: StackEntry)`

You can optionally set an `"on_pop"` hook in `CONFIG`. It runs **every time any element is popped**, receiving the
`StackEntry` that just closed. The configs use it to build utterances from speaker
notes, since all of a tag's children are only known when it is popped:

```python
def speaker_to_utterance(popped):
    if popped.element.tag == "note" and popped.element.attrib.get("type") == "speaker":
        text = "".join((i.text or "") if isinstance(i, ET.Element) else i
                       for i in popped.children)

        name = re.sub(r"^pre\S+\s+|\s*(?:\(|,|:).*$", "", text, flags=re.IGNORECASE)
        serialized = "#" + "".join(w.capitalize() for w in name.split())  # #NameSurname
        utterance_speaker_mapping.setdefault(serialized, text)
        push("u", who=serialized)   # open the <u> right after the speaker note closes
```

The speaker note closes when the next block (e.g. a `<seg>`) opens. At that point
the hook fires and opens `<u who="#Name">` so the body lands inside an utterance.
`utterance_speaker_mapping` is a mapping of `{"#PascalName": "full speaker line"}` - used for getting the list of persons in the document and their affiliations.
It's then used to build a `<listPerson>` tag (check the [listPerson tool](#running-it))

### `on_end()`

Set via `CONFIG["on_end"]`. Runs once after the whole document is parsed and the
tree is committed. ZRIP uses it to dump the speaker map for the
[listPerson tool](#running-it):

```python
def on_end():
    with open("out/speaker_utterance.json", "w", encoding="utf-8") as f:
        json.dump(utterance_speaker_mapping, f, ensure_ascii=False)
```

---

## Writing `get_chunks`

`get_chunks(filename)` is a generator that yields the `Chunk` stream. It's part of
the config because reconstructing words, runs and lines from raw glyphs is
document-specific. The shape is the same in both PDF configs; tune the thresholds
and grouping to your document.

Steps (see `get_chunks` in `config.py`):

1. **Collect characters.** A `pdfminer` `PDFLayoutAnalyzer` subclass walks each page and
   records every `LTChar`.
2. **Group characters into lines.** Walk the page's characters and start a new line when the
   y-drop between consecutive characters exceeds `line_treshold` (≈ 4.8), with a larger
   guard (`x 5`) for big jumps (column breaks).
3. **Group a line into runs.** Within a line, merge consecutive characters into a run
   while the **font name and size match** and the x-gap is small. A gap _inside_ a
   run (x-gap over `threshold` ≈ 1.7) becomes a real space in the run's text. A gap
   _between_ runs sets the next run's [`space_before`](#spacing) instead. PDFs
   usually have no literal space glyphs, so this is how spacing is inferred from gaps.
4. **Carry the line break.** A `pending_space` flag remembers that a line ran to its
   end, so the **first run of the next line** gets `space_before=True` - that's what
   keeps lines word-separated without baking a trailing space onto every line.
5. **Build chunks.** For each run, make a chunk by `make_chunk(text=..., x=..., y=..., font_name=...,
size=..., previous=prev_run, page_num=..., space_before=...)` - a `PDFChunk`,
   linked to the previous one. Then build one `PDFLineChunk` for the line, and set
   `run.line_chunk` on each run so rules can navigate line ↔ run.
6. **Yield.** `yield from run_chunks`.

```python
prev_run = make_chunk(text=r["text"], x=r["x0"], y=r["y0"],
                      font_name=r["fontname"], size=r["size"],
                      previous=prev_run, page_num=page_num,
                      space_before=r["space_before"])
```

The two configs differ in detail - `prosvetno`'s document already has space glyphs
(so it derives `space_before` from whitespace at run edges rather than from gaps),
skips a header band (`y > 740`), and handles even/odd-page indentation quirks -
this is why the config is _very_ document-specific.

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

> Word mode is older than the current engine and not actively kept in
> sync.
> Treat `config_word.py` as a reference for the _rule style_, not as an actual
> working config. **PDF gud, DOCX bad**

---

## Finding the magic numbers

Configs are full of coordinates and font sizes. Two tools help you measure them so
you're not guessing:

- **`tools/pdf_coords.html`** - a small tool for measuring coordinates on a PDF file.
  Open it in a browser, drop your PDF in, and move/click on the page. It reads out
  the cursor's **pdfplumber** coordinates (`x0/x1`, `y0/y1`, `top/bottom`) and the Δ
  between clicked markers. These are the exact `x`/`y` your `PDFChunk` tests see.
  Everything stays local (obviously).
- **`tools/dump_text_coords.py`** - `python3 tools/dump_text_coords.py file.pdf
[--page N] [--grep WORD]` dumps, per text chunk, the transformation-matrix
  translations (`cm[4]/cm[5]`, `tm[4]/tm[5]`) and font size that pypdf reports, to
  cross-check positions. Throw this to an AI agent, or something.

Workflow: find an element you want to classify (a session header, a footnote
marker), measure its x/y/size, and write a `test` that brackets those values with a
little tolerance. Page misalignment in scans means you'll often need ranges
(`48 < x < 64`) rather than exact values, and sometimes per-page configurations.

---

## ZRIP config

Reading `config.py` (the `"any"` group) top to bottom:

| Rule               | Fires when...                                                                              | Produces                                       |
| ------------------ | ------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| `HEADER`           | the chunk is one of the first 3 elements on a fresh page (running header)                  | nothing (`append_func` drops it)               |
| `SEJA_DECLARATION` | bold, at the title band (`605 < y < 610`) or already inside the session head               | `<head type="session">`                        |
| `TIME`             | italic, at the time band or following a session section                                    | `<time>`                                       |
| `CHAIRMAN`         | starts with `PREDSEDUJE`, or follows `<time>`/is already in chairman, not italic, indented | `<note type="chairman">`                       |
| `SEJA_SECTION`     | all caps, centered, first body line of a fresh page                                        | `<head type="sessionSection">`                 |
| `REFERENCE_ENTRY`  | font size == 7.0, a digit, first run on its line (a footnote definition's marker)          | `<note place="foot" n="...">`                  |
| `GENERIC_NOTE`     | at the note band and the **whole line** is italic                                          | `<note><hi rend="italic">...`                  |
| `SPEAKER`          | not indented and >= 5 leading capital letters                                              | `<note type="speaker">` (`on_pop` opens `<u>`) |
| `SEG`              | `is_seg(chunk)` - first run of a correctly-indented body line                              | a new `<seg>` inside the current `<u>`/`<div>` |

Things worth copying from it:

- **Order as precedence.** `REFERENCE_ENTRY` (digit whose font size is 7) is listed before
  `GENERIC_NOTE`/`SEG` so a footnote marker is claimed before a body rule sees it.
- **State via the stack, not globals.** `CHAIRMAN` and `TIME` test
  `tag_is_on_top(...)` to chain off the previous block instead of tracking a flag.
  This is by preference though, you can also use global state.
- **`chunked`** `REFERENCE_ENTRY` guards with
  `tag_is_on_top("note", place="foot", n=serialized)` so the multi-run footnote body
  opens exactly one `<note>`. `SEG` uses `chunked=False` to restart every paragraph.
- **Geometry helpers.** `is_seg`, `is_page_top`, `nth_previous`, `leading_caps`
  are useful helper functions, and avoid moving the config tests to a new function (instead of a lambda).

`examples/prosvetno-kulturno-vece/config.py` is a cleaner, smaller second example
(no footnotes, simpler cosmetics) and a good template to start a new config from.

---

## Gotchas

- **Footnote/multi-run blocks split across chunks.** One logical block becomes many
  run-chunks in PDF. Always guard pushes with `tag_is_on_top(...)` (or use
  `chunked=True`, the default) so you open one element, not one per run. Conversely,
  use `chunked=False` when each chunk genuinely _is_ a new block (paragraphs/`SEG`).
- **Coordinates are per-document and per-page.** Scans are misaligned! expect to use
  ranges, page-number branches (`left = 48 if page_num <= 2 else 41`), and
  even/odd-page parity (`prosvetno`'s `is_seg`). Re-measure with the tools above.
  Nobody told you this was gonna be fun, just _possible_.
- **y grows upward.** "Top of page" = large y. Footnotes sit at small y.
- **Don't double-format.** When a rule already opened an inline wrapper
  (`GENERIC_NOTE` -> `<hi rend="italic">`), append with `should_annotate=[...]` to
  stop the cosmetic layer re-wrapping the same text.
- **Spacing is reconstructed.** If words run together or get extra spaces, the fix is
  in `get_chunks` - specifically where it decides each run's
  [`space_before`](#spacing) — not in your rules or the engine.

---

## Debugging

When making a config, expect some tests or chunks not to work immediately.
Use `--diagnostics report.json` first: it records rule hit counts, unmatched
samples, page counts, and font/size distributions. For the full low-level log,
set `"debug": True` in the config and pass `--debug-file debug.txt`; that file
can be extremely large.

## Running it

```bash
# 1. Convert with an explicit config and audit outputs
python parse.py path/to/input.pdf --config path/to/config.py -o out/out.xml \
  --diagnostics out/diagnostics.json --data-output out/data.json

# 2. Turn an exported speaker map into a TEI <listPerson>
python make_list_person.py out/data.json -o out/listPerson.xml
```

To convert a _different_ document, write a new config module exporting `CONFIG`,
`COSMETIC_ANNOTATIONS` and `get_chunks`, then select it with `--config`.
There is no need to copy it to the repository root or maintain a symlink.

Dependencies: `pdfminer.six` (PDF extraction), `python-docx` (Word mode),
`pypdf` (the coord-dump tool, should be reworked into pdfminer), `requests` (`make_list_person.py`).
