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
    process_batch_job,
)
from doc2tei.parser import LoadedConfig, ParseDiagnostics, ParseResult


TEI_NAMESPACE = {"tei": "http://www.tei-c.org/ns/1.0"}


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
    relative_bundles = {
        Path(job.bundle).relative_to(output).as_posix() for job in jobs
    }
    assert "nested/other" in relative_bundles
    assert len(relative_bundles) == 3
    assert any(name.startswith("same-") for name in relative_bundles)

    same_root_jobs, _ = discover_batch_jobs([inputs], inputs, recursive=False)
    assert {Path(job.source).name for job in same_root_jobs} == {
        "same.pdf",
        "same.docx",
    }


def test_batch_job_writes_outputs_and_skips_an_unchanged_bundle(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    config_path = tmp_path / "config.py"
    bundle = tmp_path / "output" / "source"
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
            data={"speakers": {"#JanezNovak": "Janez Novak:"}},
        )

    monkeypatch.setattr(batch_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(batch_module, "parse_document", fake_parse)
    job = BatchJob(str(source), str(bundle))
    options = BatchOptions(
        config=str(config_path),
        pretty=True,
        xml_declaration=True,
        page_workers=1,
    )

    first = process_batch_job(job, options)
    second = process_batch_job(job, options)

    assert first.status == "ok"
    assert second.status == "skipped"
    assert calls == 1
    assert (bundle / "document.xml").is_file()
    assert (bundle / "diagnostics.json").is_file()
    assert (bundle / "data.json").is_file()
    assert (bundle / "listPerson.xml").is_file()
    assert not (bundle / "debug.log").exists()
    document = ET.parse(bundle / "document.xml").getroot()
    assert (
        document.findtext(
            "tei:text/tei:body/tei:div/tei:p",
            namespaces=TEI_NAMESPACE,
        )
        == "retained text"
    )
    status = json.loads((bundle / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "ok"
    assert status["chunk_count"] == 1
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
    assert not (bundle / "listPerson.xml").exists()


def test_batch_job_turns_a_hard_parser_error_into_a_reviewable_bundle(
    tmp_path, monkeypatch
):
    source = tmp_path / "broken.pdf"
    config_path = tmp_path / "config.py"
    bundle = tmp_path / "output" / "broken"
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
        BatchJob(str(source), str(bundle)),
        BatchOptions(config=str(config_path), page_workers=1),
    )

    assert result.status == "recovered"
    assert result.recovery_count >= 1
    document = ET.parse(bundle / "document.xml").getroot()
    recovery_note = document.find(
        "tei:teiHeader/tei:fileDesc/tei:notesStmt/tei:note",
        TEI_NAMESPACE,
    )
    assert recovery_note is not None
    assert "ValueError: bad PDF" in (recovery_note.text or "")
    assert document.find(".//tei:p[@type='unparsed']", TEI_NAMESPACE) is not None
    diagnostics = json.loads(
        (bundle / "diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["recovery_counts"] == {"batch.document": 1}
    assert "ValueError: bad PDF" in (bundle / "debug.log").read_text(encoding="utf-8")
    list_person = ET.parse(bundle / "listPerson.xml").getroot()
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
    relative = Path("CON") / ("a" * 180)
    source = tmp_path / "source.pdf"

    safe = batch_module._safe_relative_bundle(relative, source, tmp_path)

    assert "CON" not in safe.parts
    assert all(len(part) <= 100 for part in safe.parts)
    assert len(str(tmp_path / safe / "diagnostics.json")) <= 240
