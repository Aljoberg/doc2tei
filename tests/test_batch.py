from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
import xml.etree.ElementTree as ET

import engine
import doc2tei.batch as batch_module
from doc2tei.batch import (
    BatchJob,
    BatchOptions,
    automatic_document_workers,
    discover_batch_jobs,
    document_list_person_path,
    document_path,
    metadata_dir,
    process_batch_job,
    write_batch_list_person_outputs,
)
from doc2tei.parser import LoadedConfig, ParseDiagnostics, ParseResult

TEI_NAMESPACE = {"tei": "http://www.tei-c.org/ns/1.0"}


def _job(source: Path, group: Path, title: str) -> BatchJob:
    return BatchJob(str(source), str(group), title)


def _loaded_config(path: Path) -> LoadedConfig:
    module = ModuleType("batch_test_config")
    config: dict[str, object] = {"page_workers": 0}
    module.CONFIG = config
    return LoadedConfig(
        module=module,
        path=path,
        config=config,
        cosmetic_annotations={},
        get_chunks=lambda _filename: [],
        log=lambda *_args, **_kwargs: None,
    )


def test_batch_discovery_is_recursive_collision_safe_and_excludes_output(tmp_path):
    inputs = tmp_path / "inputs"
    nested = inputs / "nested"
    output = inputs / "generated"
    nested.mkdir(parents=True)
    output.mkdir()
    (inputs / "same.pdf").touch()
    (inputs / "same.docx").touch()
    (nested / "other.PDF").touch()
    (nested / "ignore.txt").touch()
    (output / "old.pdf").touch()

    jobs, warnings = discover_batch_jobs([inputs], output)

    assert warnings == []
    assert {Path(job.source).name for job in jobs} == {
        "same.pdf",
        "same.docx",
        "other.PDF",
    }
    documents = {document_path(job).relative_to(output).as_posix() for job in jobs}
    assert "nested/documents/other.xml" in documents
    assert len(documents) == 3
    titles = {job.title for job in jobs if Path(job.group) == output}
    # The two "same"-stemmed files in the same group get a collision suffix.
    assert "same" in titles
    assert any(title.startswith("same-") for title in titles)
    groups = {Path(job.source).name: Path(job.group) for job in jobs}
    assert groups["other.PDF"] == output / "nested"
    assert groups["same.pdf"] == output
    assert groups["same.docx"] == output

    same_root_jobs, _ = discover_batch_jobs([inputs], inputs, recursive=False)
    assert {Path(job.source).name for job in same_root_jobs} == {
        "same.pdf",
        "same.docx",
    }


def test_batch_keeps_same_named_source_folders_as_separate_groups(tmp_path):
    first = tmp_path / "one" / "conference"
    second = tmp_path / "two" / "conference"
    output = tmp_path / "output"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "minutes.pdf").touch()
    (second / "minutes.pdf").touch()

    jobs, warnings = discover_batch_jobs([first, second], output)

    assert warnings == []
    assert len(jobs) == 2
    assert len({job.group for job in jobs}) == 2
    assert all(
        document_path(job).parent == Path(job.group) / "documents" for job in jobs
    )


