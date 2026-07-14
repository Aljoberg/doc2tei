"""Config for Seje_1.4.1957-30.9.1957-primer.pdf.

The debate transcript starts on PDF page 5.  PDF page 115 starts the
appendices ("PRILOGE"), so this extractor deliberately yields pages 5-114.

Unlike the ZRIP sample, this PDF contains real space glyphs and exposes no
useful bold/italic font variants.  pdfplumber's word extraction is therefore a
better fit than splitting the raw character stream at literal spaces.
"""

from __future__ import annotations

from dataclasses import replace
import functools
import re
import xml.etree.ElementTree as ET
from typing import Any

from doc2tei.extractors import LineRecord, PageRange, WordPDFExtractor

from engine import (
    PDFChunk,
    StackEntry,
    append,
    pop_and_push_to,
    pop_to,
    push,
)
from type_decs import PDFConfig, PDFCosmeticAnnotations


FIRST_TRANSCRIPT_PAGE = 4  # zero-based: PDF page 5
APPENDICES_PAGE = 114  # zero-based: PDF page 115


@functools.wraps(print)
def log(*args, **kwargs):
    if CONFIG["debug"]:
        print(*args, **kwargs)


def line_text(chunk: PDFChunk) -> str:
    return re.sub(r"\s+", " ", chunk.line_chunk.text).strip()


def is_line_start(chunk: PDFChunk) -> bool:
    return chunk is chunk.line_chunk.runs[0]


def is_session_page(chunk: PDFChunk) -> bool:
    context = chunk.page_context
    return bool(context and context.metadata.get("session_page", False))


def is_front_matter(chunk: PDFChunk) -> bool:
    return bool(chunk.line_chunk.metadata.get("front_matter", False))


def page_left(chunk: PDFChunk) -> float:
    context = chunk.page_context
    return float(context.metadata.get("page_left", chunk.x) if context else chunk.x)


def is_body_line(chunk: PDFChunk) -> bool:
    return 8.8 <= chunk.font_size <= 10.6 and 34 < chunk.y < 590


def is_paragraph_start(chunk: PDFChunk) -> bool:
    return (
        is_line_start(chunk)
        and is_body_line(chunk)
        and not is_front_matter(chunk)
        and chunk.x >= page_left(chunk) + 14
    )


def session_heading(chunk: PDFChunk) -> bool:
    if not is_line_start(chunk) or not is_session_page(chunk):
        return False
    text = line_text(chunk)
    return chunk.font_size >= 11.5


def session_date(chunk: PDFChunk) -> bool:
    return (
        is_line_start(chunk)
        and is_session_page(chunk)
        and bool(
            re.fullmatch(
                r"\(\s*\d{1,2}\.\s+[^()]*?19\d{2}\.?\s*\)", line_text(chunk)
            )
        )
    )


def _word_core(token: str) -> str:
    return re.sub(r"[^0-9A-Za-zČŠŽĆĐčšžćđ]", "", token)


