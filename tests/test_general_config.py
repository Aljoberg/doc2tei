from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import cast
import xml.etree.ElementTree as ET

import engine
import parse as parse_cli
import doc2tei.parser as parser_module
from doc2tei.parser import (
    ParseDiagnostics,
    ParseResult,
    load_config,
    parse_document,
)
from doc2tei.extractors import (
    CharacterPDFExtractor,
    LineRecord,
    RunRecord,
    WordPDFExtractor,
)
from doc2tei.helpers import build_list_org, build_list_person
from engine import PDFPageContext, make_chunk
from type_decs import WikidataBinding

CONFIG_PATH = Path(__file__).parents[1] / "examples" / "general-config" / "config.py"


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
    context = PDFPageContext(page, 600.0, 800.0, metadata={"structure_active": True})
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
    assert not module._looks_like_person_prefix(
        "Miran Cvenk, za podpredsednika Lado Simončič in za člane"
    )
    assert not module._looks_like_person_prefix(
        "Jože Brilej, za podpredsednika Janko Cesnik, za člane pa"
    )
    assert not module._looks_like_person_prefix(
        "Predsednik vlade je predložil skupščini naslednje"
    )
    assert not module._looks_like_person_prefix("Predsednik opravlja tele zadeve")
    assert module._looks_like_person_prefix("Boris Prešern")
    assert module._looks_like_person_prefix("Predsednik Boris Prešern")
    assert not module._looks_like_person_prefix(
        "Tine Lah, dr. Heli Modic in Franc Sustersic"
    )
    assert not module._looks_like_person_prefix(
        "LR Slovenije (Uradni list LRS, st. 2-11/50)"
    )
    assert not module._looks_like_person_prefix("V Prilogi 1")
    assert not module._looks_like_person_prefix("PRISOTNI CLANI VLADE")
    assert not module._looks_like_person_prefix("Porocevalec; Predsednik")
    assert module._looks_like_person_prefix("Janez P i r n a t")


def test_repeated_parallel_colon_labels_are_marked_as_table_rows():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="ocr", body_size=10.0, styled=False)
    page = PDFPageContext(0, 600.0, 800.0)
    records = []
    for index, label in enumerate(("Sv. Ana:", "Sv. Katarina:", "Mali Dolenci:")):
        y = 300.0 - index * 12.0
        records.extend(
            (
                LineRecord(label, 50.0, y, "Times-Italic", 10.0, []),
                LineRecord(
                    f"Vrednost {index + 1}",
                    230.0,
                    y,
                    "Times-Roman",
                    10.0,
                    [],
                ),
            )
        )

    module.enrich_page(page, records)

    assert all(
        record.metadata.get("tabular_label")
        for record in records
        if record.text.endswith(":")
    )
    table_chunk = pdf_line(
        "Sv. Ana:",
        metadata={"tabular_label": True},
    )
    assert module.speaker_parts(table_chunk) is None
    assert not module.is_generic_note(table_chunk)


def test_repeated_italic_speaker_labels_are_not_a_table_without_values():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    page = PDFPageContext(0, 600.0, 800.0)
    records = [
        LineRecord(label, 72.0, 300.0 - index * 24.0, "Times-Italic", 10.0, [])
        for index, label in enumerate(
            ("Andrej Svetek:", "Vinko Hafner:", "Danijel Lepin:")
        )
    ]

    module.enrich_page(page, records)

    assert not any(record.metadata.get("tabular_label") for record in records)


def test_combined_table_columns_are_learned_from_run_geometry():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="ocr", body_size=10.0, styled=False)
    page = PDFPageContext(0, 600.0, 800.0)
    records = []
    for index, label in enumerate(("Spodnja Idrija:", "Vojsko:", "Kanal:")):
        y = 300.0 - index * 12.0
        records.append(
            LineRecord(
                f"{label} Naselje {index + 1}",
                80.0,
                y,
                "Times-Italic",
                9.0,
                [
                    RunRecord(label, 80.0, y, "Times-Italic", 9.0, False),
                    RunRecord(
                        f"Naselje {index + 1}",
                        205.0,
                        y,
                        "Times-Roman",
                        9.0,
                        True,
                    ),
                ],
            )
        )

    module.enrich_page(page, records)

    assert all(record.metadata.get("tabular_label") for record in records)


