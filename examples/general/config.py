"""General config for Yugoslav-era parliamentary stenographic PDFs.

Unlike the per-document configs, this one probes the PDF first and adapts:

- **extraction profile** - character stream with the space-as-break quirk
  (ZRIP-like, almost no literal space glyphs), character stream with real
  spaces (prosvetno-like), or pdfplumber word reconstruction when an
  invisible OCR text layer is detected (seje-1957-like);
- **body font band** - the dominant character size, measured per document;
- **columns** - detected per page by clustering line-start x positions, so
  indentation tests are column-relative instead of magic x ranges;
- **running headers** - dropped by geometry + font size + a
  "N. seja/sednica" pattern instead of per-document y bands;
- **front/back matter** - pages before the first session heading are
  skipped, and a big standalone "PRILOGE" heading stops the parse.

Rules are the union of the three per-document configs, rewritten against
the probed metadata. Expect a bit of error on any individual document -
this trades per-document precision for generality.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from typing import Iterator, cast

from doc2tei.extractors import (
    CharacterPDFExtractor,
    LineRecord,
    WordPDFExtractor,
)
from doc2tei.helpers import SpeakerUtteranceHook
from engine import (
    Chunk,
    PDFChunk,
    PDFPageContext,
    append,
    pop_and_push_to,
    pop_to,
    push,
    tag,
    tag_is_on_top,
)
from type_decs import PDFConfig, PDFCosmeticAnnotations


@functools.wraps(print)
def log(*args, **kwargs):
    if CONFIG["debug"]:
        print(*args, **kwargs)


# ---------------------------------------------------------------------------
# document profile, filled by get_chunks() before parsing starts
# ---------------------------------------------------------------------------

PROFILE: dict[str, object] = {}
_STATE: dict[str, bool] = {"seen_session": False}


def body_size() -> float:
    return float(cast(float, PROFILE.get("body_size", 10.0)))


def is_styled() -> bool:
    return PROFILE.get("mode") != "ocr"


def is_body_size(size: float) -> bool:
    return abs(size - body_size()) <= 1.6


def _probe(filename: str, sample_pages: int = 10) -> dict[str, object]:
    """Cheap pdfminer pass over the first pages to pick an extraction profile."""
    from pdfminer.layout import LTChar
    from pdfminer.high_level import extract_pages
    import itertools

    fonts: Counter[str] = Counter()
    sizes: Counter[float] = Counter()
    spaces = 0
    chars = 0
    for page in itertools.islice(extract_pages(filename), sample_pages):

        def walk(obj):
            nonlocal spaces, chars
            for item in obj:
                if isinstance(item, LTChar):
                    chars += 1
                    if item.get_text() == " ":
                        spaces += 1
                    else:
                        fonts[str(item.fontname)] += 1
                        sizes[round(float(item.size) * 2) / 2] += 1
                elif hasattr(item, "__iter__"):
                    walk(item)

        walk(page)

    space_ratio = spaces / max(chars, 1)
    ocr = any("invisible" in name.lower() or "ocr" in name.lower() for name in fonts)
    if ocr:
        mode = "ocr"
    elif space_ratio < 0.05:
        mode = "char-break"
    else:
        mode = "char-preserve"
    profile = {
        "mode": mode,
        "body_size": float(sizes.most_common(1)[0][0]) if sizes else 10.0,
        "space_ratio": space_ratio,
    }
    log(f"probed profile: {profile}")
    return profile


# ---------------------------------------------------------------------------
# shared enrichment: columns, session pages, running headers, back matter
# ---------------------------------------------------------------------------

SESSION_NUM_RE = re.compile(
    r"\d+\.\s*(?:izredna\s+|redna\s+|zajedni\S+\s+)?(?:sej[aeio]|sednic[aeio])\b",
    re.IGNORECASE,
)
SESSION_CAPS_RE = re.compile(r"\b(?:SEJ[AEO]\w*|SEDNIC\w*)\b")


def _is_session_marker(text: str) -> bool:
    text = text.strip()
    if SESSION_NUM_RE.search(text):
        return True
    return text.isupper() and bool(SESSION_CAPS_RE.search(text))


def enrich_page(page: PDFPageContext, records: list[LineRecord]) -> None:
    body_x = sorted(
        record.x
        for record in records
        if is_body_size(record.font_size) and record.y < page.height * 0.94
    )
    xs = body_x if body_x else sorted(record.x for record in records)

    def flush_edge(cluster: list[float]) -> float:
        # the column's flush-left edge: the smallest x that several other
        # lines share (a lone stray line must not become the edge)
        need = max(3, len(cluster) // 20)
        for i, value in enumerate(cluster):
            support = sum(1 for other in cluster[i:] if other - value <= 3.0)
            if support >= need:
                return value
        return cluster[0]

    # cluster line-start x positions into columns; a gap wider than ~18% of
    # the page width separates two columns
    columns: list[float] = []
    cluster: list[float] = []
    for x in xs:
        if cluster and x - cluster[-1] > page.width * 0.18:
            columns.append(flush_edge(cluster))
            cluster = []
        cluster.append(x)
    if cluster:
        columns.append(flush_edge(cluster))
    page.metadata["columns"] = columns

    if any(_is_session_marker(record.text) for record in records):
        _STATE["seen_session"] = True
    page.metadata["in_transcript"] = _STATE["seen_session"]
    page.metadata["toc_page"] = any(
        CONTENTS_RE.match(record.text.strip()) for record in records
    )


def enrich_line(
    page: PDFPageContext,
    record: LineRecord,
    _index: int,
    _records: list[LineRecord],
) -> dict[str, object]:
    columns = page.metadata.get("columns") or [record.x]
    assert isinstance(columns, list)
    col_left = record.x
    for left in columns:
        if left <= record.x + 2.0:
            col_left = left
    return {"indent": record.x - col_left}


HEADER_PATTERN_RE = re.compile(r"\d+\.?\s*(?:sej[aeio]|sednic[aeio])", re.IGNORECASE)


def line_filter(record: LineRecord, page: PDFPageContext) -> bool:
    # skip whole pages until the first session heading is seen (title pages,
    # tables of contents in front matter)
    if not page.metadata.get("in_transcript", False):
        return False
    # running page headers: top ~8.5% of the page, and either notably smaller
    # than the body or a "N. seja/sednica" colontitle
    if record.y > page.height * 0.915:
        text = record.text.strip()
        if record.font_size <= body_size() - 1.0 or len(text) <= 4:
            return False
        if HEADER_PATTERN_RE.search(text):
            return False
    return True


PRILOGE_RE = re.compile(r"^PRILOG[EIA]?\b")


def stop_before(record: LineRecord, page: PDFPageContext) -> bool:
    # appendix volume: a big standalone PRILOGE heading ends the transcript
    return bool(
        page.metadata.get("in_transcript", False)
        and record.font_size >= body_size() + 1.5
        and bool(PRILOGE_RE.match(record.text.strip()))
        and len(record.text.strip()) < 30
    )


# ---------------------------------------------------------------------------
# chunk-level helpers
# ---------------------------------------------------------------------------


def line_text(chunk: PDFChunk) -> str:
    return re.sub(r"\s+", " ", chunk.line_chunk.text).strip()


def indent(chunk: PDFChunk) -> float:
    value = chunk.line_chunk.metadata.get("indent", 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def line_has_bold(chunk: PDFChunk) -> bool:
    return any(run.bold for run in chunk.line_chunk.runs)


def is_body_line(chunk: PDFChunk) -> bool:
    return is_body_size(chunk.font_size)


# ---------------------------------------------------------------------------
# speaker detection (union of the three configs)
# ---------------------------------------------------------------------------

TITLE_PREFIX_RE = re.compile(
    r"^(?:pred?sedni[kc]\w*|podpredsedni\w*|potpredsedni\w*|predsedujo\S+|"
    r"predsedava\w*|dr\.?|d\s+r\.?|in[žz]\.?|ing\.?|mr\.?)\b",
    re.IGNORECASE,
)


def _word_core(token: str) -> str:
    return re.sub(r"[^0-9A-Za-zČŠŽĆĐčšžćđ]", "", token)


def _looks_like_person_prefix(prefix: str) -> bool:
    """Distinguish a speaker label from ordinary prose ending in a colon."""
    prefix = re.sub(r"\s*\([^)]*\)\s*$", "", prefix).strip()
    # "VESELIN DJURANOVIĆ, predsednik ZIS" - the role after the comma is
    # ordinary lowercase prose; judge the name part only
    prefix = prefix.split(",", 1)[0].strip()
    # a bare "PREDSEDAVALI:"/"Predsedoval:" is a role announcement, not a
    # person - a title only counts when a name follows it
    if TITLE_PREFIX_RE.match(prefix) and len(prefix.split()) >= 2:
        return True
    tokens = [_word_core(token) for token in prefix.split()]
    tokens = [token for token in tokens if token]
    if not 2 <= len(tokens) <= 14 or not tokens[0][:1].isupper():
        return False
    # OCR often spaces a surname ("P i r n a t") or splits it ("Me lik");
    # person labels consist of capitalized / very short fragments only
    return all(
        token[:1].isupper() or len(token) == 1 or (token.islower() and len(token) <= 3)
        for token in tokens[1:]
    )


def leading_caps(text: str) -> int:
    count = 0
    for character in text:
        if character.islower():
            break
        if character.isupper():
            count += 1
    return count


def speaker_parts(chunk: PDFChunk) -> tuple[str, str] | None:
    if not chunk.is_line_start or not is_body_line(chunk):
        return None
    match = re.match(r"^([^:]{2,90}:)(?:\s*(.*))?$", line_text(chunk))
    if not match:
        return None
    prefix = match.group(1)[:-1].strip()
    if not _looks_like_person_prefix(prefix):
        return None
    if is_styled():
        # with a usable font layer, require extra evidence so prose lines
        # ending in a colon don't become speakers
        upper_prefix = sum(1 for ch in prefix if ch.isupper())
        if not (
            line_has_bold(chunk) or upper_prefix >= 5 or TITLE_PREFIX_RE.match(prefix)
        ):
            return None
    return match.group(1).strip(), (match.group(2) or "").strip()


def _flush_caps_name(text: str) -> bool:
    """An all-caps name followed by a role/party: "VESELIN DJURANOVIĆ, ..."."""
    match = re.match(r"^([^,(:]{5,40})\s*[,(:]", text)
    if not match:
        return False
    name = match.group(1).strip()
    tokens = name.split()
    if not name.isupper():
        return False
    # space-as-break extraction can fuse the name into one token
    # ("BERISLAVŠEFER(SR Hrvatska):"), so accept a single long caps token too
    return 2 <= len(tokens) <= 4 or (len(tokens) == 1 and len(name) >= 10)


def is_speaker(chunk: PDFChunk) -> bool:
    if speaker_parts(chunk) is not None:
        return True
    # ZRIP-style speaker whose role wraps over several lines (no colon on the
    # first line): an all-caps name at the column's flush-left edge
    return (
        is_styled()
        and chunk.is_line_start
        and is_body_line(chunk)
        and indent(chunk) < 6.0
        and leading_caps(line_text(chunk)) >= 5
        and _flush_caps_name(line_text(chunk))
        and not is_chairman(chunk)
    )


def _append_text(chunk: PDFChunk, text: str) -> None:
    if not text:
        return
    from dataclasses import replace

    append(replace(chunk, text=text, space_before=False), should_annotate=[])


def speaker_append_split(chunk: PDFChunk) -> None:
    """OCR lines carry the whole line in one chunk: split label from speech."""
    parts = speaker_parts(chunk)
    assert parts is not None
    label, speech = parts
    pop_to("div")
    push("note", type="speaker")
    _append_text(chunk, label)
    pop_to("note", invert=True)  # closing the note opens <u> via the hook
    if speech:
        pop_to("u", "div")
        push("seg")
        _append_text(chunk, speech)


def speaker_action(chunk: PDFChunk) -> None:
    if PROFILE.get("mode") == "ocr":
        speaker_append_split(chunk)
    else:
        # character modes: the rest of the line arrives as further chunks and
        # flows into the open note by itself
        if not tag_is_on_top("note", type="speaker"):
            pop_to("div")
            push("note", type="speaker")
        append(chunk, should_annotate=[])


def _collapse_spaced_letters(text: str) -> str:
    tokens = text.split()
    result: list[str] = []
    i = 0
    while i < len(tokens):
        run: list[str] = []
        j = i
        while j < len(tokens) and len(_word_core(tokens[j])) == 1:
            core = _word_core(tokens[j])
            if (
                run
                and core[0].isupper()
                and (run[0][0].islower() or "".join(run).lower() in {"dr", "inž"})
            ):
                break
            run.append(core)
            j += 1
        if len(run) >= 2:
            result.append("".join(run))
            i = j
        else:
            result.append(tokens[i])
            i += 1
    return " ".join(result)


def speaker_identifier(text: str) -> str:
    name = text.rsplit(":", 1)[0]
    name = _collapse_spaced_letters(name)
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name)
    while True:
        stripped = TITLE_PREFIX_RE.sub("", name.strip())
        stripped = re.sub(
            r"^(?:zbora|ve[čć]a|skup\S+)\s+", "", stripped.strip(), flags=re.IGNORECASE
        )
        if stripped == name.strip():
            break
        name = stripped
    serialized = "#" + "".join(ch for ch in name.title() if ch.isalnum())
    return serialized if serialized != "#" else "#UnknownSpeaker"


speaker_hook = SpeakerUtteranceHook(speaker_identifier)


# ---------------------------------------------------------------------------
# other rule tests
# ---------------------------------------------------------------------------

CHAIRMAN_RE = re.compile(r"^(?:PREDSEDUJE|PREDSEDAVA\w*|Predsedoval\w*)", re.IGNORECASE)
SCRIBE_RE = re.compile(r"^(?:Zapisnikar|Tajnik)\w*\s*:", re.IGNORECASE)
CONTENTS_RE = re.compile(r"^(?:SADR[ŽZ]AJ|VSEBINA)\b", re.IGNORECASE)
DATE_LINE_RE = re.compile(
    r"^\(?\s*(?:od\s+)?\d{1,2}\.\s*\S+\s+(?:19|20)\d\d", re.IGNORECASE
)
# tolerate OCR garble of Začetek/Pričetek ("Kačctek")
START_TIME_RE = re.compile(r"^(?:\w{1,3}[čc]\w{0,2}tek|Po[čc]etak)\b", re.IGNORECASE)
STANDALONE_DATE_RE = re.compile(
    r"^\(?\s*(?:od\s+)?\d{1,2}\.\s*\S+\s+(?:i\s+\d{1,2}\.\s*\S+\s+)?"
    r"(?:19|20)\d\d\.?(?:\s*godine)?\s*\)?\.?$",
    re.IGNORECASE,
)
SEJA_BILA_RE = re.compile(r"^Seja\s+(?:je\s+)?bila\b", re.IGNORECASE)
HEAD_KEYWORD_RE = re.compile(r"\b(?:SEJ[AEO]\w*|SEDNIC\w*|NADALJEVANJE)\b")


def is_head(chunk: PDFChunk) -> bool:
    if not chunk.is_line_start:
        return False
    text = line_text(chunk)
    if not text or len(text) > 70 or text.endswith(":"):
        return False
    # clearly-larger line; the caps/session gate keeps garbled footnote and
    # scan-noise body lines out (session heads may sit flush left)
    if chunk.font_size >= body_size() + 1.8 and (
        text.isupper() or SESSION_NUM_RE.search(text)
    ):
        return True
    # body-sized headings (ZRIP): full caps + bold + a session keyword
    return (
        text.isupper()
        and line_has_bold(chunk)
        and bool(HEAD_KEYWORD_RE.search(text))
        and not is_speaker(chunk)
    )


def head_action(chunk: PDFChunk) -> None:
    text = line_text(chunk)
    kind = (
        "sessionNumber"
        if re.match(r"^\W{0,3}\d+\.", text) and SESSION_NUM_RE.search(text)
        else "session"
    )
    if not tag_is_on_top("head", type=kind):
        pop_to("div")
        push("head", type=kind)


def is_time(chunk: PDFChunk) -> bool:
    if not chunk.is_line_start:
        return False
    text = line_text(chunk)
    if START_TIME_RE.match(text):
        return True
    # a line that is nothing but a date ("OD 15. MAJA 1964. GODINE",
    # "(23. aprila 1957)") is a <time> wherever it sits
    if len(text) <= 48 and STANDALONE_DATE_RE.match(text):
        return True
    if len(text) > 48 or not DATE_LINE_RE.match(text):
        return False
    # a date line is a <time>, but "6. SEDNICA OD 15. MAJA ..." is a heading
    if SESSION_NUM_RE.search(text[:24]):
        return False
    # dates are centered or parenthesized; a flush "20. maja 1964..." line
    # is ordinary prose
    return indent(chunk) > 15 or text.startswith("(")


def time_action(chunk: PDFChunk) -> None:
    kind = "start" if START_TIME_RE.match(line_text(chunk)) else "date"
    if not tag_is_on_top("time", type=kind):
        pop_to("div")
        push("time", type=kind)


def is_chairman(chunk: PDFChunk) -> bool:
    if not chunk.is_line_start or not CHAIRMAN_RE.match(line_text(chunk)):
        return False
    # "Predsedavajući Vlado Malašič:" introduces a speaker, not the session
    # chairman note
    return speaker_parts(chunk) is None


def is_footnote_entry(chunk: PDFChunk) -> bool:
    return (
        chunk.is_line_start
        and chunk.font_size <= body_size() - 2.0
        and chunk.text.strip().isdigit()
    )


def footnote_action(chunk: PDFChunk) -> None:
    serialized = re.sub(r"[^a-zA-Z0-9]", "", chunk.text.strip())
    if not tag_is_on_top("note", place="foot", n=serialized):
        pop_to("u", "div")
        push(
            "note",
            attribs={"xml:id": f"#note{serialized}"},
            place="foot",
            n=serialized,
        )


def is_generic_note(chunk: PDFChunk) -> bool:
    if not chunk.is_line_start:
        return False
    text = line_text(chunk)
    if not text:
        return False
    if is_body_line(chunk) and 6 <= indent(chunk) and text.startswith("("):
        return True
    if SEJA_BILA_RE.match(text):
        return True
    # centered whole-italic line (stage directions in the styled documents)
    return bool(
        is_styled()
        and chunk.line_chunk.italic
        and indent(chunk) > 20
        and not is_time(chunk)
    )


def generic_note_action(chunk: PDFChunk) -> None:
    pop_to("u", "div")
    push("note")


def is_contents(chunk: PDFChunk) -> bool:
    if not chunk.is_line_start:
        return bool(tag_is_on_top("note", type="contents"))
    if CONTENTS_RE.match(line_text(chunk)):
        return True
    # table-of-contents entries are set smaller than the body; on a page
    # with a SADRŽAJ/VSEBINA heading, capture all of them even if another
    # rule interrupted the note
    if chunk.font_size > body_size() - 1.0:
        return False
    context = chunk.page_context
    return bool(
        tag_is_on_top("note", type="contents")
        or (context is not None and context.metadata.get("toc_page"))
    )


def contents_action(chunk: PDFChunk) -> None:
    if not tag_is_on_top("note", type="contents"):
        pop_to("div")
        push("note", type="contents")


def is_seg(chunk: PDFChunk) -> bool:
    return chunk.is_line_start and is_body_line(chunk) and 6.0 <= indent(chunk) <= 45.0


# ---------------------------------------------------------------------------
# cosmetics (inert in OCR mode, where the font layer is meaningless)
# ---------------------------------------------------------------------------

COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {
    "ITALIC": {
        "test": lambda chunk: is_styled() and bool(chunk.italic),
        "tag": tag("emph"),
    },
    "BOLD": {
        "test": lambda chunk: is_styled() and bool(chunk.bold),
        "tag": tag("hi", rend="bold"),
    },
    "REFERENCE": {
        "test": lambda chunk: (
            is_styled()
            and chunk.font_size <= body_size() - 2.0
            and chunk.text.strip().isdigit()
        ),
        "tag": tag("ref"),
        "append_func": lambda chunk: push(
            "ref",
            cosmetic=True,
            target=f'#note{re.sub(r"[^a-zA-Z0-9]", "", chunk.text.strip())}',
        ),
    },
}


CONFIG: PDFConfig = {
    "debug": False,
    "mode": "pdf",
    "on_start": speaker_hook.reset,
    "on_pop": speaker_hook,
    "on_end": speaker_hook.export,
    "auto_xml_ids": True,
    "rules": {
        "CONTENTS": {
            "test": is_contents,
            "action": contents_action,
            "append_func": lambda chunk: append(chunk, should_annotate=[]),
        },
        "CHAIRMAN": {
            "test": is_chairman,
            "action": pop_and_push_to("div", tag="note", type="chairman"),
        },
        "SCRIBE": {
            "test": lambda chunk: chunk.is_line_start
            and bool(SCRIBE_RE.match(line_text(chunk))),
            "action": pop_and_push_to("div", tag="note", type="scribe"),
        },
        "TIME": {
            "test": is_time,
            "action": time_action,
        },
        "HEAD": {
            "test": is_head,
            "action": head_action,
        },
        "FOOTNOTE_ENTRY": {
            "test": is_footnote_entry,
            "action": footnote_action,
            "append_func": lambda chunk: None,
        },
        "GENERIC_NOTE": {
            "test": is_generic_note,
            "action": generic_note_action,
            "append_func": lambda chunk: append(chunk, should_annotate=["REFERENCE"]),
        },
        "SPEAKER": {
            "test": is_speaker,
            "append_func": speaker_action,
        },
        "SEG": {
            "test": is_seg,
            "action": pop_and_push_to("u", "div", tag="seg", chunked=False),
        },
    },
}


# ---------------------------------------------------------------------------
# extraction dispatch
# ---------------------------------------------------------------------------


def _asymmetric_line_break(previous_y: float, current_y: float) -> bool:
    tolerance = 4.871
    return (
        previous_y - current_y > tolerance
        or abs(previous_y - current_y) > tolerance * 5
    )


def get_chunks(filename: str) -> Iterator[Chunk]:
    PROFILE.clear()
    PROFILE.update(_probe(filename))
    _STATE["seen_session"] = False

    common = dict(
        line_filter=line_filter,
        stop_before=stop_before,
        page_enricher=enrich_page,
        line_enricher=enrich_line,
    )
    mode = PROFILE["mode"]
    if mode == "ocr":
        extractor = WordPDFExtractor(
            x_tolerance=1.7,
            y_tolerance=3.0,
            line_tolerance=3.2,
            word_gap=0.6,
            join_line_end_hyphens=True,
            **common,
        )
    elif mode == "char-break":
        extractor = CharacterPDFExtractor(
            line_break=_asymmetric_line_break,
            literal_spaces="break",
            gap_threshold=1.7,
            max_run_x_gap=30,
            **common,
        )
    else:
        extractor = CharacterPDFExtractor(
            line_tolerance=4.0,
            literal_spaces="preserve",
            **common,
        )
    yield from extractor(filename)
