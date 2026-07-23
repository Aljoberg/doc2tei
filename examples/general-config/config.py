"""General config for Yugoslav-era parliamentary stenographic PDFs.

Unlike the per-document configs, this one probes the PDF first and adapts:

- **extraction profile** - character stream with the space-as-break quirk
  (ZRIP-like, almost no literal space glyphs), character stream with real
  spaces (prosvetno-like), or pdfplumber word reconstruction when an
  invisible OCR text layer is detected (seje-1957-like);
- **body font band** - the dominant character size, measured per document;
- **columns** - detected per page by clustering line-start x positions, so
  indentation tests are column-relative instead of magic x ranges;
- **running headers** - detected by geometry + font size + a
  "N. seja/sednica" pattern instead of per-document y bands;
- **front/back matter** - retained as ordinary TEI blocks unless it can be
  identified safely; source furniture and speaker indexes are retained in
  reviewable header ``note[type=sourceArtifact]`` elements instead of discarded.

Rules are the union of the three per-document configs, rewritten against
the probed metadata. Expect a bit of error on any individual document -
this trades per-document precision for generality.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
import xml.etree.ElementTree as ET
from bisect import bisect_right
from collections import Counter
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator, cast

import engine
from doc2tei import FootnoteLinker, SpeakerUtteranceHook
from doc2tei.extractors import (
    CharacterPDFExtractor,
    LineRecord,
    WordPDFExtractor,
)
from doc2tei.tei_header import Change, SourceBibl, TEIHeader
from engine import (
    PDFChunk,
    PDFPageContext,
    append,
    append_comment,
    pop_and_push_to,
    pop_to,
    push,
    sanitize_xml_id,
    tag,
    tag_is_on_top,
)
from type_decs import Chunk, PDFConfig, PDFCosmeticAnnotations


def log(*args, **kwargs):
    if CONFIG["debug"]:
        print(*args, **kwargs)


# ---------------------------------------------------------------------------
# document profile, filled by get_chunks() before parsing starts
# ---------------------------------------------------------------------------

DEFAULT_BODY_SIZE = 10.0
LINE_BREAK_TOLERANCE = 4.871
COLUMN_PROBE_Y_MAX = 0.94
RUNNING_HEADER_Y = 0.915
RUNNING_FOOTER_Y = 0.085

PROFILE: dict[str, Any] = {}


def _new_state() -> dict[str, Any]:
    return {
        "seen_meeting": False,
        "seen_session": False,
        "speaker_index": False,
        "back_matter": False,
        "consumed_line": None,
        "source_artifacts": [],
    }


_STATE = _new_state()


def reset_state():
    _STATE.clear()
    _STATE.update(_new_state())


def body_size() -> float:
    return PROFILE.get("body_size", DEFAULT_BODY_SIZE)


def is_styled():
    return bool(PROFILE.get("styled"))


def is_body_size(size: float) -> bool:
    return abs(size - body_size()) <= 1.6


def _probe(filename: str, sample_pages: int = 10):
    """Cheap pdfminer pass over the first pages to pick an extraction profile.

    Uses a raw character collector (``laparams=None``) instead of
    ``extract_pages``: the probe only counts glyphs, so pdfminer's full layout
    analysis would be pure overhead on pages that get extracted again anyway.
    """
    from pdfminer.converter import PDFLayoutAnalyzer
    from pdfminer.layout import LTChar
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage

    fonts: Counter[str] = Counter()
    sizes: Counter[float] = Counter()
    spaces = 0
    chars = 0

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

    resource_manager = PDFResourceManager()

    class ProbeCollector(PDFLayoutAnalyzer):
        def __init__(self):
            super().__init__(resource_manager, laparams=None)

        def receive_layout(self, ltpage):
            walk(ltpage)

    interpreter = PDFPageInterpreter(resource_manager, ProbeCollector())
    with open(filename, "rb") as source:
        for page in itertools.islice(PDFPage.get_pages(source), sample_pages):
            interpreter.process_page(page)

    space_ratio = spaces / max(chars, 1)
    ocr = any("invisible" in name.lower() or "ocr" in name.lower() for name in fonts)
    if not chars or ocr:
        mode = "ocr"
    elif space_ratio < 0.05:
        mode = "char-break"
    else:
        mode = "char-preserve"
    profile = {
        "mode": mode,
        "body_size": float(sizes.most_common(1)[0][0]) if sizes else DEFAULT_BODY_SIZE,
        "space_ratio": space_ratio,
        # Styling is useful only when the extractor can actually recognize it.
        # A character layer containing only a regular font is no better than OCR
        # for rules that use bold/italic as structural evidence.
        "styled": mode != "ocr"
        and any(
            marker in font.lower() for font in fonts for marker in ("bold", "italic")
        ),
    }
    log(f"probed profile: {profile}")
    return profile


# ---------------------------------------------------------------------------
# shared enrichment: columns, session pages, running headers, back matter
# ---------------------------------------------------------------------------

_SEJA_WORD = r"SEJ(?:A|E|I|O|AH|AMI)?"
_SEDNICA_WORD = r"S(?:JED|ED)NIC(?:A|E|I|O|U|OM|AMA)?"
_ZASEDANJE_WORD = r"ZAS(?:JED|ED)ANJ(?:E|A|U|EM|IMA)?"
_SESSION_WORD = rf"(?:{_SEJA_WORD}|{_SEDNICA_WORD}|{_ZASEDANJE_WORD})"
_DEBATE_SESSION_WORD = rf"(?:{_SEJA_WORD}|{_SEDNICA_WORD})"
SESSION_NUM_RE = re.compile(
    rf"\b\d+\.\s*(?:(?:izredn\w*|redn\w*|skupn\w*|zajedni\w*)\s+)?"
    rf"{_SESSION_WORD}\b",
    re.IGNORECASE,
)
SESSION_STANDALONE_RE = re.compile(
    rf"^\s*\d+\.\s*(?:(?:izredn\w*|redn\w*|skupn\w*|zajedni\w*)\s+)?"
    rf"{_SESSION_WORD}\s*[.:-]?\s*$",
    re.IGNORECASE,
)
_SESSION_CAPS = _SESSION_WORD
SESSION_CAPS_RE = re.compile(rf"\b(?:{_SESSION_CAPS})\b")
DEBATE_SESSION_RE = re.compile(rf"\b{_DEBATE_SESSION_WORD}\b", re.IGNORECASE)
TRANSCRIPT_CUE_RE = re.compile(
    r"^(?:pred?sedni[kc]\w*|podpredsedni\w*|potpredsedni\w*|"
    r"predsedujo\S+|predsedava\w*)\s+(?:dr\.?\s+)?\S.{0,70}:\s*",
    re.IGNORECASE,
)


def _session_number_match(text: str):
    """Accept a session number at the start plus one OCR-noise character."""
    match = SESSION_NUM_RE.search(text)
    if match is None:
        return None
    prefix = text[: match.start()].strip(" \t([{<.-–—")
    return match if len(prefix) <= 1 else None


def _is_session_marker(text: str):
    text = text.strip()
    if SESSION_STANDALONE_RE.fullmatch(text) or _session_number_match(text):
        return True
    return text.isupper() and bool(SESSION_CAPS_RE.search(text))


def _is_centered_record(record: LineRecord, page: PDFPageContext) -> bool:
    """Use measured line bounds when present, with a cautious text estimate."""
    x_end = record.x_end
    if x_end is None:
        x_end = record.x + len(record.text.strip()) * record.font_size * 0.48
    left_margin = max(0.0, record.x)
    right_margin = max(0.0, page.width - x_end)
    return abs(left_margin - right_margin) <= max(18.0, page.width * 0.06)


def enrich_page(page: PDFPageContext, records: list[LineRecord]):
    body_x = sorted(
        record.x
        for record in records
        if is_body_size(record.font_size)
        and record.y < page.height * COLUMN_PROBE_Y_MAX
    )
    xs = body_x if body_x else sorted(record.x for record in records)

    def flush_edge(cluster: list[float]) -> float:
        # the column's flush-left edge: the smallest x that several other
        # lines share (a lone stray line must not become the edge)
        need = max(3, len(cluster) // 20)
        for i, value in enumerate(cluster):
            support = bisect_right(cluster, value + 3.0, i) - i
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
    _mark_tabular_labels(page, records)

    session_markers = [
        record
        for record in records
        if (
            _is_session_marker(record.text)
            and (
                bool(SESSION_STANDALONE_RE.fullmatch(record.text.strip()))
                or record.font_size >= body_size() + 1.0
                or (record.text.strip().isupper() and len(record.text.strip()) < 80)
                or _is_centered_record(record, page)
            )
        )
    ]
    has_session_marker = bool(session_markers)
    has_debate_session_marker = any(
        DEBATE_SESSION_RE.search(record.text) for record in session_markers
    )
    has_transcript_cue = any(
        record.text.strip()[:1].isupper()
        and TRANSCRIPT_CUE_RE.match(record.text.strip())
        for record in records
    )
    has_speaker_index = any(
        SPEAKER_INDEX_RE.match(record.text.strip()) for record in records
    )
    has_appendix = any(
        record.font_size >= body_size() + 1.5
        and PRILOGE_RE.match(record.text.strip())
        and len(record.text.strip()) < 30
        for record in records
    )
    if has_session_marker:
        _STATE["seen_meeting"] = True
    debate_resumes = has_debate_session_marker or (
        _STATE["seen_meeting"] and has_transcript_cue
    )
    if _STATE["back_matter"]:
        # Appendices quote session names and office-holders frequently. Only
        # reopen transcript parsing when a real session marker and a speech
        # cue occur together on the same page.
        debate_resumes = has_debate_session_marker and has_transcript_cue
    if debate_resumes:
        _STATE["seen_session"] = True
        _STATE["speaker_index"] = False
        _STATE["back_matter"] = False
    if has_speaker_index:
        _STATE["speaker_index"] = True
    if has_appendix:
        _STATE["back_matter"] = True
    page.metadata["speaker_index"] = _STATE["speaker_index"]
    page.metadata["back_matter"] = _STATE["back_matter"]
    page.metadata["structure_active"] = bool(
        _STATE["seen_session"]
        and not _STATE["speaker_index"]
        and not _STATE["back_matter"]
    )
    page.metadata["heading_active"] = bool(
        page.metadata["structure_active"] or has_session_marker
    )
    page.metadata["toc_page"] = any(
        CONTENTS_RE.match(record.text.strip()) for record in records
    )


def _has_compact_qualifier(text: str) -> bool:
    match = re.search(r"\(([^)]*)\)", text)
    if match is None:
        return False
    length = sum(character.isalnum() for character in match.group(1))
    return 1 <= length <= 4


def _mark_tabular_labels(page: PDFPageContext, records: list[LineRecord]) -> None:
    """Mark repeated colon labels that form the left side of a table."""

    candidates = [
        record
        for record in records
        if record.text.strip().endswith(":")
        and len(record.text.strip()) <= 72
        and not SESSION_STANDALONE_RE.fullmatch(record.text.strip())
    ]
    clusters: list[list[LineRecord]] = []
    x_tolerance = max(8.0, page.width * 0.025)
    for record in sorted(candidates, key=lambda item: item.x):
        if not clusters or abs(record.x - clusters[-1][0].x) > x_tolerance:
            clusters.append([record])
        else:
            clusters[-1].append(record)

    for cluster in clusters:
        if len(cluster) < 3:
            continue
        italic_count = sum(
            "italic" in record.font_name.casefold()
            or "oblique" in record.font_name.casefold()
            for record in cluster
        )
        parallel_count = sum(
            any(
                other is not record
                and abs(other.y - record.y) <= 2.5
                and other.x >= record.x + page.width * 0.15
                for other in records
            )
            for record in cluster
        )
        # Italic speaker labels repeat at a common margin too. Treat the
        # cluster as tabular only when the typography and a parallel value
        # column corroborate each other.
        if italic_count * 2 < len(cluster) or parallel_count * 2 < len(cluster):
            continue
        for record in cluster:
            record.metadata["tabular_label"] = True

    # Word extraction commonly reconstructs both table columns as one line:
    # ``Spodnja Idrija: | Spodnja Kanomlja ...``.  Learn repeated label and
    # value starts from the run geometry instead of relying on coordinates
    # chosen for one particular document.
    inline_candidates: list[tuple[LineRecord, float, float, bool, bool]] = []
    for record in records:
        meaningful_runs = [
            run for run in record.runs if run.text and run.text.strip()
        ]
        for index, run in enumerate(meaningful_runs[:-1]):
            label = " ".join(
                item.text.strip() for item in meaningful_runs[: index + 1]
            )
            if (
                not label.endswith(":")
                or len(label) > 72
                or SESSION_STANDALONE_RE.fullmatch(label)
            ):
                continue
            value_run = meaningful_runs[index + 1]
            label_italic = any(
                marker in run.font_name.casefold()
                for marker in ("italic", "oblique")
            )
            style_change = run.font_name != value_run.font_name
            inline_candidates.append(
                (record, record.x, value_run.x, label_italic, style_change)
            )
            break

    inline_clusters: list[
        list[tuple[LineRecord, float, float, bool, bool]]
    ] = []
    label_tolerance = max(8.0, page.width * 0.02)
    value_tolerance = max(5.0, page.width * 0.012)
    for candidate in sorted(inline_candidates, key=lambda item: (item[1], item[2])):
        matching = next(
            (
                cluster
                for cluster in inline_clusters
                if abs(candidate[1] - cluster[0][1]) <= label_tolerance
                and abs(candidate[2] - cluster[0][2]) <= value_tolerance
            ),
            None,
        )
        if matching is None:
            inline_clusters.append([candidate])
        else:
            matching.append(candidate)

    for cluster in inline_clusters:
        if len(cluster) < 3:
            continue
        italic_count = sum(candidate[3] for candidate in cluster)
        style_change_count = sum(candidate[4] for candidate in cluster)
        # Three styled rows or five exceptionally regular unstyled rows are
        # stronger evidence of a table than of consecutive speaker labels.
        styled_table = (
            italic_count * 2 >= len(cluster)
            and style_change_count * 2 >= len(cluster)
        )
        unstyled_table = len(cluster) >= 5 and (
            max(candidate[2] for candidate in cluster)
            - min(candidate[2] for candidate in cluster)
            <= value_tolerance
        )
        if styled_table or unstyled_table:
            for record, *_signals in cluster:
                record.metadata["tabular_label"] = True

    # Some character PDFs fuse the label and value into one same-font run.
    # Two or more aligned rows whose value clauses begin alike and contain
    # several numbers are statistical table rows, not consecutive speeches.
    numeric_rows: list[tuple[LineRecord, str]] = []
    for record in records:
        match = re.match(r"^([^:]{2,72}):\s*(.+)$", record.text.strip())
        if match is None:
            continue
        label, value = match.groups()
        if sum(character.isdigit() for character in value) < 3:
            continue
        leading_token = next(
            (
                token
                for token in re.findall(r"[^\W\d_]+", value, re.UNICODE)
            ),
            "",
        )
        if _looks_like_person_prefix(label) and not leading_token[:1].islower():
            continue
        leading = leading_token.casefold()
        if leading:
            numeric_rows.append((record, leading))
    for record, leading in numeric_rows:
        peers = [
            other
            for other, other_leading in numeric_rows
            if other is not record
            and other_leading == leading
            and abs(other.x - record.x) <= x_tolerance
        ]
        if peers:
            record.metadata["tabular_label"] = True

    # Legal text also contains compact boundary/name tables whose rows are
    # fused into single same-font runs. A large dense group, or a smaller
    # group with a structural qualifier such as ``(del)``, is sufficient
    # evidence without relying on document-specific place names.
    if records:
        compact_rows: list[tuple[LineRecord, str]] = []
        for record in records:
            match = re.match(r"^[^:]{2,72}:\s*(\S.*)$", record.text.strip())
            if match is not None:
                compact_rows.append((record, match.group(1)))
        compact_clusters: list[list[tuple[LineRecord, str]]] = []
        for candidate in sorted(compact_rows, key=lambda item: item[0].x):
            if (
                not compact_clusters
                or abs(candidate[0].x - compact_clusters[-1][0][0].x)
                > x_tolerance
            ):
                compact_clusters.append([candidate])
            else:
                compact_clusters[-1].append(candidate)
        for cluster in compact_clusters:
            if len(cluster) < 3:
                continue
            short_values = sum(len(value) <= 50 for _record, value in cluster)
            if short_values * 2 < len(cluster):
                continue
            speaker_like_labels = sum(
                _looks_like_person_prefix(record.text.split(":", 1)[0])
                for record, _value in cluster
            )
            if speaker_like_labels * 2 >= len(cluster):
                continue
            has_compact_qualifier = any(
                _has_compact_qualifier(record.text.split(":", 1)[0])
                for record, _value in cluster
            )
            if len(cluster) < 5 and not has_compact_qualifier:
                continue
            for record, _value in cluster:
                label = record.text.split(":", 1)[0]
                if (
                    has_compact_qualifier
                    or not _looks_like_person_prefix(label)
                ):
                    record.metadata["tabular_label"] = True


def enrich_line(
    page: PDFPageContext,
    record: LineRecord,
    _index: int,
    _records: list[LineRecord],
):
    columns = cast(list, page.metadata.get("columns")) or [record.x]
    col_left = max(
        (left for left in columns if left <= record.x + 2.0), default=record.x
    )
    metadata: dict[str, object] = {
        "indent": record.x - col_left,
        "centered": _is_centered_record(record, page),
    }
    artifact_type = source_artifact_type(record, page)
    if artifact_type is not None:
        metadata["source_artifact"] = artifact_type
        metadata["out_of_flow"] = True
    return metadata


HEADER_PATTERN_RE = re.compile(rf"\d+\.?\s*{_SESSION_WORD}", re.IGNORECASE)
RUNNING_FOOTER_RE = re.compile(
    r"^(?:\d+\s+)?(?:st|[šs]t)\.?\s+(?:bele[žšz]k\w*|zapis\w*)\b",
    re.IGNORECASE,
)


def source_artifact_type(record: LineRecord, page: PDFPageContext) -> str | None:
    """Classify text formerly filtered out before it reached the parser."""
    if page.metadata.get("speaker_index", False):
        return "speakerIndex"
    # running page headers: top ~8.5% of the page, and either notably smaller
    # than the body or a "N. seja/sednica" colontitle
    if record.y > page.height * RUNNING_HEADER_Y:
        text = record.text.strip()
        if record.font_size <= body_size() - 1.0 or len(text) <= 4:
            return "pageNumber" if text.strip(". ").isdigit() else "runningHeader"
        if HEADER_PATTERN_RE.search(text):
            return "runningHeader"
    # Some bound volumes print a small repeated series title beside the page
    # number at the bottom (for example "3 St. beležke SNS 1964/5"). It has
    # footnote-like geometry but is page furniture, not a numbered note.
    if (
        record.y < page.height * RUNNING_FOOTER_Y
        and record.font_size <= body_size() - 1.0
        and RUNNING_FOOTER_RE.match(record.text.strip())
    ):
        return "runningFooter"
    return None


PRILOGE_RE = re.compile(r"^PRILOG[EIA]?\b")
SPEAKER_INDEX_RE = re.compile(r"^SEZNAM\s+GOVORNIKOV\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# chunk-level helpers
# ---------------------------------------------------------------------------


_WHITESPACE_RE = re.compile(r"\s+")


def line_text(chunk: PDFChunk):
    # normalized once per physical line: the rule cascade asks for it many
    # times per chunk, and line text never changes after extraction
    line = chunk.line_chunk
    cached = line.metadata.get("_line_text")
    if cached is None:
        cached = _WHITESPACE_RE.sub(" ", line.text).strip()
        line.metadata["_line_text"] = cached
    return cast(str, cached)


def indent(chunk: PDFChunk):
    value = chunk.line_chunk.metadata.get("indent", 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def line_has_bold(chunk: PDFChunk):
    return any(run.bold for run in chunk.line_chunk.runs)


def is_body_line(chunk: PDFChunk):
    return is_body_size(chunk.font_size)


def is_structural_page(chunk: PDFChunk):
    context = chunk.page_context
    return bool(context is not None and context.metadata.get("structure_active"))


def is_footnote_page(chunk: PDFChunk):
    """Footnotes occur in both debate text and retained appendices."""

    context = chunk.page_context
    return bool(
        context is not None
        and (
            context.metadata.get("structure_active")
            or context.metadata.get("back_matter")
        )
        and not context.metadata.get("speaker_index")
    )


def is_heading_page(chunk: PDFChunk):
    context = chunk.page_context
    return bool(
        context is not None
        and context.metadata.get(
            "heading_active", context.metadata.get("structure_active")
        )
    )


def open_root_division(div_type: str):
    while len(engine.stack) > 1:
        engine.pop()
    push("div", type=div_type)


# ---------------------------------------------------------------------------
# speaker detection (union of the three configs)
# ---------------------------------------------------------------------------

TITLE_PREFIX_RE = re.compile(
    r"^(?:pred?sedni[kc]\w*|podpredsedni\w*|potpredsedni\w*|predsedujo\S+|"
    r"predsedava\w*|dr\.?|d\s+r\.?|in[žz]\.?|ing\.?|mr\.?)\b",
    re.IGNORECASE,
)

PERSON_PROSE_CUE_RE = re.compile(
    r"^(?:za|je|pa|se|so|bo|bi|o|na|po|od|do|kot|kao|i|in|ter|ali|da)$",
    re.IGNORECASE,
)
ROLE_BOUNDARY_RE = re.compile(
    r"\b(?:zvezni|savezni|republi\S*|pokrajin\S*|sekretar\S*|minister\S*|"
    r"poslan\S*|delegat\S*|član\S*|predstavnik\S*)\b",
    re.IGNORECASE,
)


def _word_core(token: str):
    return re.sub(r"[^0-9A-Za-zČŠŽĆĐčšžćđ]", "", token)


def _looks_like_person_prefix(prefix: str):
    """Distinguish a speaker label from ordinary prose ending in a colon."""
    raw_prefix = prefix.strip()
    if ";" in raw_prefix:
        return False
    if raw_prefix.startswith("(") and not TITLE_PREFIX_RE.match(
        raw_prefix[1:].lstrip()
    ):
        return False
    if re.search(
        r"\b(?:uradni\s+list|ur\.?\s*(?:l|list)|prilog\w*)\b",
        raw_prefix,
        re.IGNORECASE,
    ):
        return False
    if "," in raw_prefix:
        tail = raw_prefix.split(",", 1)[1]
        capitalized_tail = sum(
            token[:1].isupper()
            for token in (_word_core(item) for item in tail.split())
            if token
        )
        if capitalized_tail >= 2 and (
            raw_prefix.count(",") >= 2
            or re.search(r"\b(?:in|ter|i)\b", tail, re.IGNORECASE)
        ):
            return False
    prefix = re.sub(r"\s*\([^)]*\)\s*$", "", prefix).strip()
    if re.search(r"\b\d+\.\s*r\.?\b", prefix, re.IGNORECASE):
        return False
    # Election/appointment prose frequently begins with a plausible name and
    # then enumerates candidates: "Miran Cvenk, za podpredsednika ...:".
    # Inspect that role-bearing remainder before reducing the label to its
    # first comma-separated name.
    if re.search(
        r",\s*(?:in\s+)?za\s+(?:(?:pod)?predsedni\w*|[cčć]lan\w*)\b",
        prefix,
        re.IGNORECASE,
    ):
        return False
    # "VESELIN DJURANOVIĆ, predsednik ZIS" - the role after the comma is
    # ordinary lowercase prose; judge the name part only
    prefix = prefix.split(",", 1)[0].strip()
    words = [_word_core(token) for token in prefix.split()]
    words = [token for token in words if token]
    if (
        len(words) >= 3
        and all(len(token) == 1 for token in words)
        and "".join(words).casefold() in {"govornik", "predsednik"}
    ):
        return False
    letters = "".join(character for character in prefix if character.isalpha())
    if letters.isupper() and len(words) >= 3 and not TITLE_PREFIX_RE.match(prefix):
        return False
    # a bare "PREDSEDAVALI:"/"Predsedoval:" is a role announcement, not a
    # person - a title only counts when a name follows it
    # Speaker labels use the title as a displayed label (normally initial
    # uppercase). Lowercase accusative forms such as "predsednika Drago
    # Sotler, za člane pa:" occur inside appointment lists and are prose.
    title = TITLE_PREFIX_RE.match(prefix)
    if title and prefix[:1].isupper():
        remainder = prefix[title.end() :].strip()
        while True:
            nested = TITLE_PREFIX_RE.match(remainder)
            if nested is None:
                break
            remainder = remainder[nested.end() :].strip()
        tokens = [_word_core(token) for token in remainder.split()]
        tokens = [token for token in tokens if token]
        capitalized = sum(token[:1].isupper() for token in tokens)
        spaced_ocr_name = sum(len(token) == 1 for token in tokens) >= 3
        # A title alone is not a person. This rejects prose such as
        # "Predsednik vlade je predložil ...:" while retaining OCR-spaced
        # names such as "Predsednik d r. F e r d o K o z a k:".
        return capitalized >= 2 or spaced_ocr_name
    tokens = words
    if not 2 <= len(tokens) <= 8 or not tokens[0][:1].isupper():
        return False
    # In appointment lists, prose often starts immediately after a plausible
    # two-token name ("Drago Sotler za člane pa:"). A function word after the
    # likely name is strong negative evidence for a speaker label.
    spaced_ocr_name = sum(len(token) == 1 for token in tokens) >= 2
    if not spaced_ocr_name and any(
        PERSON_PROSE_CUE_RE.match(token) for token in tokens[2:]
    ):
        return False
    if len(tokens) > 4 and not any(len(token) == 1 for token in tokens):
        return False
    # OCR often spaces a surname ("P i r n a t") or splits it ("Me lik");
    # person labels consist of capitalized / very short fragments only
    return sum(token[:1].isupper() for token in tokens) >= 2 and all(
        token[:1].isupper() or len(token) == 1 for token in tokens[1:]
    )


def leading_caps(text: str):
    count = 0
    for character in text:
        if character.islower():
            break
        if character.isupper():
            count += 1
    return count


def speaker_parts(chunk: PDFChunk) -> tuple[str, str] | None:
    if (
        not chunk.is_line_start
        or not is_body_line(chunk)
        or chunk.line_chunk.metadata.get("tabular_label")
    ):
        return None
    match = re.match(r"^([^:]{2,72}:)\s*(.*)$", line_text(chunk))
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
    return match.group(1).strip(), match.group(2).strip()


def _flush_caps_name(text: str):
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


def is_speaker(chunk: PDFChunk):
    if not is_structural_page(chunk):
        return False
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


def _append_text(chunk: PDFChunk, text: str):
    if not text:
        return
    append(replace(chunk, text=text, space_before=False), should_annotate=[])


def _consume_physical_line(chunk: PDFChunk):
    """Mark later font runs after the reconstructed whole line was appended."""
    _STATE["consumed_line"] = chunk.line_chunk


def is_source_artifact(chunk: PDFChunk):
    return bool(
        chunk.is_line_start and chunk.line_chunk.metadata.get("source_artifact")
    )


def source_artifact_append(chunk: PDFChunk):
    """Retain excluded source furniture for the header's review notes."""
    artifact_type = str(chunk.line_chunk.metadata["source_artifact"])
    parts: list[str] = []
    for run in chunk.line_chunk.runs:
        text = run.text.strip()
        if not text:
            continue
        if parts and run.space_before:
            parts.append(" ")
        parts.append(text)
    artifacts = cast(list[dict[str, str]], _STATE["source_artifacts"])
    artifacts.append(
        {
            "type": artifact_type,
            "page": str(chunk.page_num + 1),
            "text": "".join(parts),
        }
    )
    _consume_physical_line(chunk)


