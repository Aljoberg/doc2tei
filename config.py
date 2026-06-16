# this is a TEST CONFIG for the seventh parlamentary session "zbora republik in pokrajin"
# it works on a PDF FILE
# refer to examples/zbor-republik-in-pokrajin/config_word.py for the .docx version
# examples/zbor-republik-in-pokrajin/config_pdf.py and config.py in the project root are symlinks


import re
from typing import Any, Generator
import engine
from engine import (
    Chunk,
    PDFChunk,
    WordChunk,
    make_chunk,
    pop_and_push_to,
    tag,
    tag_is_on_top,
    pop_to,
    push,
    append,
)
from type_decs import PDFConfig, PDFCosmeticAnnotations


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
    # we don't append the ref's number as it's in the @n attribute
    # but we need to strip the leading space of the next chunk so
    # we do that
    engine.lstrip_next = True


def generic_note_action(chunk: Chunk):
    # push <note> & <hi rend="italic">
    pop_to("div")
    push("note")
    push("hi", rend="italic")


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
    "mode": "pdf",
    "alignments": {
        # all alignments
        # any other values are only used in .docx mode
        "any": {
            "run_immediate": lambda: setattr(
                engine, "is_first_run", True
            ),  # we set the first run to be True so spaces don't get appended
            # i should probably rework this first run thing
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
                "test": lambda chunk: 590 < chunk.y < 600
                and chunk.italic,  # if we're at the coordinates and italic
                "action": pop_and_push_to("div", tag="time"),
            },
            "CHAIRMAN": {
                "test": lambda chunk: (
                    tag_is_on_top("time")
                    or tag_is_on_top(
                        "note", type="chairman"
                    )  # if the last tag was time or we're already in chairman
                )
                and not chunk.italic  # and not italic (otherwise we'd catch time)
                and chunk.x > 174,  # and indented more than a paragraph
                "action": pop_and_push_to("div", tag="note", type="chairman"),
            },
            "SEJA_SECTION": {
                "test": lambda chunk: chunk.text.isupper()  # if we're all caps
                and tag_is_on_top("div", type="debateSection")  # and outside everything
                and 194 < chunk.x < 360,  # and at the coords
                "action": pop_and_push_to("div", tag="head", type="sessionSection"),
            },
            # --- not centered ---
            "REFERENCE_ENTRY": {
                "test": lambda chunk: chunk.font_size == 7.0  # if we're smol
                and chunk.text.strip().isdigit(),  # and a digit
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
                "test": lambda chunk: chunk.x < 55  # not indented
                and leading_caps(chunk.text)
                >= 3,  # and more than 3 uppercase letters at the start
                "action": pop_and_push_to("div", tag="note", type="speaker"),
            },
            "SEG": {
                # starts a bit indented
                "test": lambda chunk: 59.5 < chunk.x < 60,
                "action": pop_and_push_to(
                    "u", "div", tag="seg", chunked=False
                ),  # each paragraph is its own chunk and they repeat, so we just kill the previous seg by setting chunked=False
            },
        },
    },
}

# for pdf:
# this guy thinks he can write docs


# a searchable pdf is just a soup of positioned characters - there are no words,
# lines or paragraphs. get_frames only does the document-agnostic part: it
# reassembles the characters into visual lines (the "frames"), reconstructing the
# spacing (a gap wider than `threshold` between two chars is a space, and every
# line ends with one so consecutive lines don't fuse). every line is yielded as a
# single chunk whose `.text` is the whole line and whose `.x`/`.y` mark where it
# starts - all the document-specific judgement (what is a speaker, an indent, a
# note ...) lives in the config rules. the per-font pieces of the line are kept in
# `.runs` so it can still be appended with its italic/bold/reference formatting.


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
    # raw pdfminer.six instead of pdfplumber: pdfplumber wraps every glyph in a
    # ~25-key dict and runs resolve_all/process_attr on each (~1.4x overhead),
    # but get_chunks only needs x/y/font/size and regroups the chars itself. so
    # we read LTChars straight off each page in content-stream order - which is
    # exactly what pdfplumber's default (laparams=None) produced; verified to be
    # an identical char sequence, so the magic coordinates in the rules still hold.
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
    from pdfminer.converter import PDFLayoutAnalyzer
    from pdfminer.layout import LTChar

    threshold = 1.5  # space treshold
    line_treshold = 2  # ......line treshold

    rm = PDFResourceManager()

    class CharCollector(PDFLayoutAnalyzer):
        # collects every glyph on a page as a pdfplumber-style char dict
        def __init__(self):
            super().__init__(rm, laparams=None)
            self.chars: list[dict] = []

        def receive_layout(self, ltpage: Any):
            def walk(obj: Any):
                for item in obj:
                    if isinstance(item, LTChar):
                        self.chars.append(
                            {
                                "text": item.get_text(),
                                "x0": item.x0,
                                "x1": item.x1,
                                "y0": item.y0,
                                "fontname": item.fontname,
                                "size": item.size,
                            }
                        )
                    elif hasattr(item, "__iter__"):  # LTFigure etc. - recurse in
                        walk(item)

            walk(ltpage)

    device = CharCollector()
    interpreter = PDFPageInterpreter(rm, device)

    with open(filename, "rb") as f:
        for page in PDFPage.get_pages(f):
            device.chars = []
            interpreter.process_page(page)
            page_chars = device.chars

            # group into lines
            lines: list[list[dict]] = []
            cur: list[dict] = []
            prev_y = None
            for char in page_chars:
                if prev_y is not None and abs(char["y0"] - prev_y) > line_treshold:
                    lines.append(cur)
                    cur = []
                cur.append(char)
                prev_y = char["y0"]
            if cur:
                lines.append(cur)

            for line in lines:
                # group as much of the line as we can into runs
                runs: list[dict] = []
                prev: dict | None = None
                for char in line:
                    gap = bool(prev) and char["x0"] - prev["x1"] > threshold
                    can_be_grouped = (
                        runs  # there's at least one run
                        and runs[-1]["fontname"]
                        == char["fontname"]  # and it's the same font
                        and runs[-1]["size"] == char["size"]  # and the same size
                    )
                    if gap and runs:
                        runs[-1]["text"] += " "
                    if can_be_grouped:
                        # if the text is the same, append it to the previous run directly
                        runs[-1]["text"] += char["text"]
                    else:
                        # if not, make a new run with the new stuff
                        runs.append(
                            {
                                "text": char["text"],
                                "x0": char["x0"],
                                "y0": char["y0"],
                                "fontname": char["fontname"],
                                "size": char["size"],
                            }
                        )
                    prev = char
                runs[-1]["text"] += " "  # trailing space keeps lines apart

                # actually make the chunks we grouped
                run_chunks = [
                    make_chunk(
                        text=r["text"],
                        x=r["x0"],
                        y=r["y0"],
                        font_name=r["fontname"],
                        size=r["size"],
                    )
                    for r in runs
                ]

                first = runs[0]
                line_chunk = make_chunk(
                    text="".join(r["text"] for r in runs),
                    x=first["x0"],
                    y=first["y0"],
                    runs=run_chunks,
                )
                for run in run_chunks:
                    run.line_chunk = line_chunk

                engine.is_first_run = True
                yield from run_chunks
