from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import engine
import doc2tei.parser as parser_module
from doc2tei.parser import load_config, parse_document
from doc2tei.extractors import CharacterPDFExtractor, LineRecord
from doc2tei.helpers import build_list_person
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
    assert module.CONFIG["page_workers"] == 0
    assert module._make_extractor("char-preserve").merge_nearby_runs is True
    assert module._make_extractor("char-preserve").page_workers == 0
    assert module._make_extractor("char-break").line_break_mode == "downward"
    assert module._make_extractor("ocr").page_workers == 0

    module.CONFIG["merge_nearby_runs"] = False
    module.CONFIG["page_workers"] = 1
    assert module._make_extractor("char-preserve").merge_nearby_runs is False
    assert module._make_extractor("char-preserve").page_workers == 1
    assert module._make_extractor("ocr").page_workers == 1


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

    assert calls == len(loaded.config["rules"])


def test_disabled_debug_does_not_construct_chunk_repr(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
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


def test_empty_and_invalid_speaker_mappings_always_build_a_list_person():
    empty = build_list_person({})
    placeholder = empty.find("person")
    assert placeholder is not None
    assert placeholder.attrib["xml:id"] == "UnknownSpeaker"

    recovered = build_list_person({"#1 broken id": "1 broken id:"})
    person = recovered.find("person")
    assert person is not None
    assert person.attrib["xml:id"].startswith("speaker-")


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
    recovery_note = result.root.find(".//note[@type='conversionRecovery']")
    assert recovery_note is not None
    assert "no matching footnote definition" in (recovery_note.text or "")


def test_rule_and_extractor_failures_are_recorded_without_losing_prior_text(tmp_path):
    source = tmp_path / "sample.pdf"
    source.touch()
    loaded = load_config(CONFIG_PATH)
    original_rules = loaded.config["rules"]

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
