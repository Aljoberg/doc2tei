# config for PROSVETNO-KULTURNO VEĆE pdf
# THIS CONFIG HAS NOT BEEN TESTED AS MUCH
# it works as long as it works, but there's some inconsistencies (like bold text not being grouped)
# but it serves as an example of another config


import xml.etree.ElementTree as ET
from typing import Any, Generator
import re
from engine import (
    Chunk,
    PDFChunk,
    StackEntry,
    make_chunk,
    pop_and_push_to,
    tag,
    tag_is_on_top,
    pop_to,
    push,
    append,
)
from type_decs import PDFConfig, PDFCosmeticAnnotations


def line_text(chunk: PDFChunk):
    return chunk.line_chunk.text.strip()


def is_line_start(chunk: PDFChunk):
    return chunk is chunk.line_chunk.runs[0]


def is_title(chunk: PDFChunk):
    return (
        is_line_start(chunk)
        and chunk.x < 160
        and chunk.y > 640
        and line_text(chunk).isupper()
    )


def is_seg(chunk: PDFChunk) -> bool:
    if not is_line_start(chunk) or not 10.4 < chunk.font_size < 11.6:
        return False
    if chunk.page_num % 2 == 0:
        # WHY IS THE FORMATTING DIFFERENT ON EVEN PAGES BROOoOOOO
        return 60 < chunk.x < 72 or 305 < chunk.x < 318
    return 45 < chunk.x < 56 or 289 < chunk.x < 300


def speaker_to_utterance(popped: StackEntry):
    # same as ZRIP's config
    if popped.element.tag == "note" and popped.element.attrib.get("type") == "speaker":
        text = "".join(
            (i.text or "") if isinstance(i, ET.Element) else i for i in popped.children
        )  # kind of a weird way of getting text, but it's fine alright we're gonna pretend there's no nesting
        name_surname = re.sub(
            r"^pre\S+\s+|\s*(?:\(|,|:).*$", "", text, flags=re.IGNORECASE
        )  # remove "PREDSEDNIK" and everything after a parenthesis or a comma
        serialized = "".join(
            word.capitalize() for word in name_surname.split()
        )  # le pascal case

        push(
            "u",
            who=f"#{serialized}",
        )


def generic_note_action(chunk: Chunk):
    pop_to("div")
    push("note")
    push("hi", rend="italic")


def contents_action(chunk: Chunk):
    # sadržaj
    if not tag_is_on_top("note", type="contents"):
        pop_to("div")
        push("note", type="contents")


# inline formatting that can appear inside anything
COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {
    "ITALIC": {
        "test": lambda chunk: chunk.italic,
        "tag": tag("emph"),
    },
    "BOLD": {"test": lambda chunk: chunk.bold, "tag": tag("hi", rend="bold")},
}

CONFIG: PDFConfig = {
    "debug": False,
    "mode": "pdf",
    "on_pop": speaker_to_utterance,
    "alignments": {
        "any": {
            # --- session front matter (opening page) ---
            "SEJA_DATE": {
                # is in title & starts with "OD "
                "test": lambda chunk: is_title(chunk)
                and line_text(chunk).startswith("OD "),
                "action": pop_and_push_to("div", tag="time"),
            },
            "SEJA_NUM": {
                "test": lambda chunk: is_title(chunk) and "SEDNICA" in line_text(chunk),
                "action": pop_and_push_to("div", tag="head", type="sessionNumber"),
            },
            "SEJA_DECLARATION": {
                # if it's not a date or a num (those get matched earlier since dict items are kept in declaration order)
                "test": is_title,
                "action": pop_and_push_to("div", tag="head", type="session"),
            },
            "CHAIRMAN": {
                # all caps or sw PREDSEDAVA
                "test": lambda chunk: is_line_start(chunk)
                and 200 < chunk.x < 285
                and (
                    line_text(chunk).startswith("PREDSEDAVA")
                    or line_text(chunk).isupper()
                ),
                "action": pop_and_push_to("div", tag="note", type="chairman"),
            },
            "TIME": {
                # either "Početak" or italic
                "test": lambda chunk: is_line_start(chunk)
                and 200 < chunk.x < 285
                and (line_text(chunk).startswith("Početak") or chunk.italic),
                "action": pop_and_push_to("div", tag="time"),
            },
            "CONTENTS": {
                # sadržaj
                "test": lambda chunk: line_text(chunk).upper().startswith("SADR")
                or tag_is_on_top("note", type="contents"),
                "action": contents_action,
            },
            # --- two-column debate body ---
            "GENERIC_NOTE": {
                # centered & italic
                "test": lambda chunk: is_line_start(chunk)
                and 140 < chunk.x < 280
                and chunk.line_chunk.italic,
                "action": generic_note_action,
                "append_func": lambda chunk: append(chunk, should_annotate=[]),
            },
            "SPEAKER": {
                # bold & ends with a colon
                "test": lambda chunk: is_line_start(chunk)
                and line_text(chunk).endswith(":")
                and len(line_text(chunk)) < 100
                and any(r.bold for r in chunk.line_chunk.runs),
                "action": pop_and_push_to("div", tag="note", type="speaker"),
            },
            "SEG": {
                # segment :D
                "test": is_seg,
                "action": pop_and_push_to("u", "div", tag="seg", chunked=False),
            },
        },
    },
}


