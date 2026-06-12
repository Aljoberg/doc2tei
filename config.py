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
from docx.document import Document
from docx.oxml.ns import qn


def ref_entry_action(x: int, y: int, w: int, h: int, para: Paragraph):
    note_num = para.runs[0].text.strip()
    serialized = re.sub(r"[^a-zA-Z0-9]", "", note_num)
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
    pop_to("div")  # prolly should add tests like is_tag_on_top
    push("note")
    push("hi", rend="italic")  # i guess


def speaker_action(x: int, y: int, w: int, h: int, para: Paragraph):
    # we love runs
    if not tag_is_on_top("note", type="speaker"):
        pop_to("div")
        push("note", type="speaker")


def contents_action(x: int, y: int, w: int, h: int, para: Paragraph):
    # the SADRZAJ frame - every paragraph of it lands in one note
    if not tag_is_on_top("note", type="contents"):
        pop_to("div")
        push("note", type="contents")


def is_session_title(x: int, y: int, w: int, h: int, para: Paragraph):
    # the boxed title block: a small wide frame near the top of the page with
    # all-caps lines; body column frames in the same zone are way taller, and
    # appendix headings (IZVJEŠTAJ & co.) sit lower on the page
    # NOTE: can't test run.font.size here - it's inherited from the style
    # in this doc, so it's None on (most of) the runs
    return y < 2000 and h < 3000 and w > 2900 and para.text.strip().isupper()


# the SADRZAJ / PREDSEDAVAO / "Početak u ..." blocks that open a session;
# usually in the right column, but joint sessions center them across the page
session_front_rules: RuleGroup = {
    "CHAIRMAN": {
        # PREDSEDAVAO / PREDSEDNIK / NIKOLA SEKULIC - one centered all-caps
        # paragraph in a small frame (only the one in session 6 is also bold,
        # and the OCR sneaks lowercase letters into some names, so anchor on
        # the PREDSEDAVAO/PREDSEDAVALA/PREDSEDAVALI prefix instead)
        "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
        "test": lambda x, y, w, h, para: h < 1500
        and (para.text.strip().startswith("PREDSEDAVA") or para.text.strip().isupper()),
        "action": pop_and_push_to("div", tag="note", type="chairman"),
    },
    "TIME": {
        # "Početak u 9 č 10 min" - italic, shares the chairman's frame
        # (some have a stray bold run in the middle, hence the prefix test)
        "test": lambda x, y, w, h, para: h < 1500
        and (
            para.text.strip().startswith("Početak") or all(r.italic for r in para.runs)
        ),
        "action": pop_and_push_to("div", tag="time"),
    },
    "CONTENTS": {
        # opened by the SADRZAJ heading, keeps eating paragraphs until
        # some other rule (chairman, a title, ...) pops the note
        "test": lambda x, y, w, h, para: (
            para.text.strip().upper().startswith("SADR")
            or tag_is_on_top("note", type="contents")
        ),
        "action": contents_action,
    },
}

# rules for the two-column debate body, shared by both zones
body_rules: RuleGroup = {
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
        # "Predsednik Nikola Sekulić:" / "Janez Vipotnik:"
        # a short paragraph with a bold run that ends with a colon
        # (no first_line_indent to test on in this doc, it's all style-based)
        "test": lambda x, y, w, h, para: (
            any(r.bold for r in para.runs)
            and para.text.strip().endswith(":")
            and len(para.text.strip()) < 100
        ),
        "action": speaker_action,
    },
    "SEG": {
        # start of odstavek
        "test": "_else",
        "action": pop_and_push_to(
            "u", "div", tag="seg"
        ),  # close any open seg; land on the enclosing <u>
    },
}