def test_same_font_statistical_rows_are_learned_from_repeated_values():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    page = PDFPageContext(0, 600.0, 800.0)
    records = [
        LineRecord(
            "Murska Sobota: od 130 predloženih 60 ali 46,2 %.",
            54.0,
            300.0,
            "Times-Roman",
            9.0,
            [],
        ),
        LineRecord(
            "Novo mesto: od 153 predloženih 70 ali 45,7 %.",
            54.0,
            288.0,
            "Times-Roman",
            9.0,
            [],
        ),
    ]

    module.enrich_page(page, records)

    assert all(record.metadata.get("tabular_label") for record in records)


def test_article_numbers_in_speeches_are_not_statistical_table_rows():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    page = PDFPageContext(0, 600.0, 800.0)
    records = [
        LineRecord(
            "Dr. Marijan Brecelj: V 113. členu predlagam spremembo.",
            54.0,
            300.0,
            "Times-Roman",
            9.0,
            [],
        ),
        LineRecord(
            "Dr. Miha Potočnik: V 120. in 122. členu je določeno.",
            54.0,
            288.0,
            "Times-Roman",
            9.0,
            [],
        ),
    ]

    module.enrich_page(page, records)

    assert not any(record.metadata.get("tabular_label") for record in records)


def test_compact_structural_rows_are_learned_as_a_table():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=11.0, styled=True)
    module.reset_state()
    page = PDFPageContext(0, 600.0, 800.0)
    records = [
        LineRecord("Brinje (del): mesto", 20.0, 300.0, "Times-Roman", 11.0, []),
        LineRecord("Dravlje: mesto", 20.0, 288.0, "Times-Roman", 11.0, []),
        LineRecord(
            "Jezica: Jezica, Kleče, Savije",
            20.0,
            276.0,
            "Times-Roman",
            11.0,
            [],
        ),
        LineRecord(
            "Murska Sobota: mesto",
            20.0,
            264.0,
            "Times-Roman",
            11.0,
            [],
        ),
    ]

    module.enrich_page(page, records)

    assert all(record.metadata.get("tabular_label") for record in records)

    debate_rows = [
        LineRecord("Janez Novak: Da.", 72.0, 240.0, "Times-Roman", 11.0, []),
        LineRecord("Miha Kovač: Hvala.", 72.0, 228.0, "Times-Roman", 11.0, []),
        LineRecord("Ana Horvat: Se strinjam.", 72.0, 216.0, "Times-Roman", 11.0, []),
        LineRecord("Ivo Mlakar: Prosim.", 72.0, 204.0, "Times-Roman", 11.0, []),
        LineRecord("Maja Zupan: Nadaljujem.", 72.0, 192.0, "Times-Roman", 11.0, []),
    ]
    debate_page = PDFPageContext(1, 600.0, 800.0)
    module.enrich_page(debate_page, debate_rows)
    assert not any(record.metadata.get("tabular_label") for record in debate_rows)


def test_speaker_aliases_merge_only_close_ocr_variants(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("Predsednik Ferdo Kozak: Prvi.", y=500.0),
            pdf_line("Predsednik L e r d o K o z a k: Drugi.", y=480.0),
            pdf_line("Predsednik Miha Marinko: Tretji.", y=460.0),
            pdf_line("Predsednik Milan Marinko: Četrti.", y=440.0),
        ],
    )

    who = [utterance.attrib["who"] for utterance in result.root.findall(".//u")]
    assert who[0] == who[1] == "#FerdoKozak"
    assert who[2] != who[3]
    speakers = result.data["speakers"]
    assert isinstance(speakers, dict)
    assert set(speakers) == {
        "#FerdoKozak",
        "#MihaMarinko",
        "#MilanMarinko",
    }


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


def test_second_session_heading_opens_a_new_debate_section(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("12. SEJA", size=13.0),
            pdf_line("Boris Prešern: Prva seja.", y=480.0),
            pdf_line("13. SEJA", y=460.0, size=13.0),
            pdf_line("Boris Prešern: Druga seja.", y=440.0),
        ],
    )

    sessions = [
        division
        for division in result.root.findall(".//div[@type='debateSection']")
        if division.find("head[@type='sessionNumber']") is not None
    ]
    assert len(sessions) == 2
    assert [division.findtext("head") for division in sessions] == [
        "12. SEJA",
        "13. SEJA",
    ]
    assert all(division.find("u") is not None for division in sessions)


def test_non_session_heading_uses_a_generic_section_division(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("12. SEJA", size=13.0),
            pdf_line("PREHODNE IN KONČNE DOLOČBE", y=480.0, size=13.0),
            pdf_line("Besedilo določb.", y=460.0),
        ],
    )

    section = result.root.find(".//div[@type='section']")
    assert section is not None
    head = section.find("head")
    assert head is not None
    assert head.attrib["type"] == "section"
    assert "".join(head.itertext()) == "PREHODNE IN KONČNE DOLOČBE"