def _open_speech_seg():
    pop_to("note", invert=True)  # closing the speaker note opens <u> via the hook
    pop_to("u", "div")
    push("seg")


def speaker_append_split(chunk: PDFChunk):
    """Split a speaker label from speech using the reconstructed whole line."""
    parts = speaker_parts(chunk)
    assert parts is not None
    label, speech = parts
    pop_to("div")
    push("note", type="speaker")
    _append_text(chunk, label)
    if speech:
        _open_speech_seg()
        _append_text(chunk, speech)
    else:
        pop_to("note", invert=True)  # closing the note opens <u> via the hook
    # Character extraction can yield more font runs from this same physical
    # line. The reconstructed line was appended above, so suppress those runs.
    _consume_physical_line(chunk)


def speaker_action(chunk: PDFChunk):
    if speaker_parts(chunk) is not None:
        speaker_append_split(chunk)
    else:
        # A flush all-caps name without a colon. Keep only that physical line
        # in the label; the following body line will open the utterance.
        pop_to("div")
        push("note", type="speaker")
        append(chunk, should_annotate=[])


def is_consumed_line(chunk: PDFChunk):
    return not chunk.is_line_start and _STATE["consumed_line"] is chunk.line_chunk


def is_speech_start(chunk: PDFChunk):
    """Close a label before the first following body line, even when flush-left."""
    return (
        chunk.is_line_start
        and is_structural_page(chunk)
        and is_body_line(chunk)
        and tag_is_on_top("note", type="speaker")
    )


