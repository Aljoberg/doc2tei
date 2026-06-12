# EXAMPLE config file for 7. seja zbora republik in pokrajin Skupščine SFRJ iz 26. decembra 1978
# starts with page 7
# works upto the appendixes
# pdfs are too big to go in this repo, so find them elsewhere


from collections import OrderedDict
import re
from docx.text.paragraph import Paragraph
from engine import pop_and_push_to, tag_is_on_top, pop_to, push, append
from type_decs import (
    Config,
    RuleGroup,
)
from docx.text.paragraph import Paragraph
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx import Document
from docx.oxml.ns import qn

# if we visited time, it's the chairman's turn
visited_time = False


def ref_entry_action(x: int, y: int, w: int, h: int, para: Paragraph):
    # we need to remove all nonalphanumeric characters from the note
    # i mean, we probably don't *have* to, but if it begins with a hash, we should remove it
    note_num = para.runs[0].text.strip()
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


def ref_append(x: int, y: int, w: int, h: int, para: Paragraph, para_idx: int):
    para.runs[1].text = para.runs[1].text.strip()  # remove the space after note
    append(*para.runs[1:], para_idx=para_idx)


def generic_note_action(x: int, y: int, w: int, h: int, para: Paragraph):
    # push <note> & <hi rend="italic">
    pop_to("div")  # prolly should add tests like is_tag_on_top
    push("note")
    push("hi", rend="italic")  # i guess


def speaker_action(x: int, y: int, w: int, h: int, para: Paragraph):
    # only open a new speaker note if we're not already inside one
    # (a follow-up speaker paragraph just appends into the open note)
    if not tag_is_on_top("note", type="speaker"):
        pop_to("div")
        push("note", type="speaker")


# config explanation is in readme
config: Config = {
    "center": {
        "CHAIRMAN": {
            "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
            "test": lambda x, y, w, h, para: tag_is_on_top(
                "time"
            ),  # if we're in time, next is chairman
            "action": pop_and_push_to("div", tag="note", type="chairman"),
        },
        "SEJA_DECLARATION": {
            "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
            "test_run": lambda x, y, w, h, run: run.bold,  # bold baby
            "action": pop_and_push_to("div", tag="head", type="session"),
        },
        "TIME": {
            "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
            "test_run": lambda x, y, w, h, run: run.italic
            and not visited_time,  # italic my beloved
            "action": pop_and_push_to("div", tag="time"),
            # this should've been extracted into its own function with "global visited_time; visited_time = True"
            # but it's so small that this is acceptable (by my standards, at least)
            "after_push": lambda: globals().update({"visited_time": True}),
        },
        "SEJA_SECTION": {
            # isn't bolded or italic, so it's a 'del seje', or something
            "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
            "test_run": "_else",
            "action": pop_and_push_to("div", tag="head", type="sessionSection"),
        },
        "RANDOM_TEXT": {
            # random text
            # in case things are chunked, or something
            "test": lambda x, y, w, h, para: (
                visited_time or para.alignment != WD_PARAGRAPH_ALIGNMENT.CENTER
            ),
        },
    },
    "_else": {
        # if we're not centered anymore, everything that depends on visited_time has been visited
        # so we clear it for future time declarations
        # this is fragile, will need to change visited_time
        "run_immediate": lambda: globals().update({"visited_time": False}),
        "REFERENCE_ENTRY": {
            # opomba :O
            "test": lambda x, y, w, h, para: para.runs[0].font.superscript,
            "action": ref_entry_action,
            "append_func": ref_append,
        },
        "GENERIC_NOTE": {
            # a note about something that happened, such as "seja se je zakljucila"
            "test": lambda x, y, w, h, para: (
                para.alignment == WD_PARAGRAPH_ALIGNMENT.CENTER
                and all(r.italic for r in para.runs)
            ),
            "action": generic_note_action,
            "append_func": lambda x, y, w, h, para, para_idx: append(
                *para.runs, para_idx=para_idx, should_annotate=["REFERENCE"]
            ),
        },
        "SPEAKER": {
            # this is REALLY BAD detection but wtf am i supposed to do
            "test": lambda x, y, w, h, para: (
                para.paragraph_format.first_line_indent == 0
                and para.runs[0].text[:3].isupper()
            ),
            "action": speaker_action,
        },
        "SEG": {
            # indented - start of odstavek
            "test": "_else",
            "action": pop_and_push_to(
                "u", "div", tag="seg"
            ),  # close any open seg; land on the enclosing <u>
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

    frames = OrderedDict[tuple[int, int, int, int], list[Paragraph]]()

    for para in doc.paragraphs:
        p = para._p

        pPr = p.find(qn("w:pPr"))
        if pPr is None:
            continue

        framePr = pPr.find(qn("w:framePr"))
        if framePr is None:
            continue

        frame_key = (
            int(framePr.get(qn("w:x")) or 0),
            int(framePr.get(qn("w:y")) or 0),
            int(framePr.get(qn("w:w")) or 0),
            int(framePr.get(qn("w:h")) or 0),
        )

        if frame_key not in frames:
            frames[frame_key] = []

        frames[frame_key].append(para)

    return frames
