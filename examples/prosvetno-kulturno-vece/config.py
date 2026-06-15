# EXAMPLE config file for some PROSVETNO-KULTURNO VEĆE doc i got sent
# starts with page 3
# works upto the... upto the part where it doesn't
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
from type_decs import Config, WordRuleGroup
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx import Document
from docx.oxml.ns import qn


def ref_entry_action(chunk: WordChunk):
    note_num = chunk.text.strip()
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


def ref_append(chunk: WordChunk):
    # the marker run (e.g. "1") is recorded as the note's @n by ref_entry_action,
    # so we don't append it as text. we also don't bulk-append the rest of the
    # paragraph here - runs aren't guaranteed to be whole, so the footnote body
    # runs are appended individually as they're parsed, landing inside the note
    # that ref_entry_action left open on top of the stack.
    engine.lstrip_next = True


def generic_note_action(chunk: Chunk):
    pop_to("div")  # prolly should add tests like is_tag_on_top
    push("note")
    push("hi", rend="italic")  # i guess


def speaker_action(chunk: Chunk):
    # only open a new speaker note if we're not already inside one
    # (a follow-up speaker paragraph just appends into the open note)
    if not tag_is_on_top("note", type="speaker"):
        pop_to("div")
        push("note", type="speaker")


def contents_action(chunk: Chunk):
    # the SADRZAJ frame - every paragraph of it lands in one note
    if not tag_is_on_top("note", type="contents"):
        pop_to("div")
        push("note", type="contents")


def is_session_title(chunk: WordChunk):
    # session container at the start
    # with info about session name, num, date
    return (
        chunk.y < 2000
        and chunk.h < 3000
        and chunk.w > 2900
        and chunk.paragraph.text.strip().isupper()
    )


# the SADRZAJ / PREDSEDAVAO / "Početak u ..." blocks that open a session
# usually in the right column, but joint sessions center them across the page
session_front_rules: WordRuleGroup = {
    "CHAIRMAN": {
        # one centered all caps paragraph in a small container
        # but the ocr is kinda inconsistent so we anchor on "PREDSEDAVA"
        "alignment": WD_PARAGRAPH_ALIGNMENT.CENTER,
        "test_run": lambda chunk: chunk.h < 1500
        and (
            chunk.paragraph.text.strip().startswith("PREDSEDAVA")
            or chunk.paragraph.text.strip().isupper()
        ),
        "action": pop_and_push_to("div", tag="note", type="chairman"),
    },
    "TIME": {
        # "Početak u 9 č 10 min" - italic, shares the chairman's frame
        # (some have a stray bold run in the middle, hence the prefix test)
        "test_run": lambda chunk: chunk.h < 1500
        and (
            chunk.paragraph.text.strip().startswith("Početak")
            or all(r.italic for r in chunk.paragraph.runs)
        ),
        "action": pop_and_push_to("div", tag="time"),
    },
    "CONTENTS": {
        # opened by the SADRZAJ heading, keeps eating paragraphs until
        # some other rule (chairman, a title, ...) pops the note
        "test_run": lambda chunk: (
            chunk.paragraph.text.strip().upper().startswith("SADR")
            or tag_is_on_top("note", type="contents")
        ),
        "action": contents_action,
    },
}

# rules for the two-column debate body, shared by both zones
body_rules: WordRuleGroup = {
    "REFERENCE_ENTRY": {
        # opomba :O
        # a footnote *definition* leads its paragraph with the superscript
        # marker. an inline reference inside body text is a superscript run
        # that ISN'T the paragraph's first run - that one falls through to
        # append(), which turns it into an inline <ref>.
        "test_run": lambda chunk: (
            chunk.run.font.superscript is True
            and chunk.run._element is chunk.paragraph.runs[0]._element
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
        "append_func": lambda chunk: append(chunk, should_annotate=["REFERENCE"]),
    },
    "SPEAKER": {
        # "Predsednik Nikola Sekulić:" / "Janez Vipotnik:"
        # a short paragraph with a bold run that ends with a colon
        "test_run": lambda chunk: (
            any(r.bold for r in chunk.paragraph.runs)
            and chunk.paragraph.text.strip().endswith(":")
            and len(chunk.paragraph.text.strip()) < 100
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
}

# figure it out
# explained in zbor-republik-in-pokrajin already
# + the readme
CONFIG: Config = {
    "mode": "word",
    "alignments": {
        "left": {
            "SEJA_DATE": {
                # "OD 15. MAJA 1964. GODINE" - looks like the declaration, so
                # match on the "OD " prefix first
                "test_run": lambda chunk: (
                    is_session_title(chunk)
                    and chunk.paragraph.text.strip().startswith("OD ")
                ),
                "action": pop_and_push_to("div", tag="time"),
            },
            "SEJA_NUM": {
                # "6. SEDNICA" / "8. ZAJEDNIČKA SEDNICA" - bold in some
                # sessions, not in others, so anchor on the text
                "test_run": lambda chunk: (
                    is_session_title(chunk) and "SEDNICA" in chunk.paragraph.text
                ),
                "action": pop_and_push_to("div", tag="head", type="sessionNumber"),
            },
            "SEJA_DECLARATION": {
                # "PROSVETNO-KULTURNO VEĆE" - whatever else the title block holds
                "test_run": lambda chunk: is_session_title(chunk),
                "action": pop_and_push_to("div", tag="head", type="session"),
            },
            **body_rules,
        },
        "right": {
            **session_front_rules,
            **body_rules,
        },
        "center": {
            # full-page-width frames (joint sessions center their chairman block across both columns)
            # no body rules here on purpose - whatever else
            # is full-width (tables etc.) we can't represent anyway
            **session_front_rules,
        },
    },
}


# get frames of document
# this can be changed if you need to parse something other than a doc
# now yields one chunk per run (paragraphs aren't guaranteed to be whole)
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

        for run in para.runs:
            print(f"----- RUNNNNNNN ------: {run.text}")
            yield make_chunk(run, para)