def test_only_complete_parenthetical_lines_are_stage_directions():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="ocr", body_size=10.0, styled=False)

    assert module.is_generic_note(pdf_line("(Poslanci ploskajo.)", indent=10.0))
    assert not module.is_generic_note(
        pdf_line("(Glede predloga želim povedati naslednje.", indent=10.0)
    )
    assert not module.is_generic_note(
        pdf_line("(sedež Sevnica) Občina Sevnica obsega:", indent=10.0)
    )


def test_stage_direction_does_not_absorb_the_following_speech_line(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("Predsednik Ferdo Kozak: Zacetek.", y=500.0),
            pdf_line("(Poslanci ploskajo.)", y=480.0, indent=10.0),
            pdf_line("Nadaljevanje govora.", y=460.0),
        ],
    )

    note = result.root.find(".//u/note")
    assert note is not None
    assert "".join(note.itertext()) == "(Poslanci ploskajo.)"
    assert "Nadaljevanje govora." in "".join(result.root.itertext())


def test_wrapped_non_speech_lines_stay_in_one_paragraph(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[
            pdf_line("Prva vrstica.", y=500.0),
            pdf_line("Nadaljevanje istega odstavka.", y=480.0),
            pdf_line("Nov odstavek.", y=460.0, indent=12.0),
        ],
    )

    body = result.root.find("text/body")
    assert body is not None
    paragraphs = list(body.iter("p"))
    assert len(paragraphs) == 2
    assert "Nadaljevanje istega odstavka." in "".join(paragraphs[0].itertext())
    assert "Nov odstavek." in "".join(paragraphs[1].itertext())


def test_speaker_index_is_preserved_without_disabling_later_sessions():
    module = load_config(CONFIG_PATH).module
    context = PDFPageContext(0, 600.0, 800.0)
    index = LineRecord("SEZNAM GOVORNIKOV", 72.0, 700.0, "Times-Roman", 10.0, [])
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

    note = result.root.find(".//note[@type='sourceArtifact'][@subtype='runningHeader']")
    assert note is not None
    assert note.attrib["n"] == "1"
    assert "".join(note.itertext()) == "15. SEJA"
    assert "".join(result.root.itertext()).count("15. SEJA") == 1


def test_source_artifacts_on_one_page_are_grouped_without_text_loss(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    first = pdf_line(
        "15. SEJA",
        y=780.0,
        size=8.0,
        metadata={"source_artifact": "runningHeader"},
    )
    second = pdf_line(
        "REPUBLIŠKI ZBOR",
        y=770.0,
        size=8.0,
        metadata={"source_artifact": "runningHeader"},
    )

    result = parse_document(source, config=CONFIG_PATH, chunks=[first, second])

    notes = result.root.findall(
        ".//note[@type='sourceArtifact'][@subtype='runningHeader']"
    )
    assert len(notes) == 1
    assert (notes[0].text or "").splitlines() == ["15. SEJA", "REPUBLIŠKI ZBOR"]


def test_repeated_bottom_series_label_is_not_a_footnote():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=12.0, styled=True)
    context = PDFPageContext(0, 600.0, 800.0)
    footer = LineRecord(
        "3 St. beležke SNS 1964/5",
        48.0,
        40.0,
        "Times-Roman",
        9.0,
        [],
        x_end=170.0,
    )

    assert module.source_artifact_type(footer, context) == "runningFooter"


def test_midline_exponents_and_percentages_are_not_footnote_entries():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    module.footnotes.reset()
    exponent = pdf_runs(
        [("300 000 m", 10.0), ("3", 7.0), ("lesa", 10.0)],
        y=100.0,
    )[1]
    percentage = pdf_runs([("0", 7.0), ("/o", 7.0)], y=100.0)[0]
    far_right_table_value = pdf_runs(
        [("55", 7.0), ("ali 3,0 %", 7.0)],
        x=350.0,
        y=100.0,
    )[0]
    short_gibberish = pdf_runs(
        [("5", 7.0), ("ФД! Д", 7.0)],
        y=100.0,
    )[0]
    table_count = pdf_runs(
        [("613", 7.0), ("podjetij in uslužbencev", 7.0)],
        y=100.0,
    )[0]

    assert not module.footnotes.is_entry(exponent)
    assert not module.footnotes.is_entry(percentage)
    assert not module.footnotes.is_entry(far_right_table_value)
    assert not module.footnotes.is_entry(short_gibberish)
    assert not module.footnotes.is_entry(table_count)