def _collapse_spaced_letters(text: str):
    tokens = text.split()
    result: list[str] = []
    i = 0
    while i < len(tokens):
        run: list[tuple[str, str]] = []
        j = i
        while j < len(tokens) and 1 <= len(_word_core(tokens[j])) <= 2:
            core = _word_core(tokens[j])
            run.append((tokens[j], core))
            j += 1
        if len(run) >= 3 and sum(len(core) == 1 for _, core in run) >= 2:
            words: list[str] = []
            current = ""
            for _raw, core in run:
                if current.casefold() == "dr":
                    words.append(current)
                    current = core
                elif current and core[:1].isupper() and any(
                    character.islower() for character in current
                ):
                    words.append(current)
                    current = core
                else:
                    current += core
            if current:
                words.append(current)
            result.extend(words)
            i = j
        else:
            result.append(tokens[i])
            i += 1
    return " ".join(result)


def speaker_identifier(text: str):
    # Only the label before the first colon can identify a person. Later
    # colons belong to the speech and previously produced enormous IDs.
    name = text.split(":", 1)[0]
    name = _collapse_spaced_letters(name)
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name)
    name = name.split(",", 1)[0]
    while True:
        stripped = TITLE_PREFIX_RE.sub("", name.strip())
        stripped = re.sub(
            r"^(?:zbora|ve[čć]a|skup\S+)\s+", "", stripped.strip(), flags=re.IGNORECASE
        )
        if stripped == name.strip():
            break
        name = stripped
    role = ROLE_BOUNDARY_RE.search(name)
    if role and role.start() > 0:
        name = name[: role.start()]
    # A normal personal name has only a few lexical tokens. This fallback is
    # language-neutral and prevents residual agenda prose entering xml:id.
    tokens = name.split()
    for index, token in enumerate(tokens[2:], start=2):
        if PERSON_PROSE_CUE_RE.match(_word_core(token)):
            tokens = tokens[:index]
            name = " ".join(tokens)
            break
    if len(tokens) > 4 and not any(len(_word_core(token)) == 1 for token in tokens):
        name = " ".join(tokens[:3])
    serialized = "".join(ch for ch in name.title() if ch.isalnum())
    return "#" + sanitize_xml_id(serialized, prefix="speaker")


