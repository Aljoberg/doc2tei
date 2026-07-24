from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from streamlit.testing.v1 import AppTest
from streamlit.runtime.download_data_util import (
    convert_data_to_bytes_and_infer_mime,
)

from doc2tei.web import (
    build_corpus_archive,
    build_batch_command,
    log_tail,
    manifest_artifacts,
    manifest_counts,
    manifest_metadata_artifacts,
    parse_lines,
    validate_pipeline_request,
    write_uploads,
)
from type_decs import PipelineRequest, UploadedDocument


def test_web_request_builds_the_canonical_batch_command(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "config.py"
    config.write_text("CONFIG = {}\n", encoding="utf-8")
    output = tmp_path / "corpus"
    metadata = tmp_path / "audit"
    request = PipelineRequest(
        output_root=output,
        metadata_root=metadata,
        config=config,
        local_inputs=(source,),
        sistory_menus=("https://sistory.si/slv/menu/1/7/397/407",),
        workers=3,
        page_workers=1,
        list_person_scope="folder",
        emit_corpus=True,
        include_root_corpus=True,
        corpus_prefix="Debates",
        corpus_code="GB",
        include_wikidata=True,
        overwrite=True,
    )

    assert validate_pipeline_request(request) == []
    command = build_batch_command(request, tmp_path)

    assert command[:3] == [
        command[0],
        "-u",
        str(tmp_path / "batch_parse.py"),
    ]
    assert command[command.index("--output-dir") + 1] == str(output)
    assert command[command.index("--metadata-dir") + 1] == str(metadata)
    assert command[command.index("--workers") + 1] == "3"
    assert command[command.index("--page-workers") + 1] == "1"
    assert command[command.index("--sistory-menu") + 1] == "1/7/397/407"
    assert command[command.index("--corpus-prefix") + 1] == "Debates"
    assert command[command.index("--corpus-code") + 1] == "GB"
    assert "--emit-corpus-xml" in command
    assert "--include-root-corpus" in command
    assert "--include-wikidata" in command
    assert "--overwrite" in command
    assert command[-1] == str(source)


def test_web_request_rejects_nested_audit_and_missing_inputs(tmp_path):
    config = tmp_path / "config.py"
    config.touch()
    output = tmp_path / "corpus"
    request = PipelineRequest(
        output_root=output,
        metadata_root=output / "metadata",
        config=config,
    )

    errors = validate_pipeline_request(request)

    assert any("Choose at least one" in error for error in errors)
    assert any("separate, non-nested" in error for error in errors)

    incompatible = PipelineRequest(
        output_root=output,
        metadata_root=tmp_path / "audit",
        config=config,
        local_inputs=(tmp_path,),
        emit_corpus=False,
        include_root_corpus=True,
    )
    assert any(
        "aggregate root corpus requires" in error.casefold()
        for error in validate_pipeline_request(incompatible)
    )

    invalid_prefix = PipelineRequest(
        output_root=output,
        metadata_root=tmp_path / "audit",
        config=config,
        local_inputs=(tmp_path,),
        corpus_prefix="../unsafe",
    )
    assert any(
        "corpus prefix" in error.casefold()
        for error in validate_pipeline_request(invalid_prefix)
    )


def test_web_uploads_are_safe_and_collision_free(tmp_path):
    written = write_uploads(
        (
            UploadedDocument("../minutes.pdf", b"first"),
            UploadedDocument("minutes.pdf", b"second"),
            UploadedDocument("bad<name>.docx", b"third"),
        ),
        tmp_path,
    )

    assert [path.name for path in written] == [
        "minutes.pdf",
        "minutes-2.pdf",
        "bad-name-.docx",
    ]
    assert [path.read_bytes() for path in written] == [b"first", b"second", b"third"]
    assert all(path.parent == tmp_path for path in written)


def test_web_manifest_helpers_only_expose_existing_xml(tmp_path):
    document = tmp_path / "document.xml"
    corpus = tmp_path / "corpus.xml"
    document.write_text("<TEI/>", encoding="utf-8")
    corpus.write_text("<teiCorpus/>", encoding="utf-8")
    manifest: dict[str, object] = {
        "counts": {"ok": 1, "failed": 0, "invalid": "1"},
        "items": [
            {
                "source": "source.pdf",
                "output": str(document),
                "status": "ok",
            }
        ],
        "corpus": {"outputs": [str(corpus), str(tmp_path / "missing.xml")]},
    }

    assert manifest_counts(manifest) == {"ok": 1, "failed": 0}
    assert manifest_artifacts(manifest) == [document, corpus]


def test_web_archive_contains_only_generated_xml_below_output_root(tmp_path):
    output = tmp_path / "tei-output"
    nested = output / "corpus-a" / "session"
    nested.mkdir(parents=True)
    first = output / "corpus-a.xml"
    second = nested / "document.xml"
    ignored_text = output / "notes.txt"
    external = tmp_path / "external.xml"
    first.write_text("<teiCorpus/>", encoding="utf-8")
    second.write_text("<TEI/>", encoding="utf-8")
    ignored_text.write_text("not a deliverable", encoding="utf-8")
    external.write_text("<outside/>", encoding="utf-8")

    archive = build_corpus_archive(
        output,
        (second, ignored_text, external, first, second),
    )
    assert isinstance(archive, bytes)
    converted, inferred_mime = convert_data_to_bytes_and_infer_mime(
        archive,
        unsupported_error=AssertionError("Streamlit rejected the archive"),
    )
    assert converted == archive
    assert inferred_mime == "application/octet-stream"
    with ZipFile(BytesIO(archive)) as bundle:
        assert bundle.namelist() == [
            "tei-output/corpus-a.xml",
            "tei-output/corpus-a/session/document.xml",
        ]
        assert bundle.read("tei-output/corpus-a.xml") == b"<teiCorpus/>"
        assert bundle.read("tei-output/corpus-a/session/document.xml") == b"<TEI/>"


def test_web_archive_can_include_only_current_run_audit_metadata(tmp_path):
    output = tmp_path / "tei-output"
    metadata = tmp_path / "tei-output-metadata"
    document = output / "corpus-a" / "document.xml"
    document.parent.mkdir(parents=True)
    document.write_text("<TEI/>", encoding="utf-8")

    document_metadata = metadata / "corpus-a" / "document"
    document_metadata.mkdir(parents=True)
    for filename in ("data.json", "diagnostics.json", "status.json", "debug.log"):
        (document_metadata / filename).write_text(filename, encoding="utf-8")
    manifest_path = metadata / "batch-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    log_path = metadata / "_ui" / "current-run" / "pipeline.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("complete", encoding="utf-8")

    cached_source = metadata / "_sistory-downloads" / "source.pdf"
    cached_source.parent.mkdir()
    cached_source.write_bytes(b"PDF")
    old_log = metadata / "_ui" / "old-run" / "pipeline.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("old", encoding="utf-8")
    unrelated = metadata / "unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")

    manifest: dict[str, object] = {
        "items": [{"output": str(document), "status": "ok"}],
    }
    audit_files = manifest_metadata_artifacts(
        manifest,
        output,
        metadata,
        manifest_path=manifest_path,
        log_path=log_path,
    )

    assert {path.relative_to(metadata).as_posix() for path in audit_files} == {
        "_ui/current-run/pipeline.log",
        "batch-manifest.json",
        "corpus-a/document/data.json",
        "corpus-a/document/debug.log",
        "corpus-a/document/diagnostics.json",
        "corpus-a/document/status.json",
    }

    archive = build_corpus_archive(
        output,
        (document,),
        metadata_root=metadata,
        metadata_artifacts=audit_files,
    )
    with ZipFile(BytesIO(archive)) as bundle:
        names = set(bundle.namelist())
        assert "tei-output/corpus-a/document.xml" in names
        assert "tei-output-metadata/batch-manifest.json" in names
        assert "tei-output-metadata/corpus-a/document/diagnostics.json" in names
        assert all("_sistory-downloads" not in name for name in names)
        assert all("old-run" not in name for name in names)
        assert all("unrelated.json" not in name for name in names)


def test_web_text_and_log_helpers_are_bounded(tmp_path):
    assert parse_lines('  "first.pdf" \n\n second.pdf ') == (
        "first.pdf",
        "second.pdf",
    )
    log = tmp_path / "pipeline.log"
    log.write_bytes(b"first line\nsecond line\nthird line\n")

    assert log_tail(log, max_bytes=23) == "second line\nthird line"


def test_streamlit_app_loads_without_exceptions():
    app_path = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(app_path), default_timeout=15).run()

    assert list(app.exception) == []
    assert [title.value for title in app.title] == ["Build a TEI corpus"]
    corpus_prefix = next(
        widget for widget in app.text_input if widget.label == "Corpus prefix"
    )
    assert corpus_prefix.value == "ParlaMint"
    assert "Start pipeline" in [button.label for button in app.button]