def test_footnote_entry_allows_leading_ocr_punctuation():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    module.footnotes.reset()
    marker = pdf_runs(
        [('", ', 7.0), ("7", 7.0), ("Izvoljena na seji.", 7.0)],
        y=80.0,
    )[1]

    assert not marker.is_line_start
    assert module.footnotes.is_entry(marker)

    right_column = pdf_runs(
        [("2", 7.0), ("Besedilo druge opombe.", 7.0)],
        x=350.0,
        y=80.0,
    )[0]
    right_column.page_context.metadata["columns"] = [72.0, 350.0]
    assert module.footnotes.is_entry(right_column)


def test_large_footnote_number_requires_document_evidence():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    module.footnotes.reset()
    marker = pdf_runs(
        [("100", 7.0), ("Besedilo stote opombe.", 7.0)],
        y=80.0,
    )[0]

    assert not module.footnotes.is_entry(marker)
    module.footnotes.references[(marker.page_num, "100")] = [ET.Element("ref")]
    assert module.footnotes.is_entry(marker)


def test_footnotes_remain_enabled_in_back_matter():
    module = load_config(CONFIG_PATH).module
    context = PDFPageContext(
        0,
        600.0,
        800.0,
        metadata={
            "structure_active": False,
            "back_matter": True,
            "speaker_index": False,
        },
    )
    marker = pdf_runs(
        [("2", 7.0), ("Besedilo opombe.", 7.0)],
        y=80.0,
    )[0]
    marker.page_context = context
    marker.line_chunk.page_context = context

    assert module.is_footnote_page(marker)


def test_unmatched_line_is_preserved_and_flagged(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    loaded.module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=False)
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
    word_measure = result.root.find("teiHeader/fileDesc/extent/measure[@unit='words']")
    assert word_measure is not None
    assert word_measure.attrib["quantity"] == "4"


