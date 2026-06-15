# EXAMPLE config file
# for 7. seja zbora republik in pokrajin Skupščine SFRJ iz 26. decembra 1978
# starts with page 7
# works upto the appendixes
# pdfs are too big to go in this repo, so find them elsewhere


import re
import engine
from engine import (
    Chunk,
    WordChunk,
    make_chunk,
    pop_and_push_to,
    tag_is_on_top,
    pop_to,
    push,
    append,
)
from type_decs import Config
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx import Document
from docx.oxml.ns import qn

# if we visited time, it's the chairman's turn
visited_time = False


def ref_entry_action(chunk: WordChunk):
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
            **{"xml:id": f"#note{serialized}"},
            place="foot",
            n=serialized,
        )


def ref_append(chunk: WordChunk):
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


def speaker_action(chunks: Chunk):
    # only open a new speaker note if we're not already inside one
    # (a follow-up speaker paragraph just appends into the open note)
    if not tag_is_on_top("note", type="speaker"):
        pop_to("div")
        push("note", type="speaker")


# config explanation is in readme
CONFIG: Config = {
    "mode": "word",
    "alignments": {
        "center": {
            "SEJA_DECLARATION": {
                "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
                "test_run": lambda chunk: chunk.bold,  # bold baby
                "action": pop_and_push_to("div", tag="head", type="session"),
            },
            "TIME": {
                "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
                "test_run": lambda run: run.italic
                and not visited_time,  # italic my beloved
                "action": pop_and_push_to("div", tag="time"),
                # this should've been extracted into its own function with "global visited_time; visited_time = True"
                # but it's so small that this is acceptable (by my standards, at least)
                "after_append": lambda: globals().update({"visited_time": True}),
            },
            "CHAIRMAN": {
                "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
                "test_run": lambda chunk: tag_is_on_top("time")
                or tag_is_on_top(
                    "note", type="chairman"
                ),  # if we're in time, next is chairman
                "action": pop_and_push_to("div", tag="note", type="chairman"),
            },
            "SEJA_SECTION": {
                # isn't bolded or italic, so it's a 'del seje', or something
                "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
                "test_run": "_else",
                "action": pop_and_push_to("div", tag="head", type="sessionSection"),
            },
        },
        "_else": {
            # if we're not centered anymore, everything that depends on visited_time has been visited
            # so we clear it for future time declarations
            # this is fragile, will need to change visited_time
            "run_immediate": lambda: globals().update({"visited_time": False}),
            "REFERENCE_ENTRY": {
                # opomba :O
                # a footnote *definition* leads its paragraph with the superscript
                # marker. an inline reference inside body text is a superscript run
                # that ISN'T the paragraph's first run - that one falls through to
                # append(), which turns it into an inline <ref>.
                "test_run": lambda chunk: (
                    chunk.run.font.superscript  # is a superscript
                    and chunk.run._element
                    is chunk.paragraph.runs[
                        0
                    ]._element  # and is the first element in the paragraph
                ),
                "action": ref_entry_action,
                "append_func": ref_append,
            },
            "GENERIC_NOTE": {
                # a note about something that happened, such as "seja se je zakljucila"
                "test_run": lambda chunk: (
                    chunk.paragraph.alignment == WD_PARAGRAPH_ALIGNMENT.CENTER
                    and chunk.italic
                ),
                "action": generic_note_action,
                "append_func": lambda chunk: append(
                    chunk, should_annotate=["REFERENCE"]
                ),
            },
            "SPEAKER": {
                # this is REALLY BAD detection but wtf am i supposed to do
                "test_run": lambda chunk: (
                    chunk.paragraph.paragraph_format.first_line_indent == 0
                    and chunk.text[:3].isupper()
                ),
                "action": speaker_action,
            },
            "SEG": {
                # indented - start of odstavek
                "test_run": lambda chunk: chunk.paragraph.paragraph_format.first_line_indent
                != 0
                and chunk.paragraph.text.startswith(chunk.text),
                "action": pop_and_push_to(
                    "u",
                    "div",
                    tag="seg",
                    chunked=False,
                ),  # close any open seg; land on the enclosing <u>
            },
        },
    },
}


# get frames of document
# this can be changed if you need to parse something other than a doc
# it should return a dict of {(x, y, w, h): [para1, para2, para3]}
# it is still locked to Paragraphs
# i'll change this api later
# more in readme
def get_frames(filename: str):
    doc = Document(filename)

    for para in doc.paragraphs:
        p = para._p

        pPr = p.find(qn("w:pPr"))
        if pPr is None:
            continue
        framePr = pPr.find(qn("w:framePr"))
        if framePr is None:
            continue

        # x, y, w, h = get_para_xywh(para)

        for run in para.runs:
            print(f"----- RUNNNNNNN ------: {run.text}")
            yield make_chunk(run, para)


# for pdf:
def get_frames_pdf(filename: str):
    from pypdf import PdfReader

    pdf = PdfReader(filename)

    # can't yield because stupid extract text function

    chunks: list[Chunk] = []

    for page in pdf.pages:
        page.extract_text(
            visitor_text=lambda text, cm, tm, font_dict, font_size: chunks.append(
                make_chunk(
                    text=text, cm=cm, tm=tm, font_dict=font_dict, font_size=font_size
                )
            )
        )

    return chunks