def _speaker_comparison_key(identifier: str) -> str:
    value = identifier.removeprefix("#")
    value = re.sub(r"^speaker-+", "", value, flags=re.IGNORECASE)
    value = unicodedata.normalize("NFKD", value).casefold()
    value = value.translate(str.maketrans({"0": "o", "1": "l", "|": "l"}))
    return "".join(character for character in value if character.isalpha())


def _speaker_label_quality(label: str, frequency: int) -> tuple[int, int, int, int]:
    prefix = label.split(":", 1)[0]
    tokens = [_word_core(token) for token in prefix.split()]
    noisy = sum(
        character.isdigit()
        or (
            not character.isascii()
            and "LATIN" not in unicodedata.name(character, "")
        )
        for character in prefix
    )
    singletons = sum(len(token) == 1 for token in tokens if token)
    return frequency, -noisy, -singletons, -len(prefix)


def _merge_speaker_aliases(result) -> None:
    """Merge only very close OCR variants of a document-local speaker ID."""

    mapping = result.data.get("speakers")
    if not isinstance(mapping, dict):
        return
    utterances = [
        element
        for element in result.root.iter()
        if element.tag == "u" and element.attrib.get("who")
    ]
    frequencies = Counter(element.attrib["who"] for element in utterances)
    used = [identifier for identifier in mapping if identifier in frequencies]
    groups: list[list[str]] = []
    representative_keys: list[str] = []
    for identifier in used:
        key = _speaker_comparison_key(identifier)
        best_index = None
        best_ratio = 0.0
        if len(key) >= 7:
            for index, representative in enumerate(representative_keys):
                if abs(len(key) - len(representative)) > 2:
                    continue
                ratio = SequenceMatcher(None, key, representative).ratio()
                if ratio >= 0.90 and ratio > best_ratio:
                    best_index = index
                    best_ratio = ratio
        else:
            best_index = next(
                (
                    index
                    for index, representative in enumerate(representative_keys)
                    if key == representative
                ),
                None,
            )
        if best_index is None:
            groups.append([identifier])
            representative_keys.append(key)
        else:
            groups[best_index].append(identifier)

    replacements: dict[str, str] = {}
    merged_mapping: dict[str, str] = {}
    for group in groups:
        canonical = max(
            group,
            key=lambda identifier: _speaker_label_quality(
                str(mapping[identifier]), frequencies[identifier]
            ),
        )
        merged_mapping[canonical] = str(mapping[canonical])
        replacements.update((identifier, canonical) for identifier in group)
    for utterance in utterances:
        identifier = utterance.attrib["who"]
        utterance.set("who", replacements.get(identifier, identifier))
    result.data["speakers"] = merged_mapping


