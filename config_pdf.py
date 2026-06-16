# THIS IS A TEST AND DOESN'T WORK YET
# first config to try out pdf as an input


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

# if we visited time, it's the chairman's turn
visited_time = False


def ref_entry_action(chunk: PDFChunk):
    # we need to remove all nonalphanumeric characters from the note
    # i mean, we probably don't *have* to, but if it begins with a hash, we should remove it
    note_num = chunk.text.strip()
    serialized = re.sub(r"[^a-zA-Z0-9]", "", note_num)
    # this repeats through the whole doc - if texts are chunked (such as new lines or formatting changes), they will be split into multiple runs
    # and we only need to create ONE note tag
    # or we'll end up with 10 note tags, each having a section of the content
    # this isn't as significant in the .docx, but it is in the .pdf
    # still though, this check should be used in all pushes (except for the ones you're *certain* are not chunked, and even there it doesn't hurt)
    # i should probably integrate this by default
    # more info in readme (might cut this comment when i write it)
    if not tag_is_on_top("note", place="foot", n=serialized):
        # not already inside this footnote, open it
        pop_to("u", "div")
        push(
            "note",
            attribs={"xml:id": f"#note{serialized}"},
            place="foot",
            n=serialized,
        )


def ref_append(chunk: PDFChunk):
    # the marker run (e.g. "1") is recorded as the note's @n by ref_entry_action,
    # so we don't append it as text. we also don't bulk-append the rest of the
    # paragraph here - runs aren't guaranteed to be whole, so the footnote body
    # runs are appended individually as they're parsed, landing inside the note
    # that ref_entry_action left open on top of the stack.
    engine.lstrip_next = True


def generic_note_action(chunk: Chunk):
    # push <note> & <hi rend="italic">
    pop_to("div")  # prolly should add tests like is_tag_on_top
    push("note")
    push("hi", rend="italic")  # i guess


def speaker_action(chunk: Chunk):
    # only open a new speaker note if we're not already inside one
    # (a follow-up speaker line just appends into the open note)
    if not tag_is_on_top("note", type="speaker"):
        pop_to("div")
        push("note", type="speaker")


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
            "ref", target=f'#note{re.sub(r"[^a-zA-Z0-9]", "", chunk.text.strip())}'
        ),
    },
}

# config explanation is in readme
CONFIG: PDFConfig = {
    "mode": "pdf",
    "alignments": {
        "any": {
            "SEJA_DECLARATION": {
                "test_run": lambda chunk: (
                    605 < chunk.y < 610 or tag_is_on_top("head", type="session")
                )
                and chunk.bold,
                "action": pop_and_push_to("div", tag="head", type="session"),
            },
            "TIME": {
                "test_run": lambda chunk: 590 < chunk.y < 600 and chunk.italic,
                "action": pop_and_push_to("div", tag="time"),
                "after_append": lambda: globals().update({"visited_time": True}),
            },
            "CHAIRMAN": {
                "test_run": lambda chunk: (
                    tag_is_on_top("time") or tag_is_on_top("note", type="chairman")
                )
                and not chunk.italic
                and chunk.x > 174,
                "action": pop_and_push_to("div", tag="note", type="chairman"),
            },
            "SEJA_SECTION": {
                "test_run": lambda chunk: chunk.text.isupper()
                and tag_is_on_top("div", type="debateSection")
                and 194 < chunk.x < 360,
                "action": pop_and_push_to("div", tag="head", type="sessionSection"),
            },
            # ---
            "run_immediate": lambda: globals().update({"visited_time": False})
            or setattr(engine, "is_first_run", True),
            # ---
            "REFERENCE_ENTRY": {
                "test_run": lambda chunk: chunk.font_size == 7.0
                and chunk.text.strip().isdigit(),
                "action": ref_entry_action,
                "append_func": ref_append,
            },
            "GENERIC_NOTE": {
                # guard against whitespace: a lone reconstructed space can land in
                # this x-band with the italic font of the line it trails, which
                # would otherwise spuriously open a note (e.g. after the <time>)
                "test_run": lambda chunk: 323 < chunk.x < 457
                and chunk.line_chunk.italic
                and chunk.text.strip() != "",
                "action": generic_note_action,
                "append_func": lambda chunk: append(
                    chunk, should_annotate=["REFERENCE"]
                ),
            },
            "SPEAKER": {
                # a left-margin line (x < 55, i.e. not indented) that opens with a
                # long run of capitals is a speaker heading, e.g.
                # "PREDSEDNIK ZORAN POLIČ (SR Slovenija):"
                "test_run": lambda chunk: chunk.x < 55
                and leading_caps(chunk.text) >= 3,
                "action": speaker_action,
            },
            "SEG": {
                # a paragraph starts with an indented first line (~x=60), while its
                # continuation lines sit at the margin (~x=44) and just fall through
                # to append into the open <seg>. chunked=False => one <seg> per
                # paragraph.
                "test_run": lambda chunk: 59 < chunk.x < 74,
                "action": pop_and_push_to("u", "div", tag="seg", chunked=False),
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
def get_frames(filename: str) -> Generator[Chunk, Any, Any]:
    import pdfplumber

    threshold = 1.5
    line_treshold = 2

    with pdfplumber.open(filename) as pdf:
        page = pdf.pages[0]

        # group into lines
        lines: list[list[dict]] = []
        cur: list[dict] = []
        prev_y = None
        for char in page.chars:
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
                    runs
                    and runs[-1]["fontname"]
                    == char["fontname"]  # if it's the same font
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
            print("le run chunks")
            print(run_chunks)
            first = runs[0]
            # i should probably make a LineChunk or something, rather than separating the two by the existence of "runs" on the chunk
            # anyway, make a line chunk with info about the whole line
            # this is useful if we wanna check leading caps or things like that
            # might make it just yield run_chunks directly, idk what's more intuitive
            # since "lines" are kinda magic as well, they're just checks whether the y value changed by more than 2
            line_chunk = make_chunk(
                text="".join(r["text"] for r in runs),
                x=first["x0"],
                y=first["y0"],
                runs=run_chunks,
            )
            for run in run_chunks:
                run.line_chunk = line_chunk
            # cast(PDFChunk, line_chunk).runs = cast("list[PDFChunk]", run_chunks)
            engine.is_first_run = True
            # yield line_chunk
            yield from run_chunks  # TODO ask robert about design
