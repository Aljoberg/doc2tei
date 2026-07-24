from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from doc2tei.web import (
    build_batch_command,
    log_tail,
    manifest_artifacts,
    manifest_counts,
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
    assert "Start pipeline" in [button.label for button in app.button]