speaker_hook = SpeakerUtteranceHook(speaker_identifier)


def _remove_empty_utterances(root: ET.Element) -> None:
    """Drop hook-created utterances that never received source text."""

    for parent in root.iter():
        for child in list(parent):
            if (
                child.tag == "u"
                and not list(child)
                and not (child.text or "").strip()
            ):
                parent.remove(child)


def start_document():
    """Keep the outer debate div division-only for schema-safe later heads."""
    reset_document_state()
    speaker_hook.reset()
    open_root_division("frontMatter")


def finish_document(result):
    footnotes.finalize()
    speaker_hook.export(result)
    _remove_empty_utterances(result.root)
    _merge_speaker_aliases(result)
    if footnotes.unresolved_count:
        warnings = result.data.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(
                f"{footnotes.unresolved_count} raised numeric run(s) had no "
                "matching footnote definition and were preserved as "
                "<hi rend='superscript'> instead of dangling references."
            )
    profile_warnings = PROFILE.get("warnings", [])
    if isinstance(profile_warnings, list) and profile_warnings:
        for warning in profile_warnings:
            result.diagnostics.recover("extractor.recovery", str(warning))
    outer = engine.debate
    for child in list(outer):
        if child.tag == "div" and child.attrib.get("type") == "frontMatter":
            if not ("".join(child.itertext()).strip() or list(child)):
                outer.remove(child)
    artifacts = cast(list[dict[str, str]], _STATE["source_artifacts"])
    if artifacts:
        file_desc = result.root.find("teiHeader/fileDesc")
        if file_desc is not None:
            notes_stmt = file_desc.find("notesStmt")
            if notes_stmt is None:
                notes_stmt = ET.Element("notesStmt")
                source_desc = file_desc.find("sourceDesc")
                index = (
                    list(file_desc).index(source_desc)
                    if source_desc is not None
                    else len(file_desc)
                )
                file_desc.insert(index, notes_stmt)
            grouped_artifacts: dict[tuple[str, str], list[str]] = {}
            for artifact in artifacts:
                key = artifact["type"], artifact["page"]
                grouped_artifacts.setdefault(key, []).append(artifact["text"])
            for (artifact_type, page), texts in grouped_artifacts.items():
                note = ET.SubElement(
                    notes_stmt,
                    "note",
                    type="sourceArtifact",
                    subtype=artifact_type,
                    n=page,
                )
                note.text = "\n".join(texts)