def get_chunks(filename: str) -> Generator[Chunk, Any, Any]:
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
    from pdfminer.converter import PDFLayoutAnalyzer
    from pdfminer.layout import LTChar

    line_threshold = 4.0
    header_y = 740.0  # above this is a header

    rm = PDFResourceManager()

    class CharCollector(PDFLayoutAnalyzer):
        def __init__(self):
            super().__init__(rm, laparams=None)
            self.chars: list[LTChar] = []

        def receive_layout(self, ltpage: Any):
            def walk(obj: Any):
                for item in obj:
                    if isinstance(item, LTChar):
                        self.chars.append(item)
                    elif hasattr(item, "__iter__"):
                        walk(item)

            walk(ltpage)

    device = CharCollector()
    interpreter = PDFPageInterpreter(rm, device)

    with open(filename, "rb") as f:
        prev_run: PDFChunk | None = None
        pending_space = False
        for page_num, page in enumerate(PDFPage.get_pages(f)):
            device.chars = []
            interpreter.process_page(page)

            # this doc has no footnote refs, so we don't need a downward and upward treshold
            # so we just match the same for up and down
            lines: list[list[LTChar]] = []
            cur: list[LTChar] = []
            prev_y: float | None = None
            for ch in device.chars:
                if prev_y is not None and abs(ch.y0 - prev_y) > line_threshold:
                    lines.append(cur)
                    cur = []
                cur.append(ch)
                prev_y = ch.y0
            if cur:
                lines.append(cur)

            for line in lines:
                if not line or line[0].y0 > header_y:
                    continue  # header, just skip it
                text = "".join(c.get_text() for c in line)
                if not text.strip():
                    continue

                runs: list[dict] = []
                for c in line:
                    if (
                        runs
                        and runs[-1]["fontname"] == c.fontname
                        and runs[-1]["size"] == c.size
                    ):
                        runs[-1]["text"] += c.get_text()
                    else:
                        runs.append(
                            {
                                "text": c.get_text(),
                                "x0": c.x0,
                                "y0": c.y0,
                                "fontname": c.fontname,
                                "size": c.size,
                            }
                        )

                # this pdf actually has real spaces, so we just make space_before True for all lines but the first
                # it gets stripped in append() anyway so the actual space in the text won't matter
                for i, r in enumerate(runs):
                    if i == 0:
                        r["space_before"] = pending_space
                    else:
                        prev_r = runs[i - 1]
                        r["space_before"] = (
                            prev_r["text"][-1:].isspace() or r["text"][:1].isspace()
                        )
                pending_space = True

                run_chunks: list[PDFChunk] = []
                for r in runs:
                    prev_run = make_chunk(
                        text=r["text"],
                        x=r["x0"],
                        y=r["y0"],
                        font_name=r["fontname"],
                        size=r["size"],
                        previous=prev_run,
                        page_num=page_num,
                        space_before=r["space_before"],
                    )
                    run_chunks.append(prev_run)

                first = runs[0]
                line_chunk = make_chunk(
                    text=text,
                    x=first["x0"],
                    y=first["y0"],
                    runs=run_chunks,
                    page_num=page_num,
                )
                for run in run_chunks:
                    run.line_chunk = line_chunk

                yield from run_chunks
