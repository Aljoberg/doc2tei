from __future__ import annotations

from pathlib import Path

import engine
from doc2tei.parser import load_config, parse_document
from engine import PDFPageContext, make_chunk


CONFIG_PATH = Path(__file__).parents[1] / "examples" / "general" / "config.py"


def pdf_line(
    text: str,
    *,
    page: int = 0,
    x: float = 72.0,
    y: float = 500.0,
    size: float = 10.0,
    indent: float = 0.0,
):
    context = PDFPageContext(
        page, 600.0, 800.0, metadata={"structure_active": True}
    )
    run = make_chunk(
        text=text,
        x=x,
        y=y,
        font_name="Times-Roman",
        size=size,
        previous=None,
        page_num=page,
        space_before=False,
        page_context=context,
    )
    line = make_chunk(
        text=text,
        x=x,
        y=y,
        runs=[run],
        page_num=page,
        page_context=context,
        metadata={"indent": indent},
    )
    run.line_chunk = line
    return run


def test_speaker_identifier_discards_roles_and_speech():
    module = load_config(CONFIG_PATH).module

    assert (
        module.speaker_identifier(
            "Imer Pulja, zvezni sekretar za trg: Spoštovani delegati."
        )
        == "#ImerPulja"
    )
    assert (
        module.speaker_identifier("Boris Prešern: Tovariš predsednik: hvala.")
        == "#BorisPrešern"
    )


def test_appointment_list_prose_is_not_a_speaker():
    module = load_config(CONFIG_PATH).module

    assert not module._looks_like_person_prefix("Drago Sotler Za člane pa")
    assert not module._looks_like_person_prefix("predsednika Drago Sotler")
    assert module._looks_like_person_prefix("Boris Prešern")
    assert module._looks_like_person_prefix("Predsednik Boris Prešern")
    assert module._looks_like_person_prefix("Janez P i r n a t")


def test_same_line_speech_is_outside_speaker_note(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("Boris Prešern: Hvala lepa.", indent=0.0),
            pdf_line("Nadaljujem razpravo.", y=480.0, indent=0.0),
        ],
    )

    note = result.root.find(".//note[@type='speaker']")
    utterance = result.root.find(".//u")
    assert note is not None
    assert "".join(note.itertext()) == "Boris Prešern:"
    assert utterance is not None
    assert utterance.attrib["who"] == "#BorisPrešern"
    speech = "".join(utterance.itertext())
    assert "Hvala lepa." in speech
    assert "Nadaljujem razpravo." in speech


def test_later_heading_opens_a_new_division(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("12. SEJA", size=13.0),
            pdf_line("Boris Prešern: Hvala.", y=480.0),
            pdf_line("13. TOČKA DNEVNEGA REDA", y=460.0),
            pdf_line("Boris Prešern: Nadaljujmo.", y=440.0),
        ],
    )

    agenda = result.root.find(".//div[@type='agendaSection']")
    assert agenda is not None
    children = list(agenda)
    assert children
    assert children[0].tag == "head"
    assert children[0].attrib["type"] == "agendaItem"


def test_internal_indexes_never_terminate_extraction():
    module = load_config(CONFIG_PATH).module
    context = PDFPageContext(0, 600.0, 800.0)
    assert not module.stop_before(
        type("Record", (), {"text": "SEZNAM GOVORNIKOV"})(), context
    )
    assert not module.stop_before(
        type("Record", (), {"text": "PRILOGE"})(), context
    )