CONFIG: Config = {
    "left": {
        "SEJA_DATE": {
            # "OD 15. MAJA 1964. GODINE" - looks like the declaration, so
            # match on the "OD " prefix first
            "test": lambda x, y, w, h, para: (
                is_session_title(x, y, w, h, para)
                and para.text.strip().startswith("OD ")
            ),
            "action": pop_and_push_to("div", tag="time"),
        },
        "SEJA_NUM": {
            # "6. SEDNICA" / "8. ZAJEDNIČKA SEDNICA" - bold in some
            # sessions, not in others, so anchor on the text
            "test": lambda x, y, w, h, para: (
                is_session_title(x, y, w, h, para) and "SEDNICA" in para.text
            ),
            "action": pop_and_push_to("div", tag="head", type="sessionNumber"),
        },
        "SEJA_DECLARATION": {
            # "PROSVETNO-KULTURNO VEĆE" - whatever else the title block holds
            "test": is_session_title,
            "action": pop_and_push_to("div", tag="head", type="session"),
        },
        **body_rules,
    },
    "right": {
        **session_front_rules,
        **body_rules,
    },
    # "either": body_rules,
    "center": {
        # full-page-width frames (joint sessions center their chairman block
        # across both columns); no body rules here on purpose - whatever else
        # is full-width (tables etc.) we can't represent anyway
        **session_front_rules,
    },
    # "center": {
    #     "CHAIRMAN": {
    #         "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
    #         "test": lambda x, y, w, h, para: tag_is_on_top(
    #             "time"
    #         ),  # if we're in time, next is chairman
    #         "action": pop_and_push_to("div", tag="note", type="chairman"),
    #     },
    #     "SEJA_DECLARATION": {
    #         "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
    #         "test_run": lambda x, y, w, h, run: run.bold,  # bold baby
    #         "action": pop_and_push_to("div", tag="head", type="session"),
    #     },
    #     "TIME": {
    #         "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
    #         "test_run": lambda x, y, w, h, run: run.italic
    #         and not visited_time,  # italic my beloved
    #         "action": pop_and_push_to("div", tag="time"),
    #         "after_push": lambda: globals().update({"visited_time": True}),
    #     },
    #     "SEJA_SECTION": {
    #         # isn't bolded or italic, so it's a 'del seje', or something
    #         "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
    #         "test_run": "_else",
    #         "action": pop_and_push_to("div", tag="head", type="sessionSection"),
    #     },
    #     "RANDOM_TEXT": {
    #         # random text
    #         # in case things are chunked, or something
    #         "test": lambda x, y, w, h, para: (
    #             visited_time or para.alignment != WD_PARAGRAPH_ALIGNMENT.CENTER
    #         ),
    #     },
    # },
    # "_else": {
    #     "run_immediate": lambda: globals().update({"visited_time": False}),
    #     "REFERENCE_ENTRY": {
    #         # opomba :O
    #         "test": lambda x, y, w, h, para: para.runs[0].font.superscript,
    #         "action": ref_entry_action,
    #         "append_func": ref_append,
    #     },
    #     "GENERIC_NOTE": {
    #         # a note about something that happened, such as "seja se je zakljucila"
    #         "test": lambda x, y, w, h, para: (
    #             para.alignment == WD_PARAGRAPH_ALIGNMENT.CENTER
    #             and all(r.italic for r in para.runs)
    #         ),
    #         "action": generic_note_action,
    #         "append_func": lambda x, y, w, h, para, para_idx: append(
    #             *para.runs, para_idx=para_idx, should_annotate=["REFERENCE"]
    #         ),
    #     },
    #     "SPEAKER": {
    #         # this is REALLY BAD detection but wtf am i supposed to do
    #         "test": lambda x, y, w, h, para: (
    #             para.paragraph_format.first_line_indent == 0
    #             and para.runs[0].text[:3].isupper()
    #         ),
    #         "action": speaker_action,
    #     },
    #     "SEG": {
    #         # indented - start of odstavek
    #         "test": "_else",
    #         "action": pop_and_push_to(
    #             "u", "div", tag="seg"
    #         ),  # close any open seg; land on the enclosing <u>
    #     },
    # },
}


def get_frames(doc: Document):
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