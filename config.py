# this is a TEST CONFIG for the seventh parlamentary session "zbora republik in pokrajin"
# it works on a PDF FILE
# refer to examples/zbor-republik-in-pokrajin/config_word.py for the .docx version
# examples/zbor-republik-in-pokrajin/config_pdf.py and config.py in the project root are symlinks


import functools
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Generator
from engine import (
    Chunk,
    PDFChunk,
    WordChunk,
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


@functools.wraps(print)
def log(*args, **kwargs):
    if CONFIG["debug"]:
        print(*args, **kwargs)


def ref_entry_action(chunk: PDFChunk):
    # we need to remove all nonalphanumeric characters from the note
    # i mean, we probably don't *have* to, but if it begins with a hash, we should remove it
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


def ref_append(chunk: PDFChunk):
    # we don't append the ref number since it's added to the @n attribute instead
    pass


def generic_note_action(chunk: Chunk):
    # push <note> & <hi rend="italic">
    pop_to("div")
    push("note")
    push("hi", rend="italic")


def header_test(chunk: PDFChunk):
    # kill the first 3 elements on a page

    for i in range(1, 4):
        prev = nth_previous(chunk, i)
        if prev and prev.page_num != chunk.page_num:
            return True

    return False


utterance_speaker_mapping: dict[str, str] = (
    {}
)  # {"#SomeName": "Some Name (affiliation)"} -- maps serialized and unserialized - this is probably kind of weird but we'll ball


def speaker_to_utterance(popped: StackEntry):
    # runs for every pop
    # The utterance's @who needs to have the whole speaker's info (all chunks, at least)
    # and we can't go ahead in chunks, so we have to wait for <note type="speaker"> to do its recognision
    # but only after it's popped (a seg starts, for example), we have every child of it
    # so that's how we get the who representation
    if popped.element.tag == "note" and popped.element.attrib.get("type") == "speaker":
        text = "".join(
            (i.text or "") if isinstance(i, ET.Element) else i for i in popped.children
        )  # kind of a weird way of getting text, but it's fine alright we're gonna pretend there's no nesting
        name_surname = re.sub(
            r"^pre\S+\s+|\s*(?:\(|,|:).*$", "", text, flags=re.IGNORECASE
        )  # remove "PREDSEDNIK" and everything after a parenthesis or a comma
        serialized = "#" + "".join(
            word.capitalize() for word in name_surname.split()
        )  # le pascal case

        if (
            serialized not in utterance_speaker_mapping
        ):  # the first occurence is probably the most descriptive
            utterance_speaker_mapping[serialized] = text

        push(
            "u",
            who=serialized,
        )


def leading_caps(text: str) -> int:
    # counts leading capitalized letters
    # :O
    n = 0
    for ch in text:
        if ch.islower():
            break
        if ch.isupper():
            n += 1
    return n


def is_seg(chunk: PDFChunk) -> bool:
    # are we in a seg?
    # if we're not at line start, no
    if chunk is not chunk.line_chunk.runs[0]:
        return False

    # if the difference in x or difference in y is more than... these magic numbers, we probably are, but need to check font size
    prev = chunk.previous
    if prev is None or abs(chunk.x - prev.x) > 240 or abs(chunk.y - prev.y) > 230:
        # should maybe add page_num instead of these
        return 8.9 < chunk.font_size < 10.1  # column / page top -> always body

    # because of page misalignment (:D) we have to do whatever this is
    left = 48 if chunk.page_num <= 2 else 41

    # if we're indented, correct font size and not in a note
    indented = left < chunk.x < 64 or 287 < chunk.x < 303
    return indented and (
        9.0 <= chunk.font_size < 10.1 and not tag_is_on_top("note", place="foot")
    )


def nth_previous(chunk: PDFChunk, n: int) -> PDFChunk | None:
    # returns nth previous chunk
    # or None if we run out of them
    cur: PDFChunk | None = chunk
    for _ in range(n):
        if cur is None:
            return None
        cur = cur.previous
    return cur


def is_page_top(chunk: PDFChunk) -> bool:
    # checks if chunk is on page top
    # skips header (still magic values, to be fixed)
    # and junk chunks
    prev = chunk.previous
    while prev is not None and (724 < prev.y < 742 or len(prev.text.strip()) <= 2):
        prev = prev.previous
    return prev is not None and prev.page_num != chunk.page_num


def on_end():
    # write the speaker utterance mapping somewhere
    with open(
        "out/speaker_utterance.json", "w", encoding="utf-8"
    ) as f:  # TODO unhardcode
        json.dump(utterance_speaker_mapping, f, ensure_ascii=False)


# cosmetic annotations -- things that can appear inside anything and do not alter layout or structure of the document
# find the pyramid on https://excalidraw.com/#json=s5d5fPvL0PFW2FxKaYbm4,XXCL3rpalK3FEg9lBwiKHQ
COSMETIC_ANNOTATIONS: PDFCosmeticAnnotations = {
    "ITALIC": {
        "test": lambda chunk: chunk.italic,  # if the chunk is italic
        # we declare the tag emph
        # this will be checked whenever the tag needs to be removed
        # and this tag will also be pushed when the test succeeds
        "tag": tag("emph"),
    },
    "BOLD": {"test": lambda chunk: chunk.bold, "tag": tag("hi", rend="bold")},
    "REFERENCE": {
        "test": lambda chunk: (
            chunk.run.font.superscript  # if it's a superscript - that only exists in .docx though
            if isinstance(chunk, WordChunk)
            else chunk.font_size == 7.0  # or if the font size is 7, if we're in a pdf
        ),
        "tag": tag("ref"),  # used for removing
        "append_func": lambda chunk: push(
            "ref",
            target=f'#note{re.sub(r"[^a-zA-Z0-9]", "", chunk.text.strip())}',  # we need to push it ourselves, since the tag is dynamic (note target)
        ),
    },
}

# le config
CONFIG: PDFConfig = {
    "debug": False,
    "mode": "pdf",
    "on_pop": speaker_to_utterance,
    "on_end": on_end,
    "alignments": {
        # all alignments
        # any other values are only used in .docx mode
        "any": {
            "HEADER": {"test": header_test, "append_func": lambda chunk: None},  # :3
            # --- centered ---
            "SEJA_DECLARATION": {
                "test": lambda chunk: (
                    605 < chunk.y < 610
                    or tag_is_on_top(
                        "head", type="session"
                    )  # if we're at the coordinates or already in the head
                )
                and chunk.bold,  # and bold
                "action": pop_and_push_to("div", tag="head", type="session"),
            },
            "TIME": {
                "test": lambda chunk: chunk.italic  # time is italic
                and (
                    590 < chunk.y < 600  # initial coords
                    # or the "Začetek ob" that follows a session section
                    or tag_is_on_top("head", type="sessionSection")
                ),
                "action": pop_and_push_to("div", tag="time"),
            },
            "CHAIRMAN": {
                "test": lambda chunk: (
                    chunk.text.strip().startswith("PREDSEDUJE")
                    or tag_is_on_top("time")
                    or tag_is_on_top(
                        "note", type="chairman"
                    )  # if the last tag was time or we're already in chairman
                )
                and not chunk.italic  # and not italic (otherwise we'd catch time)
                and chunk.x > 174,  # and indented more than a paragraph
                "action": pop_and_push_to("div", tag="note", type="chairman"),
            },
            "SEJA_SECTION": {
                # "nadaljevanje seje" or stuff like that
                "test": lambda chunk: (
                    chunk.text.isupper()  # all caps
                    and 194 < chunk.x < 360  # centered
                    and is_page_top(chunk)  # first body line on a fresh page
                ),
                "action": pop_and_push_to("div", tag="head", type="sessionSection"),
            },
            # --- not centered ---
            "REFERENCE_ENTRY": {
                "test": lambda chunk: (
                    chunk.font_size == 7.0 or 6.9 < chunk.font_size < 7.0
                )  # if we're smol
                and chunk.text.strip().isdigit()  # and a digit
                and chunk
                is chunk.line_chunk.runs[0],  # and the first thing in the line
                "action": ref_entry_action,
                "append_func": ref_append,
            },
            "GENERIC_NOTE": {
                "test": lambda chunk: 323 < chunk.x < 457  # if we're at the coords
                and chunk.line_chunk.italic  # and the WHOLE LINE is italic
                and chunk.text.strip()
                != "",  # and we're not empty (i should just kill the chunk in get_frames, probably)
                "action": generic_note_action,
                "append_func": lambda chunk: append(
                    chunk,
                    should_annotate=["REFERENCE"],  # do not let emph do the emphing
                ),
            },
            "SPEAKER": {
                "test": lambda chunk: (
                    0 < chunk.x < 55 or 271 < chunk.x < 293
                )  # not indented
                and leading_caps(chunk.text)
                >= 5,  # and more than 5 uppercase letters at the start
                # this used to be 3 but "SFRJ" appeared at the start
                # yippie
                "action": pop_and_push_to("div", tag="note", type="speaker"),
            },
            "SEG": {
                # segment
                "test": is_seg,
                "action": pop_and_push_to(
                    "u", "div", tag="seg", chunked=False
                ),  # each paragraph is its own chunk and they repeat, so we just kill the previous seg by setting chunked=False
            },
        },
    },
}


# since a searchable pdf is just a soup of characters at a specific x & y value
# we need to make logic out of it
# this function figures out where spaces are (if the spacing is above the treshold)
# and assembles lines (we don't really need lines, but they're really helpful to the end user)
# then it takes apart each line and tries to group as much text as it can (all of it, until a different size or font is hit)
# so we end up with chunks with text that has the same properties, or is broken by a line
# those chunks get appended to .runs of a LINE CHUNK - a chunk that contains all child chunks on that line
# each child chunk has a .line_chunk property that carries the line chunk it's in
# then it just yields each child chunk
def get_chunks(filename: str) -> Generator[Chunk, Any, Any]:
    # we use pdfminer to extract things more efficiently
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
    from pdfminer.converter import PDFLayoutAnalyzer
    from pdfminer.layout import LTChar

    threshold = 1.7  # space treshold
    line_treshold = 4.871  # ......line treshold

    rm = PDFResourceManager()

    class CharCollector(PDFLayoutAnalyzer):
        # pdfminer stuff
        def __init__(self):
            super().__init__(rm, laparams=None)
            self.chars: list[LTChar] = []

        def receive_layout(self, ltpage: Any):
            def walk(obj: Any):
                for item in obj:
                    if isinstance(item, LTChar):
                        self.chars.append(item)
                    elif hasattr(item, "__iter__"):  # LTFigure etc
                        walk(item)

            walk(ltpage)

    device = CharCollector()
    interpreter = PDFPageInterpreter(rm, device)

    with open(filename, "rb") as f:
        prev_run: PDFChunk | None = None
        pending_space = False  # whether to add a space to the next line's first chunk
        for page_num, page in enumerate(PDFPage.get_pages(f)):
            device.chars = []
            interpreter.process_page(page)
            page_chars = device.chars

            # group into lines
            lines: list[list[LTChar]] = []
            cur: list[LTChar] = []
            prev_y = None
            for char in page_chars:
                if prev_y is not None:
                    # just some logs I found useful when debugging
                    # I don't have time for a better debug utility so you'll have to work with this
                    log(
                        f"-- line check --: {char.get_text()=} {abs(char.y0 - prev_y)=}, cur={''.join(i.get_text() for i in cur)}"
                    )
                    log(f"{char=}")
                if prev_y is not None and (
                    prev_y - char.y0 > line_treshold
                    or abs(prev_y - char.y0) > line_treshold * 5
                ):
                    lines.append(cur)
                    cur = []
                cur.append(char)
                prev_y = char.y0
            if cur:
                lines.append(cur)

            for line in lines:
                # group as much of the line as we can into runs
                runs: list[dict] = []
                prev: LTChar | None = None
                broke = False  # whether a literal space cut a word (deljaj)
                for char in line:
                    if prev:
                        # again, some cool debug logs
                        log(
                            f"-- char --: x difference of char and prev is {char.x0 - prev.x1}, {char.get_text()=}, "
                            f"{prev.get_text()=}, text={''.join(i.get_text() for i in line)}",
                        )
                    gap = bool(prev) and char.x0 - prev.x1 > threshold
                    if char.get_text() == " ":  # deljaj
                        broke = True
                        break  # last thing in the line anyway
                    can_be_grouped = (
                        runs  # there's at least one run
                        and runs[-1]["fontname"]
                        == char.fontname  # and it's the same font
                        and runs[-1]["size"] == char.size  # and the same size
                        and (
                            abs(prev.x0 - char.x0) < 30 if prev else True
                        )  # we won't bridge if there's too much of a gap
                    )
                    if can_be_grouped:
                        if gap:
                            runs[-1]["text"] += " "
                        runs[-1]["text"] += char.get_text()
                    else:
                        # make a new run
                        # if there's already a run, that means we need to add a space if there's a gap
                        # otherwise we add a space if the last line was continued by this run
                        runs.append(
                            {
                                "text": char.get_text(),
                                "x0": char.x0,
                                "y0": char.y0,
                                "fontname": char.fontname,
                                "size": char.size,
                                "space_before": gap if runs else pending_space,
                            }
                        )
                    prev = char

                # a line that ran to its end (no literal-space break) is kept apart
                # from the next line's first run by a separator
                pending_space = not broke

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
                    text="".join(r["text"] for r in runs),
                    x=first["x0"],
                    y=first["y0"],
                    runs=run_chunks,
                    page_num=page_num,
                )
                for run in run_chunks:
                    run.line_chunk = line_chunk

                yield from run_chunks
