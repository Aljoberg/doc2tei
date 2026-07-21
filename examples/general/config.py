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
import xml.etree.ElementTree as ET
from bisect import bisect_right
from collections import Counter
from dataclasses import replace
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
    Chunk,
    PDFChunk,
    PDFPageContext,
    append,
    append_comment,
    pop_and_push_to,
    pop_to,
    push,
    tag,
    tag_is_on_top,
)
from type_decs import PDFConfig, PDFCosmeticAnnotations


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

PROFILE: dict[str, Any] = {}


def _new_state() -> dict[str, Any]:
    return {
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
    """Cheap pdfminer pass over the first pages to pick an extraction profile."""
    from pdfminer.layout import LTChar
    from pdfminer.high_level import extract_pages

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

    for page in itertools.islice(extract_pages(filename), sample_pages):
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

SESSION_NUM_RE = re.compile(
    r"\d+\.\s*(?:izredna\s+|redna\s+|zajedni\S+\s+)?(?:sej[aeio]|sednic[aeio])\b",
    re.IGNORECASE,
)
_SESSION_CAPS = r"SEJ[AEO]\w*|SEDNIC\w*"
SESSION_CAPS_RE = re.compile(rf"\b(?:{_SESSION_CAPS})\b")


def _is_session_marker(text: str):
    text = text.strip()
    if SESSION_NUM_RE.search(text):
        return True
    return text.isupper() and bool(SESSION_CAPS_RE.search(text))


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

    has_session_marker = any(
        _is_session_marker(record.text)
        and (
            record.font_size >= body_size() + 1.0
            or (record.text.strip().isupper() and len(record.text.strip()) < 80)
        )
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
    page.metadata["toc_page"] = any(
        CONTENTS_RE.match(record.text.strip()) for record in records
    )


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
    metadata: dict[str, object] = {"indent": record.x - col_left}
    artifact_type = source_artifact_type(record, page)
    if artifact_type is not None:
        metadata["source_artifact"] = artifact_type
        metadata["out_of_flow"] = True
    return metadata


HEADER_PATTERN_RE = re.compile(r"\d+\.?\s*(?:sej[aeio]|sednic[aeio])", re.IGNORECASE)


def source_artifact_type(
    record: LineRecord, page: PDFPageContext
) -> str | None:
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
    return None


PRILOGE_RE = re.compile(r"^PRILOG[EIA]?\b")
SPEAKER_INDEX_RE = re.compile(r"^SEZNAM\s+GOVORNIKOV\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# chunk-level helpers
# ---------------------------------------------------------------------------


def line_text(chunk: PDFChunk):
    return re.sub(r"\s+", " ", chunk.line_chunk.text).strip()


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
    prefix = re.sub(r"\s*\([^)]*\)\s*$", "", prefix).strip()
    # "VESELIN DJURANOVIĆ, predsednik ZIS" - the role after the comma is
    # ordinary lowercase prose; judge the name part only
    prefix = prefix.split(",", 1)[0].strip()
    # a bare "PREDSEDAVALI:"/"Predsedoval:" is a role announcement, not a
    # person - a title only counts when a name follows it
    # Speaker labels use the title as a displayed label (normally initial
    # uppercase). Lowercase accusative forms such as "predsednika Drago
    # Sotler, za člane pa:" occur inside appointment lists and are prose.
    if (
        TITLE_PREFIX_RE.match(prefix)
        and prefix[:1].isupper()
        and len(prefix.split()) >= 2
    ):
        return True
    tokens = [_word_core(token) for token in prefix.split()]
    tokens = [token for token in tokens if token]
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
    if not chunk.is_line_start or not is_body_line(chunk):
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
        chunk.is_line_start
        and chunk.line_chunk.metadata.get("source_artifact")
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
    return (
        not chunk.is_line_start
        and _STATE["consumed_line"] is chunk.line_chunk
    )


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
        run: list[str] = []
        j = i
        while j < len(tokens) and len(_word_core(tokens[j])) == 1:
            core = _word_core(tokens[j])
            if (
                run
                and core.isupper()
                and (run[0].islower() or "".join(run).lower() in {"dr", "inž"})
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
    serialized = "#" + "".join(ch for ch in name.title() if ch.isalnum())
    return serialized if serialized != "#" else "#UnknownSpeaker"


speaker_hook = SpeakerUtteranceHook(speaker_identifier)


def start_document():
    """Keep the outer debate div division-only for schema-safe later heads."""
    reset_document_state()
    speaker_hook.reset()
    open_root_division("frontMatter")


def finish_document(result):
    speaker_hook.export(result)
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
            for artifact in artifacts:
                note = ET.SubElement(
                    notes_stmt,
                    "note",
                    type="sourceArtifact",
                    subtype=artifact["type"],
                    n=artifact["page"],
                )
                note.text = artifact["text"]


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


def is_head(chunk: PDFChunk):
    if not chunk.is_line_start or not is_structural_page(chunk):
        return False
    text = line_text(chunk)
    if not text or len(text) > 100 or text.endswith(":") or is_speaker(chunk):
        return False
    if AGENDA_HEAD_RE.search(text):
        return True
    # clearly-larger line; the caps/session gate keeps garbled footnote and
    # scan-noise body lines out (session heads may sit flush left)
    if chunk.font_size >= body_size() + 1.8 and (
        text.isupper() or SESSION_NUM_RE.search(text)
    ):
        return True
    # body-sized headings (ZRIP): full caps + bold + a session keyword
    return (
        text.isupper() and line_has_bold(chunk) and bool(HEAD_KEYWORD_RE.search(text))
    )


def head_action(chunk: PDFChunk):
    text = line_text(chunk)
    if AGENDA_HEAD_RE.search(text):
        kind = "agendaItem"
        div_type = "agendaSection"
    else:
        kind = (
            "sessionNumber"
            if re.match(r"^\W{0,3}\d+\.", text) and SESSION_NUM_RE.search(text)
            else "session"
        )
        div_type = "session"

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
        if current_type == "agendaSection" and has_body:
            engine.pop()
            current_type = engine.stack[-1].element.attrib.get("type")
        if current_type != "agendaSection":
            push("div", type="agendaSection")
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
    text = line_text(chunk)
    if not text:
        return False
    if is_body_line(chunk) and indent(chunk) >= 6 and text.startswith("("):
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
    """Keep non-speech text in a TEI block instead of directly under div."""
    return chunk.is_line_start and not has_utterance_context()


def unmatched_line_needs_review(chunk: PDFChunk):
    if not chunk.is_line_start:
        return False
    current = next(
        (entry for entry in reversed(engine.stack) if not entry.cosmetic),
        None,
    )
    return bool(
        current is not None and current.element.tag in {"div", "u"}
    )


def unmatched_line_append(chunk: PDFChunk):
    """Flag and safely contain a line that would otherwise be direct text."""
    while len(engine.stack) > 1 and engine.stack[-1].cosmetic:
        engine.pop()
    append_comment(
        "doc2tei: unmatched source line; text preserved for manual review"
    )
    current_tag = next(
        entry.element.tag
        for entry in reversed(engine.stack)
        if not entry.cosmetic
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
    structural_page=is_structural_page,
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


def _asymmetric_line_break(previous_y: float, current_y: float):
    return (
        previous_y - current_y > LINE_BREAK_TOLERANCE
        or abs(previous_y - current_y) > LINE_BREAK_TOLERANCE * 5
    )


def get_chunks(filename: str) -> Iterator[Chunk]:
    PROFILE.clear()
    PROFILE.update(_probe(filename))
    reset_document_state()

    common = dict(
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