def build_tei_header():
    """Build a generic header without claiming document-specific metadata."""
    source_title = Path(engine.filename).stem
    return TEIHeader(
        main_titles={"": source_title},
        source=SourceBibl(titles={"": source_title}),
        project_desc={
            "en": "Automatically converted from a searchable PDF by doc2tei."
        },
        changes=[Change(name="doc2tei", note="Automatic conversion from source PDF.")],
    )


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
HEAD_KEYWORD_RE = re.compile(rf"\b(?:{_SESSION_CAPS}|NADALJEVANJE)\b")
AGENDA_HEAD_RE = re.compile(
    r"^\s*\d+\.\s*(?:TOČK[AE]|TAČK[AE])\s+(?:DNEVNEGA|DNEVNOG)\s+REDA\b",
    re.IGNORECASE,
)
CHAMBER_HEAD_RE = re.compile(
    r"^(?:R\s*E\s*P\s*U\s*B\s*L\s*I\s*Š\s*K\s*I\s+)?ZBOR\b|"
    r"\bZBORA\s+(?:PROIZVAJALCEV|IN\s+REPUBLIŠKEGA\s+ZBORA)\b",
    re.IGNORECASE,
)
ROMAN_HEADING_RE = re.compile(r"^[IVXLCDM]+\.?$", re.IGNORECASE)


def _plausible_heading_text(text: str) -> bool:
    if ROMAN_HEADING_RE.fullmatch(text.strip()):
        return True
    return sum(character.isalpha() for character in text) >= 4