def test_batch_job_writes_outputs_and_skips_an_unchanged_bundle(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    config_path = tmp_path / "config.py"
    group = tmp_path / "output"
    job = _job(source, group, "source")
    document = document_path(job)
    meta = metadata_dir(job)
    list_person = document_list_person_path(job)
    source.write_bytes(b"fake PDF")
    config_path.write_text("CONFIG = {}\n", encoding="utf-8")
    loaded = _loaded_config(config_path)
    calls = 0

    def fake_parse(input_path, *, config):
        nonlocal calls
        calls += 1
        assert Path(input_path) == source
        assert config.config["page_workers"] == 1
        root, content = engine.default_document()
        ET.SubElement(content, "p").text = "retained text"
        return ParseResult(
            root=root,
            diagnostics=ParseDiagnostics(
                input=str(source),
                config=str(config_path),
                chunk_count=1,
            ),
            data={
                "speakers": {"#JanezNovak": "Janez Novak:"},
                "warnings": ["review retained superscript"],
            },
        )

    monkeypatch.setattr(batch_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(batch_module, "parse_document", fake_parse)
    options = BatchOptions(
        config=str(config_path),
        pretty=True,
        xml_declaration=True,
        page_workers=1,
    )

    first = process_batch_job(job, options)
    second = process_batch_job(job, options)

    assert first.status == "ok"
    assert first.warning_count == 1
    assert first.message == "completed with warnings"
    assert second.status == "skipped"
    assert second.warning_count == 1
    assert calls == 1
    folder_options = BatchOptions(
        config=str(config_path),
        pretty=True,
        xml_declaration=True,
        page_workers=1,
        list_person_scope="folder",
    )
    assert process_batch_job(job, folder_options).status == "skipped"
    assert calls == 1
    assert document.is_file()
    assert document.parent == group / "documents"
    assert (meta / "diagnostics.json").is_file()
    assert (meta / "data.json").is_file()
    assert list_person.is_file()
    assert not (meta / "debug.log").exists()
    parsed_document = ET.parse(document).getroot()
    assert (
        parsed_document.findtext(
            "tei:text/tei:body/tei:div/tei:p",
            namespaces=TEI_NAMESPACE,
        )
        == "retained text"
    )
    status = json.loads((meta / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "ok"
    assert status["chunk_count"] == 1
    assert status["warning_count"] == 1
    assert "parser" in status["fingerprint"]["implementation"]

    source.write_bytes(b"changed fake PDF")
    assert process_batch_job(job, options).status == "ok"
    assert calls == 2

    no_list_options = BatchOptions(
        config=str(config_path),
        page_workers=1,
        write_list_person=False,
    )
    assert process_batch_job(job, no_list_options).status == "ok"
    assert not list_person.exists()


def test_batch_job_turns_a_hard_parser_error_into_a_reviewable_bundle(
    tmp_path, monkeypatch
):
    source = tmp_path / "broken.pdf"
    config_path = tmp_path / "config.py"
    job = _job(source, tmp_path / "output", "broken")
    document = document_path(job)
    meta = metadata_dir(job)
    source.write_bytes(b"not really a PDF")
    config_path.write_text("CONFIG = {}\n", encoding="utf-8")
    loaded = _loaded_config(config_path)

    monkeypatch.setattr(batch_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(
        batch_module,
        "parse_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad PDF")),
    )

    result = process_batch_job(
        job,
        BatchOptions(config=str(config_path), page_workers=1),
    )

    assert result.status == "recovered"
    assert result.recovery_count >= 1
    parsed_document = ET.parse(document).getroot()
    recovery_note = parsed_document.find(
        "tei:teiHeader/tei:fileDesc/tei:notesStmt/tei:note",
        TEI_NAMESPACE,
    )
    assert recovery_note is not None
    assert "ValueError: bad PDF" in (recovery_note.text or "")
    assert parsed_document.find(".//tei:p[@type='unparsed']", TEI_NAMESPACE) is not None
    diagnostics = json.loads((meta / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["recovery_counts"] == {"batch.document": 1}
    assert "ValueError: bad PDF" in (meta / "debug.log").read_text(encoding="utf-8")
    list_person = ET.parse(document_list_person_path(job)).getroot()
    unknown = list_person.find("tei:person", TEI_NAMESPACE)
    assert unknown is not None
    assert unknown.attrib["{http://www.w3.org/XML/1998/namespace}id"] == (
        "UnknownSpeaker"
    )


def test_automatic_batch_workers_leave_capacity_and_cap_memory_pressure(monkeypatch):
    monkeypatch.setattr(batch_module.os, "cpu_count", lambda: 16)
    assert automatic_document_workers(100) == 4
    assert automatic_document_workers(2) == 2
    assert automatic_document_workers(0) == 1


def test_batch_shortens_unsafe_and_excessively_long_output_paths(tmp_path):
    source = tmp_path / "source.pdf"
    group_dir = tmp_path / ("g" * 80)
    used: set[str] = set()

    title = batch_module._safe_title("a" * 180, group_dir, source, used)
    duplicate = batch_module._safe_title("a" * 180, group_dir, source, used)

    # Long titles are shortened in place so the whole bundle stays nested
    # inside its group rather than being relocated to a flat sibling folder.
    assert "_long-paths" not in title
    assert "_long-paths" not in str(batch_module.document_path(BatchJob("", "", title)))
    assert len(str(group_dir / "metadata" / title / "diagnostics.json")) <= 240
    # A second document with the same name still gets a distinct title.
    assert duplicate != title

    # Reserved Windows names are escaped by the component sanitiser.
    assert batch_module._safe_bundle_component("CON") not in {"CON", "con"}

    long_group = Path("a" * 100) / ("b" * 100) / ("c" * 100)
    safe_group = batch_module._safe_relative_group(long_group, tmp_path)
    assert all(len(part) <= 100 for part in safe_group.parts)
    assert len(str(tmp_path / safe_group / "listPerson.xml")) <= 240


def test_batch_writes_folder_and_corpus_list_person_outputs(tmp_path):
    output = tmp_path / "output"
    first_group = output / "conference-a"
    second_group = output / "conference-b"
    jobs = [
        _job(tmp_path / "a1.pdf", first_group, "a1"),
        _job(tmp_path / "a2.pdf", first_group, "a2"),
        _job(tmp_path / "b1.pdf", second_group, "b1"),
    ]
    mappings = [
        {"#JanezNovak": "Janez Novak:"},
        {
            "#JanezNovak": "Predsednik Janez Novak:",
            "#MajaZupan": "Maja Zupan:",
        },
        {"#MihaKovac": "Miha Kovač:"},
    ]
    for job, mapping in zip(jobs, mappings):
        meta = metadata_dir(job)
        meta.mkdir(parents=True)
        (meta / "data.json").write_text(
            json.dumps({"speakers": mapping}, ensure_ascii=False),
            encoding="utf-8",
        )
        stale = document_list_person_path(job)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale", encoding="utf-8")
    (output / "listPerson.xml").write_text("stale", encoding="utf-8")

    folder_options = BatchOptions(
        config=str(tmp_path / "config.py"),
        pretty=True,
        xml_declaration=True,
        list_person_scope="folder",
    )
    folder_outputs = write_batch_list_person_outputs(
        jobs,
        output,
        folder_options,
    )

    assert set(folder_outputs) == {
        (first_group / "listPerson.xml").resolve(),
        (second_group / "listPerson.xml").resolve(),
    }
    assert not (output / "listPerson.xml").exists()
    assert not any(document_list_person_path(job).exists() for job in jobs)
    first_people = (
        ET.parse(first_group / "listPerson.xml")
        .getroot()
        .findall(
            "tei:person",
            TEI_NAMESPACE,
        )
    )
    assert {
        person.attrib["{http://www.w3.org/XML/1998/namespace}id"]
        for person in first_people
    } == {"JanezNovak", "MajaZupan"}
    assert (
        first_people[0].findtext("tei:persName", namespaces=TEI_NAMESPACE)
        == "Predsednik Janez Novak"
    )

    corpus_options = BatchOptions(
        config=str(tmp_path / "config.py"),
        list_person_scope="corpus",
    )
    corpus_outputs = write_batch_list_person_outputs(
        jobs,
        output,
        corpus_options,
    )

    assert corpus_outputs == [(output / "listPerson.xml").resolve()]
    assert not (first_group / "listPerson.xml").exists()
    assert not (second_group / "listPerson.xml").exists()
    corpus_people = (
        ET.parse(output / "listPerson.xml")
        .getroot()
        .findall(
            "tei:person",
            TEI_NAMESPACE,
        )
    )
    assert {
        person.attrib["{http://www.w3.org/XML/1998/namespace}id"]
        for person in corpus_people
    } == {"JanezNovak", "MajaZupan", "MihaKovac"}

    document_options = BatchOptions(
        config=str(tmp_path / "config.py"),
        list_person_scope="document",
    )
    document_outputs = write_batch_list_person_outputs(
        jobs,
        output,
        document_options,
    )

    assert set(document_outputs) == {
        document_list_person_path(job).resolve() for job in jobs
    }
    assert not (output / "listPerson.xml").exists()
    assert not (first_group / "listPerson.xml").exists()
    assert not (second_group / "listPerson.xml").exists()
    assert all(path.is_file() for path in document_outputs)

    disabled_options = BatchOptions(
        config=str(tmp_path / "config.py"),
        write_list_person=False,
        list_person_scope="corpus",
    )
    assert (
        write_batch_list_person_outputs(
            jobs,
            output,
            disabled_options,
        )
        == []
    )
    assert not (output / "listPerson.xml").exists()


def test_scope_switch_separates_document_and_folder_list_person(tmp_path):
    # A document in a group and its parent's group used to be able to collide on
    # ``<group>/listPerson.xml``. Document-scoped output now lives under
    # ``<group>/documents/<title>.listPerson.xml`` so the two never overlap, and
    # switching scopes must still clean up the alternative layout.
    output = tmp_path / "output"
    conference = output / "conference"
    root_job = _job(tmp_path / "conference.pdf", output, "conference")
    nested_job = _job(tmp_path / "conference" / "minutes.pdf", conference, "minutes")
    for job, mapping in (
        (root_job, {"#RootSpeaker": "Root Speaker:"}),
        (nested_job, {"#FolderSpeaker": "Folder Speaker:"}),
    ):
        meta = metadata_dir(job)
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "data.json").write_text(
            json.dumps({"speakers": mapping}),
            encoding="utf-8",
        )

    folder_options = BatchOptions(
        config=str(tmp_path / "config.py"),
        list_person_scope="folder",
    )
    write_batch_list_person_outputs(
        [root_job, nested_job],
        output,
        folder_options,
    )
    folder_list = conference / "listPerson.xml"
    assert "Folder Speaker" in folder_list.read_text(encoding="utf-8")

    document_options = BatchOptions(
        config=str(tmp_path / "config.py"),
        list_person_scope="document",
    )
    write_batch_list_person_outputs(
        [root_job, nested_job],
        output,
        document_options,
    )
    # The stale folder-scoped file is removed and per-document files replace it,
    # each in its own group's ``documents`` folder without overwriting another.
    assert not folder_list.exists()
    assert "Root Speaker" in document_list_person_path(root_job).read_text(
        encoding="utf-8"
    )
    assert "Folder Speaker" in document_list_person_path(nested_job).read_text(
        encoding="utf-8"
    )