def test_footnote_reference_links_to_definition_without_hash_in_xml_id(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    loaded.module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    reference_runs = pdf_runs([("Besedilo", 10.0), ("1", 7.0)], indent=10.0)
    reference_runs[1].y += 3.0
    result = parse_document(
        source,
        config=loaded,
        chunks=[
            pdf_line("Boris Prešern: Hvala.", indent=0.0),
            *reference_runs,
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
    same_page_duplicate = pdf_runs([("Drugo besedilo", 10.0), ("1", 7.0)], page=1)[1]

    assert module.footnotes.definition_id(first) == "note1"
    assert module.footnotes.definition_id(second) == "note1-p2"
    assert module.footnotes.definition_id(second) == "note1-p2"
    assert module.footnotes.definition_id(same_page_duplicate) == "note1-p2-2"
    assert module.footnotes.target_id(second) == "note1-p2"


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


def test_character_extractor_merges_only_insignificant_run_differences():
    class FakeChar:
        def __init__(
            self,
            text: str,
            x: float,
            *,
            size: float = 10.0,
            y: float = 500.0,
            font: str = "Times-Roman",
        ):
            self.text = text
            self.x0 = x
            self.x1 = x + 5.0
            self.y0 = y
            self.fontname = font
            self.size = size

        def get_text(self):
            return self.text

    insignificant = [
        FakeChar("X", -5.0, size=12.0),
        FakeChar("A", 0.0, size=10.0),
        FakeChar("B", 5.0, size=10.04, y=500.03),
    ]
    significant_size = insignificant + [FakeChar("C", 10.0, size=9.7)]
    raised = insignificant + [FakeChar("1", 10.0, size=10.05, y=502.0)]
    style_transition = [
        FakeChar("P", 0.0, size=11.0, font="Times-Bold"),
        FakeChar(" ", 5.0, size=11.04, font="Times-Bold"),
        FakeChar("u", 10.0, size=11.0, font="Times-Italic"),
        FakeChar(" ", 15.0, size=11.04, font="Times-Italic"),
        FakeChar("9", 20.0, size=11.02, font="Times-Italic"),
    ]

    extractor = CharacterPDFExtractor()
    exact_extractor = CharacterPDFExtractor(merge_nearby_runs=False)

    merged, _ = extractor._make_records([insignificant], False)
    exact, _ = exact_extractor._make_records([insignificant], False)
    sized, _ = extractor._make_records([significant_size], False)
    baseline, _ = extractor._make_records([raised], False)
    styled, _ = extractor._make_records([style_transition], False)

    assert [run.text for run in merged[0].runs] == ["X", "AB"]
    assert [run.text for run in exact[0].runs] == ["X", "A", "B"]
    assert [run.text for run in sized[0].runs] == ["X", "AB", "C"]
    assert [run.text for run in baseline[0].runs] == ["X", "AB", "1"]
    assert [run.text for run in styled[0].runs] == ["P", " ", "u", "9"]


def test_general_config_can_disable_nearby_run_merging():
    module = load_config(CONFIG_PATH).module

    assert module.CONFIG["merge_nearby_runs"] is True
    configured_workers = int(module.CONFIG["page_workers"])
    assert configured_workers >= 0
    assert module._make_extractor("char-preserve").merge_nearby_runs is True
    assert module._make_extractor("char-preserve").page_workers == configured_workers
    assert module._make_extractor("char-break").line_break_mode == "downward"
    assert module._make_extractor("ocr").page_workers == configured_workers

    module.CONFIG["merge_nearby_runs"] = False
    module.CONFIG["page_workers"] = 1
    assert module._make_extractor("char-preserve").merge_nearby_runs is False
    assert module._make_extractor("char-preserve").page_workers == 1
    assert module._make_extractor("ocr").page_workers == 1


def test_word_extractor_releases_each_pdfplumber_page(monkeypatch):
    import pdfplumber

    class FakePage:
        width = 600.0
        height = 800.0
        closed = False

        def extract_words(self, **_kwargs):
            return [
                {
                    "text": "Text",
                    "x0": 72.0,
                    "x1": 92.0,
                    "top": 90.0,
                    "bottom": 100.0,
                    "fontname": "Times-Roman",
                    "size": 10.0,
                }
            ]

        def close(self):
            self.closed = True

    class FakePDF:
        def __init__(self, page):
            self.pages = [page]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    page = FakePage()
    monkeypatch.setattr(pdfplumber, "open", lambda _filename: FakePDF(page))

    chunks = list(WordPDFExtractor(page_workers=1)("unused.pdf"))

    assert page.closed
    assert [chunk.text for chunk in chunks] == ["Text"]


def test_rules_are_normalized_once_per_document(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    calls = 0
    original = parser_module._normalize_rule

    def counted(rule):
        nonlocal calls
        calls += 1
        return original(rule)

    monkeypatch.setattr(parser_module, "_normalize_rule", counted)
    parse_document(
        source,
        config=loaded,
        chunks=[
            pdf_line("Prva vrstica."),
            pdf_line("Druga vrstica.", y=480.0),
            pdf_line("Tretja vrstica.", y=460.0),
        ],
    )

    rules = loaded.config["rules"]
    assert isinstance(rules, Mapping)
    assert calls == len(rules)


def test_disabled_debug_does_not_construct_chunk_repr(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    assert isinstance(loaded.config, MutableMapping)
    loaded.config["debug"] = False

    def fail_repr(_chunk):
        raise AssertionError("disabled debug attempted to render a chunk")

    monkeypatch.setattr(engine.PDFChunk, "__repr__", fail_repr)
    result = parse_document(
        source,
        config=loaded,
        chunks=[pdf_line("Ohranjeno besedilo.")],
    )

    assert "Ohranjeno besedilo." in "".join(result.root.itertext())


def test_body_sized_standalone_session_and_zasedanje_activate_structure():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="ocr", body_size=10.0, styled=False)

    first_page = PDFPageContext(0, 600.0, 800.0)
    session = LineRecord(
        "1. seja",
        270.0,
        700.0,
        "Times-Roman",
        10.0,
        [],
        x_end=330.0,
    )
    module.reset_state()
    module.enrich_page(first_page, [session])
    assert first_page.metadata["structure_active"] is True

    second_page = PDFPageContext(1, 600.0, 800.0)
    zasedanje = LineRecord(
        "SKUPNO ZASEDANJE",
        210.0,
        700.0,
        "Times-Roman",
        10.0,
        [],
        x_end=390.0,
    )
    module.reset_state()
    module.enrich_page(second_page, [zasedanje])
    assert second_page.metadata["heading_active"] is True
    assert second_page.metadata["structure_active"] is False

    transcript_page = PDFPageContext(2, 600.0, 800.0)
    speaker = LineRecord(
        "Predsednik Janez Novak:",
        72.0,
        680.0,
        "Times-Roman",
        10.0,
        [],
        x_end=190.0,
    )
    module.enrich_page(transcript_page, [speaker])
    assert transcript_page.metadata["structure_active"] is True


def test_session_mention_in_prose_does_not_reactivate_back_matter():
    module = load_config(CONFIG_PATH).module
    module.PROFILE.update(mode="ocr", body_size=10.0, styled=False)
    module.reset_state()
    module._STATE.update(seen_meeting=True, seen_session=True, back_matter=True)
    page = PDFPageContext(0, 600.0, 800.0)
    prose = LineRecord(
        "Naš zbor je na svoji 9. seji potrdil statut.",
        72.0,
        700.0,
        "Times-Roman",
        12.0,
        [],
        x_end=400.0,
    )

    module.enrich_page(page, [prose])

    assert page.metadata["back_matter"] is True
    assert page.metadata["structure_active"] is False
    assert not module._is_session_marker("28. Sejanci")
    assert module._is_session_marker("r 6. SEDNICA OD 15. MAJA 1964. GODINE")
    assert not module._is_session_marker("Naš zbor je na svoji 9. seji potrdil statut.")

    role_prose_page = PDFPageContext(1, 600.0, 800.0)
    role_prose = LineRecord(
        "predsednik volilnega odbora pojasni: postopek ostaja enak.",
        72.0,
        680.0,
        "Times-Roman",
        10.0,
        [],
        x_end=430.0,
    )
    module.enrich_page(role_prose_page, [role_prose])
    assert role_prose_page.metadata["structure_active"] is False

    quoted_session_page = PDFPageContext(2, 600.0, 800.0)
    quoted_session = LineRecord(
        "6. SEJA",
        270.0,
        680.0,
        "Times-Bold",
        14.0,
        [],
        x_end=330.0,
    )
    module.enrich_page(quoted_session_page, [quoted_session])
    assert quoted_session_page.metadata["back_matter"] is True
    assert quoted_session_page.metadata["structure_active"] is False


def test_auto_ids_and_speaker_ids_recover_digit_prefixes(tmp_path):
    source = tmp_path / "2.bad source.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[pdf_line("Predsednik dr. 1' e r d o K o z a k: Pozdravljeni.")],
    )

    ids = [
        element.attrib["xml:id"]
        for element in result.root.iter()
        if "xml:id" in element.attrib
    ]
    assert ids
    assert all(identifier[0].isalpha() or identifier[0] == "_" for identifier in ids)
    utterance = result.root.find(".//u")
    assert utterance is not None
    assert utterance.attrib["who"].startswith("#speaker-")


def test_auto_ids_can_use_the_final_batch_component_stem(tmp_path):
    source = tmp_path / "old source filename.pdf"
    source.touch()
    component = "ParlaMint-SI_1969-09-01-sklic-05-01"

    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[pdf_line("Predsednik Janez Novak: Pozdravljeni.")],
        id_prefix=component,
    )

    generated = [
        element.attrib["xml:id"]
        for element in result.root.iter()
        if "xml:id" in element.attrib and "." in element.attrib["xml:id"]
    ]
    assert generated
    assert all(identifier.startswith(f"{component}.") for identifier in generated)
    assert all("old-source-filename" not in identifier for identifier in generated)


def test_empty_and_invalid_speaker_mappings_always_build_a_list_person():
    empty = build_list_person({})
    placeholder = empty.find("person")
    assert placeholder is not None
    assert placeholder.attrib["xml:id"] == "UnknownSpeaker"

    recovered = build_list_person({"#1 broken id": "1 broken id:"})
    person = recovered.find("person")
    assert person is not None
    assert person.attrib["xml:id"].startswith("speaker-")


def test_list_person_recovers_roles_and_affiliations_from_labels():
    root = build_list_person(
        {
            "#ZoranPolic": "PREDSEDNIK ZORAN POLIČ (SR Slovenija):",
            "#VeselinDjuranovic": "VESELIN DJURANOVIČ, predsednik ZIS:",
            "#FerdoKozak": (
                "Predsednik dr, Ferdp Kozak prične ob S.20 sejo z "
                "naslednjimi besedami:"
            ),
            "#ImerPulja": "IMER PULJA, zvezni sekretar za trg:",
            "#NotAPerson": "SR Slovenije, da se na lastno željo razrešijo:",
        }
    )
    people = root.findall("person")

    first_affiliation = people[0].find("affiliation")
    assert first_affiliation is not None
    assert first_affiliation.attrib["role"] == "predsednik"
    assert first_affiliation.findtext("roleName") == "PREDSEDNIK"
    assert first_affiliation.findtext("orgName") == "SR Slovenija"
    # The affiliation points at the matching listOrg entry by a stable id.
    assert first_affiliation.attrib["ref"] == "#org.SR-Slovenija"

    second_affiliation = people[1].find("affiliation")
    assert second_affiliation is not None
    assert second_affiliation.attrib["role"] == "predsednik"
    assert second_affiliation.findtext("roleName") == "predsednik ZIS"

    # A comma inside OCR text must not override a known leading role, while a
    # generic prose clause must not be invented as an affiliation role.
    assert people[2].findtext("affiliation/roleName") == "Predsednik"
    fourth_affiliation = people[3].find("affiliation")
    assert fourth_affiliation is not None
    assert fourth_affiliation.attrib["role"] == "sekretar"
    assert people[4].find("affiliation") is None


def test_list_org_collects_and_ids_affiliation_organizations():
    mapping = {
        "#ZoranPolic": "PREDSEDNIK ZORAN POLIČ (SR Slovenija):",
        "#AnaNovak": "Ana Novak (SR Slovenija):",  # same org, deduplicated
        "#ImerPulja": "IMER PULJA, zvezni sekretar za trg:",  # no organization
        "#MihaKovac": "Miha Kovač (SDS):",
    }
    root = build_list_org(mapping)

    orgs = root.findall("org")
    assert {org.attrib["xml:id"] for org in orgs} == {"org.SR-Slovenija", "org.SDS"}
    assert {org.findtext("orgName") for org in orgs} == {"SR Slovenija", "SDS"}

    # The listOrg id is exactly what the listPerson affiliation references.
    person = build_list_person({"#MihaKovac": "Miha Kovač (SDS):"}).find("person")
    assert person is not None
    affiliation = person.find("affiliation")
    assert affiliation is not None
    assert affiliation.attrib["ref"] == "#org.SDS"


def test_empty_list_org_uses_a_schema_safe_placeholder():
    root = build_list_org({})
    organization = root.find("org")

    assert organization is not None
    assert organization.attrib["xml:id"] == "org.UnknownOrganization"
    assert organization.findtext("orgName") == "Unknown organization"


def test_wikidata_list_person_is_enriched_and_remains_fail_soft():
    calls: list[str] = []

    def fetch(search_name: str) -> list[WikidataBinding]:
        calls.append(search_name)
        if search_name == "Broken Lookup":
            raise RuntimeError("network unavailable")
        if search_name == "Malformed Result":
            return cast(
                list[WikidataBinding],
                [{"p": "not a SPARQL value object"}],
            )
        return [
            {
                "p": {"value": "http://www.wikidata.org/entity/Q123"},
                "pLabel": {"value": "Janez Novak"},
                "givenLabel": {"value": "Janez"},
                "familyLabel": {"value": "Novak"},
                "birth": {"value": "1920-01-02T00:00:00Z"},
                "bplaceLabel": {"value": "Ljubljana"},
                "sexLabel": {"value": "moški"},
                "occLabel": {"value": "politik"},
                "citizenLabel": {"value": "Slovenija"},
                "party": {"value": "http://www.wikidata.org/entity/Q456"},
                "partyLabel": {"value": "Preskusna stranka"},
                "viaf": {"value": "12345"},
                "gnd": {"value": "67890"},
                "isni": {"value": "0000000000000000"},
            }
        ]

    root = build_list_person(
        {
            "#JanezNovak": "Janez Novak, minister (Vlada):",
            "#BrokenLookup": "Broken Lookup (Skupščina):",
            "#MalformedResult": "Malformed Result:",
        },
        include_wikidata=True,
        wikidata_fetcher=fetch,
        wikidata_workers=2,
    )

    assert sorted(calls) == ["Broken Lookup", "Janez Novak", "Malformed Result"]
    assert [head.text for head in root.findall("head")] == [
        "Seznam govornikov",
        "List of speakers",
    ]
    enriched, fallback, malformed = root.findall("person")
    assert enriched.findtext("persName/surname") == "Novak"
    assert enriched.findtext("persName/forename") == "Janez"
    assert enriched.find("idno[@subtype='wikidata']") is not None
    birth = enriched.find("birth")
    sex = enriched.find("sex")
    assert birth is not None
    assert sex is not None
    assert birth.attrib["when"] == "1920-01-02"
    assert enriched.findtext("birth/placeName") == "Ljubljana"
    assert sex.attrib["value"] == "M"
    assert enriched.findtext("occupation") == "politik"
    assert enriched.findtext("nationality") == "Slovenija"
    affiliations = enriched.findall("affiliation")
    assert any(item.findtext("orgName") == "Preskusna stranka" for item in affiliations)
    assert any(item.findtext("orgName") == "Vlada" for item in affiliations)
    assert fallback.findtext("persName/forename") == "Broken"
    assert fallback.findtext("persName/surname") == "Lookup"
    assert fallback.findtext("affiliation/orgName") == "Skupščina"
    assert malformed.findtext("persName/forename") == "Malformed"
    assert malformed.find("idno") is None


def test_cli_forwards_wikidata_toggle_only_to_list_person(monkeypatch):
    calls: dict[str, dict[str, object]] = {}

    class FakeResult:
        def write_xml(self, _path, **kwargs):
            calls["xml"] = kwargs

        def write_list_person(self, _path, **kwargs):
            calls["list_person"] = kwargs

    monkeypatch.setattr(
        parse_cli,
        "parse_document",
        lambda _input, *, config: FakeResult(),
    )

    assert (
        parse_cli.main(
            [
                "input.pdf",
                "--config",
                "config.py",
                "--out",
                "document.xml",
                "--list-person-output",
                "listPerson.xml",
                "--include-wikidata",
            ]
        )
        == 0
    )
    assert calls["xml"] == {"xml_declaration": False, "pretty": False}
    assert calls["list_person"] == {
        "xml_declaration": False,
        "pretty": False,
        "include_wikidata": True,
    }


def test_unresolved_superscript_is_retained_as_typography_not_a_reference(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    loaded.module.PROFILE.update(mode="char-preserve", body_size=10.0, styled=True)
    runs = pdf_runs([("Besedilo", 10.0), ("7", 7.0)], indent=10.0)
    runs[1].y += 3.0

    result = parse_document(source, config=loaded, chunks=runs)

    assert result.root.find(".//ref[@type='footnote']") is None
    superscript = result.root.find(".//hi[@rend='superscript']")
    assert superscript is not None
    assert "7" in "".join(superscript.itertext())
    assert result.root.find(".//note[@type='conversionRecovery']") is None
    warning_note = result.root.find(".//note[@type='conversionWarning']")
    assert warning_note is not None
    assert "no matching footnote definition" in (warning_note.text or "")
    assert result.data["warnings"] == [warning_note.text]


def test_rule_and_extractor_failures_are_recorded_without_losing_prior_text(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    assert isinstance(loaded.config, MutableMapping)
    original_rules = loaded.config["rules"]
    assert isinstance(original_rules, Mapping)

    def broken_test(_chunk):
        raise RuntimeError("bad source-specific assumption")

    loaded.config["rules"] = {
        "BROKEN": {"test": broken_test},
        **original_rules,
    }

    def broken_stream():
        yield pdf_line("Ohranjeno besedilo.")
        raise RuntimeError("damaged later page")

    result = parse_document(source, config=loaded, chunks=broken_stream())

    assert "Ohranjeno besedilo." in "".join(result.root.itertext())
    assert result.diagnostics.recovery_counts["rule.BROKEN.test"] == 1
    assert result.diagnostics.recovery_counts["extractor"] == 1
    recovery_note = result.root.find(".//note[@type='conversionRecovery']")
    assert recovery_note is not None


def test_forbidden_xml_control_characters_are_visible_not_dropped(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    result = parse_document(
        source,
        config=CONFIG_PATH,
        chunks=[pdf_line("Pred\x00pona")],
    )

    assert "Pred[U+0000]pona" in "".join(result.root.itertext())
    ET.fromstring(result.to_bytes())


def test_pretty_xml_preserves_mixed_content_and_original_tree():
    root = ET.fromstring(
        "<TEI><text><body><p>Hello <hi>world</hi>!</p>"
        "<div><p>Second paragraph.</p></div></body></text></TEI>"
    )
    result = ParseResult(root, ParseDiagnostics(input="sample", config="test"))
    compact_before = result.to_bytes()

    pretty = result.to_bytes(xml_declaration=True, pretty=True)

    assert b"\n  <text>" in pretty
    pretty_root = ET.fromstring(pretty)
    paragraph = pretty_root.find(".//p")
    assert paragraph is not None
    assert "".join(paragraph.itertext()) == "Hello world!"
    assert result.to_bytes() == compact_before


def test_malformed_pdf_still_returns_serializable_outputs(tmp_path):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"this is not a PDF")

    result = parse_document(source, config=CONFIG_PATH)
    list_person_path = tmp_path / "listPerson.xml"
    result.write_list_person(list_person_path, xml_declaration=True)

    ET.fromstring(result.to_bytes(xml_declaration=True))
    list_person = ET.parse(list_person_path).getroot()
    assert list_person.find("{http://www.tei-c.org/ns/1.0}person") is not None
    assert result.diagnostics.recovery_counts["extractor.recovery"] >= 1
    assert result.diagnostics.recovery_counts["extractor.no_text"] == 1
    assert result.root.find(".//note[@type='conversionRecovery']") is not None