def _head_kind(text: str) -> tuple[str, str]:
    if AGENDA_HEAD_RE.search(text):
        return "agendaItem", "agendaSection"
    if _session_number_match(text):
        return "sessionNumber", "session"
    if SESSION_CAPS_RE.search(text):
        return "session", "session"
    if CHAMBER_HEAD_RE.search(text):
        return "chamber", "session"
    if ROMAN_HEADING_RE.fullmatch(text.strip()):
        return "sectionNumber", "section"
    return "section", "section"


def is_head(chunk: PDFChunk):
    if not chunk.is_line_start or not is_heading_page(chunk):
        return False
    text = line_text(chunk)
    if (
        not text
        or len(text) > 100
        or text.endswith(":")
        or is_speaker(chunk)
        or not _plausible_heading_text(text)
    ):
        return False
    if AGENDA_HEAD_RE.search(text):
        return True
    if SESSION_STANDALONE_RE.fullmatch(text):
        return True
    if (
        text.isupper()
        and SESSION_CAPS_RE.search(text)
        and (bool(chunk.line_chunk.metadata.get("centered")) or len(text) <= 80)
    ):
        return True
    # clearly-larger line; the caps/session gate keeps garbled footnote and
    # scan-noise body lines out (session heads may sit flush left)
    if chunk.font_size >= body_size() + 1.8 and (
        text.isupper() or _session_number_match(text)
    ):
        return True
    # body-sized headings (ZRIP): full caps + bold + a session keyword
    return (
        text.isupper() and line_has_bold(chunk) and bool(HEAD_KEYWORD_RE.search(text))
    )


def head_action(chunk: PDFChunk):
    text = line_text(chunk)
    kind, div_type = _head_kind(text)

    # Heads are only schema-valid at the beginning of a div. Create a new
    # structural division once the current one already contains body content.
    pop_to("div")
    current = engine.stack[-1]
    has_body = any(
        not isinstance(child, str) and child.tag != "head" for child in current.children
    )
    current_type = current.element.attrib.get("type")
    if div_type == "session":
        if current_type != "session" or has_body:
            open_root_division("session")
    else:
        if current_type == div_type and has_body:
            engine.pop()
            current_type = engine.stack[-1].element.attrib.get("type")
        if current_type != div_type:
            push("div", type=div_type)
    if not tag_is_on_top("head", type=kind):
        push("head", type=kind)


def is_time(chunk: PDFChunk):
    if not chunk.is_line_start or not is_structural_page(chunk):
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


def time_action(chunk: PDFChunk):
    kind = "start" if START_TIME_RE.match(line_text(chunk)) else "date"
    if not tag_is_on_top("time", type=kind):
        pop_to("div")
        push("p")
        push("time", type=kind)


def is_chairman(chunk: PDFChunk):
    if (
        not chunk.is_line_start
        or not is_structural_page(chunk)
        or not CHAIRMAN_RE.match(line_text(chunk))
    ):
        return False
    # "Predsedavajući Vlado Malašič:" introduces a speaker, not the session
    # chairman note
    return speaker_parts(chunk) is None


def is_generic_note(chunk: PDFChunk):
    if not chunk.is_line_start or not is_structural_page(chunk):
        return False
    if chunk.line_chunk.metadata.get("tabular_label"):
        return False
    text = line_text(chunk)
    if not text:
        return False
    closing_parenthesis = text.rfind(")")
    if (
        is_body_line(chunk)
        and indent(chunk) >= 6
        and text.startswith("(")
        and 0 < closing_parenthesis <= 300
        and not text[closing_parenthesis + 1 :].strip(" .!?")
    ):
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


def generic_note_action(chunk: PDFChunk):
    pop_to("u", "div")
    push("note")


def is_after_generic_note(chunk: PDFChunk):
    """Do not let a one-line stage note absorb an unrelated following line."""

    if not chunk.is_line_start or not tag_is_on_top("note"):
        return False
    current = next(
        (entry for entry in reversed(engine.stack) if not entry.cosmetic),
        None,
    )
    return bool(
        current is not None
        and current.element.tag == "note"
        and current.element.attrib.get("type") is None
        and current.element.attrib.get("place") is None
        and not is_generic_note(chunk)
    )


def after_generic_note_action(chunk: PDFChunk):
    while len(engine.stack) > 1 and engine.stack[-1].cosmetic:
        engine.pop()
    pop_to("note", invert=True)
    if tag_is_on_top("u"):
        push("seg")
    else:
        pop_to("div")
        push("p")


def is_contents(chunk: PDFChunk):
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


def contents_action(chunk: PDFChunk):
    if not tag_is_on_top("note", type="contents"):
        pop_to("div")
        push("note", type="contents")


def is_seg(chunk: PDFChunk):
    return (
        chunk.is_line_start
        and is_structural_page(chunk)
        and is_body_line(chunk)
        and 6.0 <= indent(chunk) <= 45.0
        and has_utterance_context()
    )


def has_utterance_context():
    """An open utterance or speaker note that will open one when popped."""
    return any(
        entry.element.tag == "u"
        or (
            entry.element.tag == "note"
            and entry.element.attrib.get("type") == "speaker"
        )
        for entry in engine.stack
    )


def is_paragraph(chunk: PDFChunk):
    """Open non-speech paragraphs at measured starts, not every wrapped line."""

    if not chunk.is_line_start or has_utterance_context():
        return False
    current = next(
        (entry for entry in reversed(engine.stack) if not entry.cosmetic),
        None,
    )
    if current is None or current.element.tag != "p":
        return True
    if current.element.attrib.get("type") == "unparsed":
        return True
    return is_body_line(chunk) and 6.0 <= indent(chunk) <= 45.0


def unmatched_line_needs_review(chunk: PDFChunk):
    if not chunk.is_line_start:
        return False
    current = next(
        (entry for entry in reversed(engine.stack) if not entry.cosmetic),
        None,
    )
    return bool(current is not None and current.element.tag in {"div", "u"})


def unmatched_line_append(chunk: PDFChunk):
    """Flag and safely contain a line that would otherwise be direct text."""
    while len(engine.stack) > 1 and engine.stack[-1].cosmetic:
        engine.pop()
    append_comment("doc2tei: unmatched source line; text preserved for manual review")
    current_tag = next(
        entry.element.tag for entry in reversed(engine.stack) if not entry.cosmetic
    )
    push(
        "seg" if current_tag == "u" else "p",
        type="unparsed",
        n=str(chunk.page_num + 1),
    )
    append(chunk)


