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
doc2tei parses this as a small pipeline:

1. **An extractor** reads raw PDF characters or words and builds temporary
   page-level `LineRecord` and `RunRecord` objects.
2. Config callbacks may filter those records or enrich them with document-specific metadata.
3. The extractor converts accepted records into a stream of _chunks_ - small
   pieces of text with position, font, style, line, page, and spacing information.
4. **The rule engine** feeds each chunk through your ordered rules. A matching
   rule can mutate the TEI stack and control how the text is emitted.

```text
PDF -> extractor -> records -> chunks -> ordered rules -> TEI tree
```

Everything that involves the document lives in the config. It's what determines something like
"session headers in this PDF are between y 600 and 610, and footnotes' font size is 7."

Because PDF files differ **a lot** (in their coordinates, fonts and layout), rules usually need to be _very_ specific.
This means that **almost every new document needs its own config**, though similar documents can be parsed with the same config, assuming it's written well.

doc2tei's output is a `<TEI>` tree:

```
TEI > teiHeader? + text > body > div[type=debateSection] > ( head | time | note | u > seg | ... )
```

The `teiHeader` is optional and comes from `CONFIG["tei_header"]`; the
`text > body > div` skeleton is only the default and can be replaced with
`CONFIG["document"]` (see [The TEI header and document
skeleton](#the-tei-header-and-document-skeleton)).

## Quick start

Run the CLI with an explicit config:

```bash
python parse.py input.pdf --config path/to/config.py -o output.xml
```

Add audit and config-produced data files when tuning a config:

```bash
python parse.py input.pdf --config path/to/config.py -o output.xml \
  --diagnostics diagnostics.json --data-output data.json
```

- `diagnostics.json` contains rule hit counts, unmatched samples, page counts,
  fonts, sizes, and any fail-soft recovery events. "Unmatched" means that no
  structural rule fired; the chunk was still appended normally.
- `data.json` contains values placed in `result.data` by config hooks, such as a
  speaker mapping.

The same operation is available as a library API:

```python
from doc2tei import parse_document

result = parse_document("input.pdf", config="path/to/config.py")
result.write_xml("output.xml")
result.write_diagnostics("diagnostics.json")
result.write_data("data.json")

# Or inspect these directly:
result.root
result.diagnostics
result.data
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
3. For every chunk, selects the PDF rule list directly (or a legacy alignment
   group for Word) and tests its rules in order.
4. The first matching rule runs.
   If none match, the chunk's text is appended into whatever element is currently
   open on the stack.
5. After the last chunk, the engine pops every still-open element back down to the
   root `<div>` and commits the tree.
6. Calls `on_end(result)` if the config defines it (zero-argument legacy hooks
   are still supported).
7. The tree is stringified and written to the output file (or printed).

```
INPUT ─▶ get_chunks() ─▶ Chunk stream ─▶ ordered rules ─▶ rule fires
                                                │                         │
                                                │                         ├─ action()        (mutate stack)
                                                │                         ├─ append_func()    (emit text)
                                                │                         └─ after_append()   (update state)
                                                │
                                                └─ no match ─▶ append(chunk) into open element
```

</details>

## The config module

A runnable config is normally stored as `examples/<document-family>/config.py`.
The root `config.py` is only a pointer and intentionally does not run. A config
module exports (at least):

- `CONFIG` - a WordConfig or a PDFConfig, containing the rule tree
- `COSMETIC_ANNOTATIONS` - inline formatting rules
- `get_chunks(filename) -> Iterable[Chunk]` - the extractor

All of these will be explained later.

The config path is not global anymore. Select it explicitly with
`--config path/to/config.py`, or pass it to `parse_document(..., config=...)`.
Old rule dictionaries and zero-argument hooks remain supported.

A minimal complete PDF config looks like this:

```python
from doc2tei import CharacterPDFExtractor, LineStart, Text, rule, rule_group
from engine import pop_and_push_to
from type_decs import PDFConfig, PDFCosmeticAnnotations


get_chunks = CharacterPDFExtractor(
    line_tolerance=4.0,
    literal_spaces="preserve",
)

COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {}

CONFIG: PDFConfig = {
    "mode": "pdf",
    "debug": False,
    "rules": rule_group(
        rule(
            "TITLE",
            Text(starts_with="SESSION", source="line"),
            action=pop_and_push_to("div", tag="head"),
        ),
        rule(
            "PARAGRAPH",
            LineStart(),
            action=pop_and_push_to("div", tag="p", chunked=False),
        ),
    ),
}
```

`get_chunks` is callable because extractor instances implement `__call__`.
`COSMETIC_ANNOTATIONS` may be empty, but it must be exported. The parser loads
the module, resets its engine state, obtains the chunk stream, and evaluates
`CONFIG["rules"]` from top to bottom for every PDF chunk.

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
    merge_nearby_runs=True,      # default: merge harmless PDF size jitter
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
    preserve_word_runs=True,       # retain superscript/font geometry per word
    page_enricher=enrich_page,
    line_enricher=enrich_line,
    page_error_handler=record_page_error,
)
```

Both accept document callbacks for filtering, stopping, and metadata
enrichment. `PDFChunk.page_context.metadata`, `PDFChunk.metadata`, and
`PDFLineChunk.metadata` let rules consume those decisions without adding
one-off attributes to core chunk classes.

#### Choosing an extractor

Use `CharacterPDFExtractor` when the PDF has a reliable character layer and
meaningful font changes. It may emit several chunks for one printed line when
font or size changes.

Its main options are:

- `pages=PageRange(start, end)` - zero-based, end-exclusive pages.
- `line_tolerance` - vertical movement that starts a new line.
- `line_break(previous_y, current_y)` - optional replacement for the standard
  line tolerance test.
- `literal_spaces="preserve"` - retain real PDF space glyphs.
- `literal_spaces="break"` - split at a literal space into independently
  positioned records, as required by PDFs such as ZRIP. Text after the space
  is retained as the next record.
- `literal_spaces="ignore"` - discard literal space glyphs.
- `gap_threshold` - horizontal distance used to infer a space.
- `merge_nearby_runs=True` - merge adjacent same-font runs split only by
  insignificant PDF font-size/baseline jitter. Line starts, font/style
  transitions, superscript-like baseline changes, significant size changes,
  and rendered whitespace are preserved. Set it to `False` to restore exact
  font-size grouping. The optional `font_size_tolerance` (default `0.1`) and
  `baseline_tolerance` (default `0.25`) tune the conservative comparison.
- `max_run_x_gap` - prevent distant same-font characters from being merged.
- `line_filter`, `stop_before`, `page_enricher`, `line_enricher` - document callbacks.
- `page_error_handler(page_num, error)` - optionally skip and report one broken
  page instead of aborting the remaining document.

Use `WordPDFExtractor` when pdfplumber's reconstructed words are more reliable
than the raw character stream. Despite its name, it is still a PDF extractor;
by default it reconstructs each accepted printed line into one `PDFChunk`.

Its main options are:

- `x_tolerance`, `y_tolerance` - pdfplumber word-extraction tolerances.
- `line_tolerance` - controls grouping extracted words into lines.
- `word_gap` - horizontal gap that inserts a reconstructed space.
- `join_line_end_hyphens` - remove a line-ending hyphen and join the next line.
- `preserve_word_runs` - emit contiguous font/size runs while retaining a
  reconstructed whole-line view. This is useful for superscripts and footnotes.
- the same page range and record callbacks described above.

### Extraction records

Records are temporary objects between raw PDF extraction and parser chunks.
They let a config inspect a complete page and annotate its lines before the
parser begins. Records never enter the rule engine.

A `LineRecord` contains:

| Attribute                | Meaning                                                                 |
| ------------------------ | ----------------------------------------------------------------------- |
| `text`                   | Reconstructed complete line text                                        |
| `x`, `y`                 | Position of the line's first run; `y` grows upward from the page bottom |
| `font_name`, `font_size` | Representative line font and size                                       |
| `runs`                   | The line's `RunRecord` objects                                          |
| `metadata`               | Document-specific line facts added by the config                        |

A `RunRecord` contains `text`, `x`, `y`, `font_name`, `font_size`, and
`space_before`. With character extraction it represents a same-font portion of
the line. With word extraction there is currently one run record per line.

Every callback also receives a `PDFPageContext` with:

- `page_num` - zero-based page index.
- `width`, `height` - PDF page dimensions.
- `metadata` - page-level facts added by the config and later available on chunks.

You normally do not instantiate records. The extractor creates them and passes
them through callbacks in this order:

1. Extract raw characters or words and build every non-empty `LineRecord` on the page.
2. Call `page_enricher(page, records)` with the complete page.
3. For each record, call `stop_before(record, page)`.
4. Call `line_filter(record, page)`; a false result permanently drops the line.
5. Call `line_enricher(page, record, index, records)` for accepted lines.
6. Convert accepted records into `PDFChunk`/`PDFLineChunk` objects and yield them.

Filtering a running header can be as small as:

```python
from doc2tei import LineRecord
from engine import PDFPageContext

def below_running_header(line: LineRecord, page: PDFPageContext) -> bool:
    return line.y <= 740

get_chunks = CharacterPDFExtractor(
    line_tolerance=4,
    literal_spaces="preserve",
    line_filter=below_running_header,
)
```

Use `line_filter` only when dropping source text is intentional. A lossless
config should instead mark the record in `line_enricher` and route it to a TEI
element; the general config uses `<note type="sourceArtifact">` for this.

Page and line enrichers are useful for derived geometry:

```python
def enrich_page(page: PDFPageContext, records: list[LineRecord]) -> None:
    body_x = [line.x for line in records if 8.8 <= line.font_size <= 10.6]
    page.metadata["page_left"] = min(body_x) if body_x else 0.0
    page.metadata["session_page"] = any(
        line.font_size >= 11.5 and line.text.lower().endswith("seja")
        for line in records
    )

def enrich_line(
    page: PDFPageContext,
    line: LineRecord,
    index: int,
    records: list[LineRecord],
) -> dict[str, object]:
    left = page.metadata.get("page_left")
    return {
        "indented": isinstance(left, (int, float)) and line.x > float(left) + 14
    }
```

The shared extractors recognize `record.metadata["out_of_flow"] = True` for
page furniture that should be yielded but must not become the predecessor of
the next body chunk. In `WordPDFExtractor` it also preserves the pending-space
and dehyphenation state across that record. The general config uses this when
retaining running headers and page numbers.

Rules can consume the result with `Metadata("indented", True, source="line")`
or `Metadata("session_page", True, source="page")`.

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
    "rules": rule_group(
        rule(
            "SESSION",
            AllOf(LineStart(), Text(pattern=r"^\d+\. seja$", source="line")),
            action=open_session,
        ),
        rule("SEG", body_start, action=open_segment),
    ),
}
```

Rule order is still precedence. These helpers package common tests; they do
not introduce parliamentary concepts into the parser. Rule mappings are
validated and compiled once per document after `on_start`; treat their
definitions as immutable while chunks are being processed.

The selector helpers are:

- `LineStart()` - first chunk in a printed line.
- `Text(...)` - exact, prefix, suffix, or regular-expression matching against
  chunk text or `source="line"` text; `normalize_space=True` is available.
- `Attribute("font_size", Between(8.8, 10.6))` - test a chunk attribute.
  `Between` is exclusive unless `inclusive=True`.
- `Metadata(key, expected, source="chunk" | "line" | "page")` - test enriched metadata.
- `AllOf(...)`, `AnyOf(...)`, and `Not(...)` - compose selectors.

Ordinary functions remain the right choice for genuinely document-specific tests:

```python
def is_paragraph_start(chunk: PDFChunk) -> bool:
    return (
        chunk.is_line_start
        and 8.8 <= chunk.font_size <= 10.6
        and chunk.x > 50
    )
```

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

`FootnoteLinker` packages the run-level recognition, state, ID allocation, and
stack actions needed for PDF footnotes. The config still supplies its adaptive
document signals, so the helper does not assume a particular font size or page
height:

```python
from doc2tei import FootnoteLinker

footnotes = FootnoteLinker(
    body_size=current_body_size,
    mode=current_extraction_mode,
    structural_page=is_structural_page,
    utterance_context=has_utterance_context,
)

CONFIG["cosmetic_annotations"]["REFERENCE"] = {
    "test": footnotes.is_inline_reference,
    "tag": tag("ref", type="footnote"),
    "append_func": footnotes.inline_reference_action,
}

def start_document():
    footnotes.reset()
    # Open any document-specific initial structure here.

def finish_document(result):
    footnotes.finalize()  # unresolved raised digits become superscript <hi>
```

Its rule-facing methods are `is_consumed_run` / `consumed_run_action`,
`is_entry` / `entry_action`, `is_continuation`, and `is_after` /
`after_action`. A config may override the relative footer band with `y_min`
and `y_max`; the defaults are fractions of page height, not fixed coordinates.

Shared type-only declarations live in `type_decs.py`: config `TypedDict`s,
chunk/parser protocols, callback and selector aliases, extractor hook types,
and the Word/PDF rule types. Runtime dataclasses such as `PDFChunk`,
`ParseResult`, and `TEIHeader` stay beside the code that constructs and uses
them; `type_decs.py` refers to those models only through cycle-safe forward
references.

---

## The TEI header and document skeleton

### `CONFIG["tei_header"]`

ParlaMint documents carry a `<teiHeader>` with file, encoding, profile and
revision metadata ([sample](https://github.com/clarin-eric/ParlaMint/blob/main/Samples/ParlaMint-SI/2022/ParlaMint-SI_2022-04-06-SDZ8-Izredna-99.xml)).
`doc2tei.tei_header.TEIHeader` builds that structure; every field defaults to
empty, so you only fill what you know and get a complete, blank skeleton for
the rest:

```python
from doc2tei.tei_header import Meeting, Setting, SourceBibl, TEIHeader

TEI_HEADER = TEIHeader(
    language="sl",                       # xml:lang on the <TEI> root
    tei_id="ZRIP-7",                     # xml:id on the <TEI> root
    main_titles={"sl": "...", "en": "..."},  # {xml:lang: text}
    meetings=[Meeting(text="7. seja", n="7")],
    source=SourceBibl(titles={"sl": "..."}),
    setting=Setting(city="Beograd", country_key="YU", country_name="Jugoslavija"),
)

CONFIG = {..., "tei_header": TEI_HEADER}
```

The parser inserts the built header as the first child of `<TEI>` and applies
`tei_id`/`language`/`ana` as root attributes. Multilingual fields are
`{xml:lang: text}` mappings. The repeatable pieces have their own small
dataclasses: `Meeting`, `RespStmt` + `Person`, `Funder`, `Measure`,
`SourceBibl`, `Setting`, `Change` (all importable from `doc2tei`).

Two header parts are **computed from the parsed document** because they can't
be known up front: `<tagsDecl>` (per-tag usage counts) and `<extent>`
(speech/word counts). After the tree is committed the parser fills each one
that was left empty - provide your own `measures=[...]` (or a non-empty
`tagsDecl`) and they are left alone.

`CONFIG["tei_header"]` also accepts a **raw `ET.Element`** (any `<teiHeader>`
you built yourself) or a **callable returning either**, so nothing forces you
through the dataclass:

```python
"tei_header": my_header_element,          # full manual control
"tei_header": lambda: build_header(...),  # built fresh per parse
```

For surgical edits beyond that, remember that `on_start(result)` and
`on_end(result)` receive the whole tree as `result.root` - the header (and
anything else) can be mutated there; `on_end` runs after the computed counts
are filled, so it can override them.

### `CONFIG["document"]`

The default output skeleton is `TEI > text > body > div[type=debateSection]`,
with parsed content appended into the `<div>`. To replace it, export a
factory returning `(root, content)` - the root element and the descendant
that parsing should append into:

```python
import xml.etree.ElementTree as ET

def make_document():
    tei = ET.Element("TEI", xmlns="http://www.tei-c.org/ns/1.0")
    text = ET.SubElement(tei, "text")
    front = ET.SubElement(text, "front")
    return tei, front

CONFIG = {..., "document": make_document}
```

`engine.default_document()` builds the standard skeleton, so a factory can
also start from it and just decorate (extra attributes, siblings, a
different content `<div>`). `tei_header` composes with `document`: the header
is inserted as the first child of whatever root the factory returns.

---

## The chunk model

Every action / function in the config receives a _chunk_. It then does something with it (performs checks, pushes, pops, whatever).
Records belong to extraction; chunks belong to parsing. Once a record has been
converted into chunks, rules never see the record again.
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
| `page_context`   | The shared `PDFPageContext` for the page                           |
| `metadata`       | Line metadata returned by `line_enricher`                          |

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

Each runnable config is defined by its `CONFIG` dictionary.
It has a few properties:

```python
CONFIG: PDFConfig = {
    "mode": "pdf",
    "debug": False,
    "rules": { ... },               # ordered PDF rules (required)
    "on_start": on_start,           # optional, after engine reset
    "on_pop": on_pop,               # optional, whenever an element closes
    "on_end": on_end,               # optional, after the tree is committed
    "auto_xml_ids": True,           # optional, generate xml:id on structural elements
    "recover_errors": True,         # optional, preserve partial output on content errors
    "merge_nearby_runs": True,      # general config: tolerate harmless PDF size jitter
    "page_workers": 0,              # general config: auto-parallel page extraction
    "tei_header": TEI_HEADER,       # optional, a <teiHeader> for the output
    "document": make_document,      # optional, replaces the default TEI skeleton
}
```

With `"auto_xml_ids": True`, every structural element opened by `push()` gets
a ParlaMint-style generated id (`<input-basename>.<tag><N>`, e.g.
`slo-53-899.seg12`, counted per tag) unless the push already provides an
explicit `xml:id`. Cosmetic wrappers (`<emph>`, `<hi>`, `<ref>`) are never
auto-id'd. Invalid filename characters and digit-leading basenames are repaired
to conservative XML names. The default is `False`.

With `"recover_errors": True`, document-content failures in extraction, rule
tests/actions, lifecycle hooks, stack closing, or header count filling are
recorded in diagnostics and in header `note[type="conversionRecovery"]`
elements. The parser keeps the partial tree and preserves the affected chunk in
an unparsed block where possible. Missing inputs, invalid config modules, and
filesystem write failures remain hard errors because there is no document or
output target to recover from.

The shipped general config reads `"merge_nearby_runs"` and passes it to its
character extractors; it defaults to `True`. A custom config that constructs
`CharacterPDFExtractor` directly should set the identically named constructor
argument instead.

The general config also reads `"page_workers"` for both character and word/OCR
PDF extraction. `0` automatically uses up to eight local worker processes, `1`
forces sequential extraction, and values above one set an explicit maximum.
Workers only perform pdfminer/pdfplumber page interpretation and stateless
line/run construction. Page and line enrichment, cross-page spacing and
dehyphenation, chunk links, and rule matching remain sequential and ordered, so
configuration callbacks do not need to be process-safe. Small documents and
interactive Windows sessions automatically use the sequential path. A custom
character `line_break` callback also uses the sequential path; use the built-in
`line_break_mode="downward"` when that policy is suitable and parallel extraction
is desired.

For batch pipelines, avoid nesting two full levels of process parallelism. Run
one document at a time with `"page_workers": 0`, or parallelize whole documents
and set `"page_workers": 1` in each document process. `batch_parse.py` applies
this automatically: one-document runs retain the config setting, while
multi-document runs default to one page worker per document.

PDF configs have one top-level `rules` group. There is no alignment wrapper.

### Word alignment groups

`alignments` is a legacy Word-only feature. For Word, the parser computes the
run frame's horizontal center and picks a group by legacy coordinate ranges:

- center `4550-6560` -> `"center"`
- center `2500-3660` -> `"left"`
- center `7820-8460` -> `"right"`
- otherwise -> `"_else"`

Only the groups you define are consulted, so the ZRIP Word config uses just
`"center"` and `"_else"`. A custom Word config can provide
`route_alignment(chunk) -> str` to replace the legacy coordinate routing.

### Rules

`CONFIG["rules"]` is an **ordered dict** of `{"NAME": rule}` for PDF.
Each Word alignment group has the same shape.
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

> Rule names do not affect matching, but they appear in logs and diagnostics,
> so give them stable descriptive names.

| Key            | Required  | Signature                                       | Purpose                                                                                                                                                                        |
| -------------- | --------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `test`         | yes       | `(chunk) -> bool`, **or** the literal `"_else"` | Decides if the rule fires. `"_else"` makes this the group's fallback (runs only when nothing else in the group matched)                                                        |
| `action`       | no        | `(chunk) -> None`                               | Decides what to do, usually pops and appends a TEI tag                                                                                                                         |
| `append_func`  | no        | `(chunk) -> None`                               | Emits the chunk's content. **If omitted, the engine calls `append(chunk)`** for you. Override it to suppress/transform the text or to control which cosmetic annotations apply |
| `after_append` | no        | `() -> None`                                    | Side-effect after appending (e.g. flipping a state flag)                                                                                                                       |
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

If **no** rule matched (and there was no `"_else"`), the parser falls back to
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
    pop_to, push, append, append_comment
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
push("note", attribs={"xml:id": "note3"}, place="foot", n="3")  # mixed attrs
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

`append_comment(text)` adds a safely escaped XML comment at the current stack
position. It is useful for review markers; comments are ignored by generated
`tagsDecl` and word counts.

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

`COSMETIC_ANNOTATIONS` in the selected config is an ordered dict of
`{"NAME": annotation}`:

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

## Lifecycle hooks and result data

The optional hooks are:

- `on_start(result)` - after engine state is reset, before extraction begins.
- `on_pop(popped)` - whenever an XML element closes.
- `on_end(result)` - after all chunks are parsed and the TEI tree is committed.

Zero-argument legacy start/end hooks are still accepted. New hooks should use
the result object. It contains `root`, `diagnostics`, and a free-form `data`
dictionary. Put auxiliary output in `result.data`; the parser itself does not
write config-specific files.

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

### `on_end(result)`

Set via `CONFIG["on_end"]`. Runs once after the whole document is parsed and the
tree is committed:

```python
def on_end(result: ParseResult) -> None:
    result.data["speakers"] = dict(utterance_speaker_mapping)
```

The CLI writes that dictionary only when `--data-output` is supplied.

### Optional speaker helper

Debate semantics are not built into the parser. A config may opt into the
configurable `SpeakerUtteranceHook`:

```python
from doc2tei import SpeakerUtteranceHook

def make_speaker_id(text: str) -> str:
    return "#" + "".join(word.capitalize() for word in text.split())

speakers = SpeakerUtteranceHook(identifier=make_speaker_id)

CONFIG = {
    "mode": "pdf",
    "debug": False,
    "rules": { ... },
    "on_start": speakers.reset,
    "on_pop": speakers,
    "on_end": speakers.export,
}
```

---

## Writing `get_chunks`

`get_chunks` only needs to be callable as `get_chunks(filename)` and yield
objects satisfying the `Chunk` protocol. For the supported PDF paths, assign a
configured extractor instance directly:

```python
get_chunks = CharacterPDFExtractor(...)
# or
get_chunks = WordPDFExtractor(...)
```

The shared extractors handle page iteration, raw extraction, record creation,
spacing, line chunks, page context, and the `previous` linked list. Keep the
document decisions explicit through their thresholds and callbacks.

If you bring a completely different source format, implement your own callable
that yields `Chunk` objects. A chunk minimally has `x`, `y`, `text`, `bold`,
`italic`, and `space_before`. PDF rules usually also depend on the richer
`PDFChunk` line, page, font, and metadata fields.

Magic numbers such as line tolerances, header bands, and indentation ranges are
still document-specific and normally need to be measured for every new family.

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

Reading `examples/zbor-republik-in-pokrajin/config.py`
(`CONFIG["rules"]`) top to bottom:

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
`examples/seje-1957/config.py` demonstrates word reconstruction, page and line
metadata enrichment, an explicit appendix cutoff, and result-data export.
Each example directory also includes an `out.xml` reference output.

## The general config

`examples/general-config/config.py` is a single config that parses all bundled test
documents. Instead of per-document magic numbers it probes the PDF first
(space-glyph ratio, OCR text layer, dominant font size) to pick one of three
extraction profiles, then detects columns per page by clustering line-start x
positions so indentation tests are column-relative. Session headings, running
headers, speakers, dates, and table-of-contents blocks are recognized by
unioned text heuristics rather than coordinates.

The general config is deliberately loss-aware: probable page furniture and
speaker-index lines are retained under `<teiHeader>/<fileDesc>/<notesStmt>` as
`<note type="sourceArtifact" subtype="..." n="source-page">` instead of being
filtered out. Keeping them outside `<text>` prevents a page header from
interrupting a word dehyphenated across pages. A physical line that reaches no
structural rule is appended normally when a text container is already open. If
it would otherwise land directly under `<u>` or `<div>`, the config creates a
low-confidence `<seg type="unparsed">` or `<p type="unparsed">` and adds a `doc2tei: unmatched
source line` XML comment for manual review.

```bash
python parse.py testdocs/<document>.pdf \
  --config examples/general-config/config.py -o out.xml
```

On the 1957 volume it matches the dedicated config almost exactly; on ZRIP it
is within a few percent; on prosvetno it trades some precision for recall (it
finds ~40 real speakers the dedicated config missed, but also marks appendix
datelines as `<time>` and stage directions as `<note>`). Expect _some_ error
on any new document - measure with `--diagnostics` and tighten the tests when
a document family deserves its own config.

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
samples, recovery counts, page counts, and font/size distributions. For the full low-level log,
set `"debug": True` in the config and pass `--debug-file debug.txt`; that file
can be extremely large.

## Running it

```bash
# 1. Convert with an explicit config and audit outputs
python parse.py path/to/input.pdf --config path/to/config.py -o out/out.xml \
  --diagnostics out/diagnostics.json --data-output out/data.json \
  --list-person-output out/listPerson.xml

# Optional: enrich listPerson with fail-soft Wikidata matches in the same run
python parse.py path/to/input.pdf --config path/to/config.py -o out/out.xml \
  --list-person-output out/listPerson.xml --include-wikidata
```

`--list-person-output` always recovers organization and role affiliations from
speaker labels. Wikidata access is disabled by default so ordinary runs are
offline and deterministic. With `--include-wikidata`, timeouts, throttling,
unavailable records, and low-confidence matches all fall back to the local
record rather than failing the parse. `make_list_person.py` remains available
as a compatibility wrapper for an already-exported JSON speaker map.

To convert a _different_ document, write a new config module exporting `CONFIG`,
`COSMETIC_ANNOTATIONS` and `get_chunks`, then select it with `--config`.
There is no need to copy it to the repository root or maintain a symlink.

Dependencies: `pdfminer.six` (PDF extraction), `python-docx` (Word mode),
`pypdf` (the coord-dump tool, should be reworked into pdfminer), `requests`
(optional Wikidata enrichment).

### Batch conversion

For collections, use the dedicated runner instead of starting many independent
`parse.py` commands:

```bash
python batch_parse.py path/to/documents --output-dir out/batch
```

It recursively discovers PDF and DOCX inputs, preserves their relative
directory layout, writes one output bundle per document, and maintains
`batch-manifest.json`. The default config is
`examples/general-config/config.py`; select another with `--config`.

`--workers 0` (the default) uses up to four document processes. Multiple
document workers automatically imply `page_workers=1`, avoiding nested process
pools. Use `--workers 1` to process documents sequentially while retaining the
config's page-level parallelism. Workers are normally recycled after each file
to release PDF parser memory; `--reuse-workers` is faster for many tiny files.

The runner is resumable. A completed `status.json` records a fingerprint of the
source, config, parser implementation, and output options, so unchanged
documents are skipped and changed documents or code are rebuilt. `--overwrite`
disables this check.

Hard per-document failures are converted into reviewable fallback bundles and
reported as `recovered`; they do not stop other documents or make the batch
command fail. A `failed` status is reserved for cases where output files cannot
be written. Intentional best-effort outcomes that merely need human review are
reported as warnings in `data.json`, the TEI header, each `status.json`, and the
batch manifest; they do not change a document's `ok` status. See
`python batch_parse.py --help` for filtering, Wikidata, XML, and concurrency
options.

The batch runner can also acquire its inputs from the bundled `sistory-dl`
submodule before conversion:

```bash
python batch_parse.py --sistory-menu 1/7/397/407 --output-dir out/sistory
```

Bare menu paths and full SIstory menu URLs are accepted. Repeat
`--sistory-menu` to crawl multiple roots. Each root receives a stable cache
below `OUTPUT_DIR/_sistory-downloads`; the downloader's existing-file skip and
the parser's output fingerprints make the complete operation resumable.
`--sistory-download-dir` moves that cache elsewhere.

SIstory acquisition results are embedded in `batch-manifest.json`. Files
already downloaded remain eligible for parsing even when a later menu refresh
partially fails. Such a run finishes as `incomplete` and returns a nonzero exit
status, while all available documents continue through the batch. Run
`git submodule update --init sistory-dl` if the checkout is missing.
