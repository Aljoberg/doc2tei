"""PDF config for the seventh session of Zbor republik in pokrajin."""

from __future__ import annotations

import functools
import re

from doc2tei.extractors import CharacterPDFExtractor
from doc2tei.helpers import SpeakerUtteranceHook
from engine import (
    Chunk,
    PDFChunk,
    WordChunk,
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


def ref_entry_action(chunk: PDFChunk):
    note_num = chunk.text.strip()
    serialized = re.sub(r"[^a-zA-Z0-9]", "", note_num)
    if not tag_is_on_top("note", place="foot", n=serialized):
        pop_to("u", "div")
        push(
            "note",
            attribs={"xml:id": f"#note{serialized}"},
            place="foot",
            n=serialized,
        )


def generic_note_action(_chunk: Chunk):
    pop_to("div")
    push("note")
    push("hi", cosmetic=True, rend="italic")


def nth_previous(chunk: PDFChunk, n: int) -> PDFChunk | None:
    current: PDFChunk | None = chunk
    for _ in range(n):
        if current is None:
            return None
        current = current.previous
    return current


def header_test(chunk: PDFChunk):
    for distance in range(1, 4):
        previous = nth_previous(chunk, distance)
        if previous and previous.page_num != chunk.page_num:
            return True
    return False


def speaker_identifier(text: str) -> str:
    name_surname = re.sub(r"^pre\S+\s+|\s*(?:\(|,|:).*$", "", text, flags=re.IGNORECASE)
    return "#" + "".join(word.capitalize() for word in name_surname.split())


speaker_hook = SpeakerUtteranceHook(speaker_identifier)


def leading_caps(text: str) -> int:
    count = 0
    for character in text:
        if character.islower():
            break
        if character.isupper():
            count += 1
    return count


def is_seg(chunk: PDFChunk) -> bool:
    if not chunk.is_line_start:
        return False
    previous = chunk.previous
    if (
        previous is None
        or abs(chunk.x - previous.x) > 240
        or abs(chunk.y - previous.y) > 230
    ):
        return 8.9 < chunk.font_size < 10.1
    left = 48 if chunk.page_num <= 2 else 41
    indented = left < chunk.x < 64 or 287 < chunk.x < 303
    return indented and (
        9.0 <= chunk.font_size < 10.1 and not tag_is_on_top("note", place="foot")
    )


def is_page_top(chunk: PDFChunk) -> bool:
    previous = chunk.previous
    while previous is not None and (
        724 < previous.y < 742 or len(previous.text.strip()) <= 2
    ):
        previous = previous.previous
    return previous is not None and previous.page_num != chunk.page_num


COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {
    "ITALIC": {"test": lambda chunk: chunk.italic, "tag": tag("emph")},
    "BOLD": {"test": lambda chunk: chunk.bold, "tag": tag("hi", rend="bold")},
    "REFERENCE": {
        "test": lambda chunk: (
            chunk.run.font.superscript
            if isinstance(chunk, WordChunk)
            else chunk.font_size == 7.0
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
        "HEADER": {"test": header_test, "append_func": lambda chunk: None},
        "SEJA_DECLARATION": {
            "test": lambda chunk: (
                605 < chunk.y < 610 or tag_is_on_top("head", type="session")
            )
            and chunk.bold,
            "action": pop_and_push_to("div", tag="head", type="session"),
        },
        "TIME": {
            "test": lambda chunk: chunk.italic
            and (590 < chunk.y < 600 or tag_is_on_top("head", type="sessionSection")),
            "action": pop_and_push_to("div", tag="time"),
        },
        "CHAIRMAN": {
            "test": lambda chunk: (
                chunk.text.strip().startswith("PREDSEDUJE")
                or tag_is_on_top("time")
                or tag_is_on_top("note", type="chairman")
            )
            and not chunk.italic
            and chunk.x > 174,
            "action": pop_and_push_to("div", tag="note", type="chairman"),
        },
        "SEJA_SECTION": {
            "test": lambda chunk: (
                chunk.text.isupper() and 194 < chunk.x < 360 and is_page_top(chunk)
            ),
            "action": pop_and_push_to("div", tag="head", type="sessionSection"),
        },
        "REFERENCE_ENTRY": {
            "test": lambda chunk: (
                chunk.font_size == 7.0 or 6.9 < chunk.font_size < 7.0
            )
            and chunk.text.strip().isdigit()
            and chunk.is_line_start,
            "action": ref_entry_action,
            "append_func": lambda chunk: None,
        },
        "GENERIC_NOTE": {
            "test": lambda chunk: 323 < chunk.x < 457
            and chunk.line_chunk.italic
            and bool(chunk.text.strip()),
            "action": generic_note_action,
            "append_func": lambda chunk: append(chunk, should_annotate=["REFERENCE"]),
        },
        "SPEAKER": {
            "test": lambda chunk: (0 < chunk.x < 55 or 271 < chunk.x < 293)
            and leading_caps(chunk.text) >= 5,
            "action": pop_and_push_to("div", tag="note", type="speaker"),
        },
        "SEG": {
            "test": is_seg,
            "action": pop_and_push_to("u", "div", tag="seg", chunked=False),
        },
    },
}


def zrip_line_break(previous_y: float, current_y: float) -> bool:
    tolerance = 4.871
    return (
        previous_y - current_y > tolerance
        or abs(previous_y - current_y) > tolerance * 5
    )


get_chunks = CharacterPDFExtractor(
    line_break=zrip_line_break,
    literal_spaces="break",
    gap_threshold=1.7,
    max_run_x_gap=30,
)
