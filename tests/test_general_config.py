from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import engine
from doc2tei.parser import load_config, parse_document
from doc2tei.extractors import CharacterPDFExtractor, LineRecord
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
    metadata: dict[str, object] | None = None,
):
    return pdf_runs(
        [(text, size)],
        page=page,
        x=x,
        y=y,
        indent=indent,
        metadata=metadata,
    )[0]


def pdf_runs(
    parts: list[tuple[str, float]],
    *,
    page: int = 0,
    x: float = 72.0,
    y: float = 500.0,
    indent: float = 0.0,
    metadata: dict[str, object] | None = None,
):
    context = PDFPageContext(
        page, 600.0, 800.0, metadata={"structure_active": True}
    )
    runs = []
    previous = None
    run_x = x
    for text, size in parts:
        run = make_chunk(
            text=text,
            x=run_x,
            y=y,
            font_name="Times-Roman",
            size=size,
            previous=previous,
            page_num=page,
            space_before=bool(runs),
            page_context=context,
        )
        runs.append(run)
        previous = run
        run_x += max(8.0, len(text) * size * 0.45)
    line = make_chunk(
        text=" ".join(text for text, _size in parts),
        x=x,
        y=y,
        runs=runs,
        page_num=page,
        page_context=context,
        metadata={"indent": indent, **(metadata or {})},
    )
    for run in runs:
        run.line_chunk = line
    return runs


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


def test_speaker_index_is_preserved_without_disabling_later_sessions():
    module = load_config(CONFIG_PATH).module
    context = PDFPageContext(0, 600.0, 800.0)
    index = LineRecord(
        "SEZNAM GOVORNIKOV", 72.0, 700.0, "Times-Roman", 10.0, []
    )
    module.reset_state()
    module.enrich_page(context, [index])
    index_metadata = module.enrich_line(context, index, 0, [index])
    assert index_metadata["source_artifact"] == "speakerIndex"

    later_context = PDFPageContext(1, 600.0, 800.0)
    session = LineRecord("20. SEJA", 72.0, 700.0, "Times-Bold", 13.0, [])
    module.enrich_page(later_context, [session])
    session_metadata = module.enrich_line(later_context, session, 0, [session])
    assert "source_artifact" not in session_metadata


def test_source_artifact_text_is_retained_once(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=pdf_runs(
            [("15.", 8.0), ("SEJA", 8.0)],
            y=780.0,
            metadata={"source_artifact": "runningHeader"},
        ),
    )

    note = result.root.find(
        ".//note[@type='sourceArtifact'][@subtype='runningHeader']"
    )
    assert note is not None
    assert note.attrib["n"] == "1"
    assert "".join(note.itertext()) == "15. SEJA"
    assert "".join(result.root.itertext()).count("15. SEJA") == 1


def test_unmatched_line_is_preserved_and_flagged(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    loaded.module.PROFILE.update(
        mode="char-preserve", body_size=10.0, styled=False
    )
    result = parse_document(
        source,
        config=loaded,
        chunks=[
            pdf_line("Boris PreĹˇern:", indent=0.0),
            pdf_line("Neujemajoca vrstica.", y=480.0, size=16.0),
        ],
    )

    assert "Neujemajoca vrstica." in "".join(result.root.itertext())
    comments = [
        element.text or ""
        for element in result.root.iter()
        if element.tag is ET.Comment
    ]
    assert any("unmatched source line" in comment for comment in comments)
    assert result.diagnostics.rule_counts["UNMATCHED_LINE"] == 1
    unparsed = result.root.find(".//seg[@type='unparsed']")
    assert unparsed is not None
    assert unparsed.attrib["n"] == "1"
    word_measure = result.root.find(
        "teiHeader/fileDesc/extent/measure[@unit='words']"
    )
    assert word_measure is not None
    assert word_measure.attrib["quantity"] == "4"


def test_footnote_reference_links_to_definition_without_hash_in_xml_id(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    loaded.module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    result = parse_document(
        source,
        config=loaded,
        chunks=[
            pdf_line("Boris Prešern: Hvala.", indent=0.0),
            *pdf_runs([("Besedilo", 10.0), ("1", 7.0)], indent=10.0),
            *pdf_runs([("1", 7.0), ("Besedilo opombe.", 7.0)], y=80.0),
            pdf_line("Nadaljevanje.", page=1, y=500.0, indent=10.0),
        ],
    )

    reference = result.root.find(".//ref[@type='footnote']")
    note = result.root.find(".//note[@place='foot']")
    assert reference is not None
    assert reference.attrib["target"] == "#note1"
    assert note is not None
    assert note.attrib["xml:id"] == "note1"
    assert note.attrib["n"] == "1"
    assert "Besedilo opombe." in "".join(note.itertext())


def test_duplicate_footnote_numbers_get_valid_page_scoped_ids():
    module = load_config(CONFIG_PATH).module
    module.footnotes.reset()
    first = pdf_runs([("Besedilo", 10.0), ("1", 7.0)], page=0)[1]
    second = pdf_runs([("Besedilo", 10.0), ("1", 7.0)], page=1)[1]
    same_page_duplicate = pdf_runs(
        [("Drugo besedilo", 10.0), ("1", 7.0)], page=1
    )[1]

    assert module.footnotes.definition_id(first) == "note1"
    assert module.footnotes.definition_id(second) == "note1-p2"
    assert module.footnotes.definition_id(second) == "note1-p2"
    assert module.footnotes.definition_id(same_page_duplicate) == "note1-p2-2"
    assert module.footnotes.target_id(second) == "note1"


def test_split_numeric_runs_are_one_footnote_number():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    runs = pdf_runs([("Besedilo", 7.0), ("1", 7.0), ("5", 7.0), ("Opombe", 7.0)])

    assert module.footnotes.number(runs[1]) == "15"
    assert not module.footnotes.is_first_numeric_run(runs[2])


def test_literal_space_break_preserves_the_remainder_as_another_record():
    class FakeChar:
        def __init__(self, text: str, x: float):
            self.text = text
            self.x0 = x
            self.x1 = x + 5.0
            self.y0 = 500.0
            self.fontname = "Times-Roman"
            self.size = 10.0

        def get_text(self):
            return self.text

    chars = [
        FakeChar("A", 0.0),
        FakeChar("B", 5.0),
        FakeChar(" ", 10.0),
        FakeChar("C", 50.0),
        FakeChar("D", 55.0),
    ]
    extractor = CharacterPDFExtractor(literal_spaces="break")

    records, _pending = extractor._make_records([chars], False)

    assert [record.text for record in records] == ["AB", "CD"]
    assert [record.x for record in records] == [0.0, 50.0]