def _looks_like_person_prefix(prefix: str) -> bool:
    """Distinguish a speaker label from ordinary prose ending in a colon."""
    if re.match(
        r"^(?:predsednik|podpredsednik|predsedujoči|dr\.?|d\s+r\.?|inž\.?)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True

    tokens = [_word_core(token) for token in prefix.split()]
    tokens = [token for token in tokens if token]
    if not 2 <= len(tokens) <= 14 or not tokens[0][0].isupper():
        return False

    # OCR often spaces a surname ("P i r n a t") or splits it ("Me lik").
    # Person labels still consist only of capitalized/very short fragments;
    # normal prose soon contains a longer lowercase word.
    return all(
        token[0].isupper() or len(token) == 1 or (token.islower() and len(token) <= 3)
        for token in tokens[1:]
    )


def speaker_parts(chunk: PDFChunk) -> tuple[str, str] | None:
    if (
        not is_line_start(chunk)
        or not is_body_line(chunk)
        or is_front_matter(chunk)
    ):
        return None
    match = re.match(r"^([^:]{2,90}:)(?:\s*(.*))?$", line_text(chunk))
    if not match or not _looks_like_person_prefix(match.group(1)[:-1].strip()):
        return None
    return match.group(1).strip(), (match.group(2) or "").strip()


def is_speaker(chunk: PDFChunk) -> bool:
    return speaker_parts(chunk) is not None


def _append_text(
    chunk: PDFChunk, text: str, *, space_before: bool | None = None
) -> None:
    if not text:
        return
    copy = replace(
        chunk,
        text=text,
        space_before=chunk.space_before if space_before is None else space_before,
    )
    append(copy, should_annotate=[])


def speaker_append(chunk: PDFChunk) -> None:
    """Emit a speaker note and the speech sharing the same printed line."""
    parts = speaker_parts(chunk)
    assert parts is not None
    speaker, speech = parts

    pop_to("div")
    push("note", type="speaker")
    _append_text(chunk, speaker, space_before=False)

    # Closing the note invokes speaker_to_utterance(), which opens <u>.
    pop_to("note", invert=True)
    if speech:
        pop_to("u", "div")
        push("seg")
        _append_text(chunk, speech, space_before=False)


def _collapse_spaced_letters(text: str) -> str:
    tokens = text.split()
    result: list[str] = []
    i = 0
    while i < len(tokens):
        run: list[str] = []
        j = i
        while j < len(tokens) and len(_word_core(tokens[j])) == 1:
            core = _word_core(tokens[j])
            # Keep an OCR-spaced lowercase title ("d r.", "i n ž.")
            # separate from the following capitalized given name.
            if run and core[0].isupper() and (
                run[0][0].islower()
                or "".join(run).lower() in {"dr", "inž"}
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


utterance_speaker_mapping: dict[str, str] = {}


def speaker_to_utterance(popped: StackEntry) -> None:
    if popped.element.tag != "note" or popped.element.attrib.get("type") != "speaker":
        return

    text = "".join(popped.element.itertext()).strip()
    name = text.rsplit(":", 1)[0]
    name = _collapse_spaced_letters(name)
    name = re.sub(
        r"^(?:(?:predsednik|podpredsednik|predsedujoči)\s+)+",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"^(?:(?:dr|inž)\.?\s+)+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    serialized = "#" + "".join(ch for ch in name.title() if ch.isalnum())
    if serialized == "#":
        serialized = "#UnknownSpeaker"

    utterance_speaker_mapping.setdefault(serialized, text)
    push("u", who=serialized)


def on_end(result: Any) -> None:
    result.data["speakers"] = dict(utterance_speaker_mapping)


# The OCR layer exposes every line as Times-Roman, including visibly bold text.
# Adding bold/italic cosmetics from the font metadata would therefore be wrong.
COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {}


CONFIG: PDFConfig = {
    "debug": False,
    "mode": "pdf",
    "on_pop": speaker_to_utterance,
    "on_end": on_end,
    "alignments": {
        "any": {
            # Session opening page.
            "SESSION_HEADING": {
                "test": session_heading,
                "action": pop_and_push_to("div", tag="head", type="session"),
            },
            "SESSION_DATE": {
                "test": session_date,
                "action": pop_and_push_to("div", tag="time", type="date"),
            },
            "CHAIRMAN": {
                "test": lambda chunk: is_line_start(chunk)
                and is_session_page(chunk)
                and line_text(chunk).lower().startswith("predsedoval"),
                "action": pop_and_push_to("div", tag="note", type="chairman"),
            },
            "SCRIBE": {
                "test": lambda chunk: is_line_start(chunk)
                and is_session_page(chunk)
                and bool(re.match(r"^(?:Zapisnikar|Tajnik)\s*:", line_text(chunk))),
                "action": pop_and_push_to("div", tag="note", type="scribe"),
            },
            "START_TIME": {
                "test": lambda chunk: is_line_start(chunk)
                and is_session_page(chunk)
                and bool(
                    re.match(
                        r"^(?:Začetek|Pričetek|Kačctek)\s+seje\b",
                        line_text(chunk),
                        re.IGNORECASE,
                    )
                ),
                "action": pop_and_push_to("div", tag="time", type="start"),
            },
            # Debate body.  Order matters: stage directions and speakers are
            # paragraph starts too, so they must precede the generic SEG rule.
            "GENERIC_NOTE": {
                "test": lambda chunk: is_paragraph_start(chunk)
                and (
                    line_text(chunk).startswith("(")
                    or re.match(r"^Seja\s+(?:je\s+)?bila\b", line_text(chunk))
                ),
                "action": pop_and_push_to("div", tag="note", chunked=False),
            },
            "SPEAKER": {
                "test": is_speaker,
                "append_func": speaker_append,
            },
            "SEG": {
                "test": is_paragraph_start,
                "action": pop_and_push_to("u", "div", tag="seg", chunked=False),
            },
        }
    },
}


def line_filter(record: LineRecord, _page: Any) -> bool:
    return not (record.y > 590 and record.font_size <= 8.2)


def enrich_page(page: Any, records: list[LineRecord]) -> None:
    body_x = [
        record.x
        for record in records
        if 8.8 <= record.font_size <= 10.6 and 34 < record.y < 590
    ]
    page.metadata["page_left"] = min(body_x) if body_x else 0.0
    page.metadata["session_page"] = any(
        record.font_size >= 11.5
        and re.fullmatch(r"\d+\.\s*seja", record.text, re.IGNORECASE)
        for record in records
    )
    page.metadata["start_time_index"] = next(
        (
            index
            for index, record in enumerate(records)
            if re.match(
                r"^(?:Začetek|Pričetek|Kačctek)\s+seje\b",
                record.text,
                re.IGNORECASE,
            )
        ),
        None,
    )


def enrich_line(
    page: Any,
    _record: LineRecord,
    index: int,
    _records: list[LineRecord],
) -> dict[str, Any]:
    start_time_index = page.metadata.get("start_time_index")
    return {
        "front_matter": bool(
            page.metadata.get("session_page")
            and start_time_index is not None
            and index <= start_time_index
        )
    }


get_chunks = WordPDFExtractor(
    pages=PageRange(FIRST_TRANSCRIPT_PAGE, APPENDICES_PAGE),
    x_tolerance=1.7,
    y_tolerance=3.0,
    line_tolerance=3.2,
    word_gap=0.6,
    join_line_end_hyphens=True,
    line_filter=line_filter,
    page_enricher=enrich_page,
    line_enricher=enrich_line,
)