footnotes = FootnoteLinker(
    body_size=body_size,
    mode=lambda: PROFILE.get("mode"),
    structural_page=is_footnote_page,
    utterance_context=has_utterance_context,
)


def reset_document_state():
    reset_state()
    footnotes.reset()


def is_appendix_head(chunk: PDFChunk):
    context = chunk.page_context
    return bool(
        chunk.is_line_start
        and context is not None
        and context.metadata.get("back_matter")
        and PRILOGE_RE.match(line_text(chunk))
        and len(line_text(chunk)) < 30
    )


def appendix_head_action(_chunk: PDFChunk):
    open_root_division("backMatter")
    push("head", type="appendix")


def is_unstructured_start(chunk: PDFChunk):
    """Close a debate utterance when retained back matter begins."""
    if not chunk.is_line_start or is_structural_page(chunk):
        return False
    current = next(
        (entry for entry in reversed(engine.stack) if entry.element.tag == "div"),
        None,
    )
    return bool(
        current is not None
        and current.element.attrib.get("type") not in {"frontMatter", "backMatter"}
    )


def unstructured_start_action(_chunk: PDFChunk):
    open_root_division("backMatter")
    push("p")


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
        "test": footnotes.is_inline_reference,
        "tag": tag("ref", type="footnote"),
        "append_func": footnotes.inline_reference_action,
    },
}


CONFIG: PDFConfig = {
    "debug": False,
    "mode": "pdf",
    "on_start": start_document,
    "on_pop": speaker_hook,
    "on_end": finish_document,
    "auto_xml_ids": True,
    "recover_errors": True,
    "merge_nearby_runs": True,
    # 0 selects up to eight local processes; 1 forces the exact sequential
    # path. Stateful page/line enrichment still runs in document order here.
    "page_workers": 0,
    "tei_header": build_tei_header,
    "rules": {
        "SOURCE_ARTIFACT": {
            "test": is_source_artifact,
            "append_func": source_artifact_append,
        },
        "CONSUMED_LINE": {
            "test": is_consumed_line,
            "append_func": lambda chunk: None,
        },
        "CONSUMED_FOOTNOTE_RUN": {
            "test": footnotes.is_consumed_run,
            "append_func": footnotes.consumed_run_action,
        },
        "APPENDIX_HEAD": {
            "test": is_appendix_head,
            "action": appendix_head_action,
        },
        "UNSTRUCTURED_START": {
            "test": is_unstructured_start,
            "action": unstructured_start_action,
        },
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
            "test": footnotes.is_entry,
            "action": footnotes.entry_action,
            "append_func": lambda chunk: None,
        },
        "FOOTNOTE_CONTINUATION": {
            "test": footnotes.is_continuation,
            "append_func": lambda chunk: append(chunk, should_annotate=[]),
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
        "SPEECH_START": {
            "test": is_speech_start,
            "action": lambda _: _open_speech_seg(),
        },
        "AFTER_FOOTNOTE": {
            "test": footnotes.is_after,
            "action": footnotes.after_action,
        },
        "AFTER_GENERIC_NOTE": {
            "test": is_after_generic_note,
            "action": after_generic_note_action,
        },
        "SEG": {
            "test": is_seg,
            "action": pop_and_push_to("u", "div", tag="seg", chunked=False),
        },
        "PARAGRAPH": {
            "test": is_paragraph,
            "action": pop_and_push_to("div", tag="p", chunked=False),
        },
        "UNMATCHED_LINE": {
            "test": unmatched_line_needs_review,
            "append_func": unmatched_line_append,
        },
    },
}


# ---------------------------------------------------------------------------
# extraction dispatch
# ---------------------------------------------------------------------------


def _record_page_error(page_num: int, error: Exception) -> None:
    warnings = PROFILE.setdefault("warnings", [])
    if isinstance(warnings, list):
        warnings.append(
            f"page {page_num + 1} extraction failed; conversion continued: "
            f"{type(error).__name__}: {error}"
        )


def _make_extractor(mode: str):
    common = dict(
        page_enricher=enrich_page,
        line_enricher=enrich_line,
        page_error_handler=_record_page_error,
    )
    character_common = dict(
        page_workers=int(CONFIG.get("page_workers", 0)),
        merge_nearby_runs=bool(CONFIG.get("merge_nearby_runs", True)),
        **common,
    )
    if mode == "ocr":
        return WordPDFExtractor(
            x_tolerance=1.7,
            y_tolerance=3.0,
            line_tolerance=3.2,
            word_gap=0.6,
            join_line_end_hyphens=True,
            preserve_word_runs=True,
            page_workers=int(CONFIG.get("page_workers", 0)),
            **common,
        )
    if mode == "char-break":
        return CharacterPDFExtractor(
            line_tolerance=LINE_BREAK_TOLERANCE,
            line_break_mode="downward",
            literal_spaces="break",
            gap_threshold=1.7,
            max_run_x_gap=30,
            **character_common,
        )
    return CharacterPDFExtractor(
        line_tolerance=4.0,
        literal_spaces="preserve",
        **character_common,
    )


def get_chunks(filename: str) -> Iterator[Chunk]:
    PROFILE.clear()
    try:
        PROFILE.update(_probe(filename))
    except Exception as error:
        # Probing is an optimization, not a reason to reject a document. The
        # word extractor is the safest first fallback for unusual PDF internals.
        PROFILE.update(
            mode="ocr",
            body_size=DEFAULT_BODY_SIZE,
            space_ratio=0.0,
            styled=False,
            warnings=[f"profile probe failed: {type(error).__name__}: {error}"],
        )
    reset_document_state()

    preferred_mode = str(PROFILE["mode"])
    fallback_mode = "char-preserve" if preferred_mode == "ocr" else "ocr"
    for attempt, mode in enumerate((preferred_mode, fallback_mode), start=1):
        PROFILE["mode"] = mode
        if mode == "ocr":
            PROFILE["styled"] = False
        emitted = 0
        try:
            for chunk in _make_extractor(mode)(filename):
                emitted += 1
                yield chunk
        except Exception as error:
            warnings = PROFILE.setdefault("warnings", [])
            if isinstance(warnings, list):
                warnings.append(
                    f"{mode} extraction failed after {emitted} chunk(s): "
                    f"{type(error).__name__}: {error}"
                )
            # Restarting with another extractor after text was emitted would
            # duplicate the document. Keep the safe partial output instead.
            if emitted:
                return
        if emitted:
            return
        if attempt == 1:
            reset_document_state()
