from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import batch_parse
import doc2tei.sistory as sistory
from doc2tei.sistory import SIstoryDownloadResult


def test_sistory_adapter_accepts_paths_and_urls_and_returns_stats(
    tmp_path, monkeypatch
):
    observed = {}

    @dataclass(frozen=True)
    class FakeOptions:
        root_segments: list[str]
        output_directory: Path
        dry_run: bool

    @dataclass
    class FakeStats:
        folders: int = 2
        publications: int = 3
        files_found: int = 4
        downloaded: int = 1
        renamed: int = 0
        skipped: int = 3
        failed: int = 0

    def run(options):
        observed["options"] = options
        return FakeStats()

    def filesystem_path(path):
        observed["filesystem_path"] = path
        return path / "from-downloader"

    module = SimpleNamespace(
        Options=FakeOptions,
        run=run,
        filesystem_path=filesystem_path,
    )
    monkeypatch.setattr(sistory, "_load_sistory_module", lambda _path: module)

    result = sistory.download_sistory_menu(
        "https://sistory.si/slv/menu/1/7/397/407",
        tmp_path / "downloads",
        downloader_directory=tmp_path / "sistory-dl",
    )

    assert result.status == "ok"
    assert result.menu_path == "1/7/397/407"
    assert result.stats["files_found"] == 4
    assert observed["options"].root_segments == ["1", "7", "397", "407"]
    assert observed["options"].dry_run is False

    filesystem_result = sistory.sistory_filesystem_path(
        tmp_path,
        downloader_directory=tmp_path / "sistory-dl",
    )
    assert filesystem_result == tmp_path.absolute() / "from-downloader"
    assert observed["filesystem_path"] == tmp_path.absolute()


def test_sistory_adapter_turns_loader_and_download_failures_into_status(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        sistory,
        "_load_sistory_module",
        lambda _path: (_ for _ in ()).throw(ImportError("requests unavailable")),
    )

    result = sistory.download_sistory_menu(
        "1/7/397/407",
        tmp_path,
        downloader_directory=tmp_path / "missing",
    )

    assert result.status == "failed"
    assert "ImportError: requests unavailable" in result.message
    assert sistory.sistory_filesystem_path(
        tmp_path,
        downloader_directory=tmp_path / "missing",
    ) == tmp_path.absolute()
    invalid = sistory.download_sistory_menu("../bad", tmp_path)
    assert invalid.status == "failed"
    wrong_site = sistory.download_sistory_menu(
        "https://example.com/slv/menu/1/7",
        tmp_path,
    )
    assert wrong_site.status == "failed"


@pytest.mark.parametrize(
    ("download_status", "expected_exit", "manifest_status"),
    [("ok", 0, "complete"), ("partial", 1, "incomplete")],
)
def test_batch_cli_downloads_a_sistory_menu_then_parses_the_cache(
    tmp_path,
    monkeypatch,
    download_status,
    expected_exit,
    manifest_status,
):
    output = tmp_path / "output"

    def fake_download(menu_path, download_directory, **_kwargs):
        source_folder = Path(download_directory) / "Downloaded menu"
        source_folder.mkdir(parents=True)
        (source_folder / "01 - publication.pdf").write_bytes(b"not a real PDF")
        return SIstoryDownloadResult(
            menu_path=menu_path,
            output=str(download_directory),
            status=download_status,
            stats={
                "folders": 1,
                "publications": 1,
                "files_found": 1,
                "downloaded": 1,
                "renamed": 0,
                "skipped": 0,
                "failed": 1 if download_status == "partial" else 0,
            },
            message="one failed file" if download_status == "partial" else "",
        )

    monkeypatch.setattr(batch_parse, "download_sistory_menu", fake_download)

    assert batch_parse.main(
        [
            "--sistory-menu",
            "1/7/397/407",
            "--output-dir",
            str(output),
            "--list-person-scope",
            "folder",
            "--quiet",
        ]
    ) == expected_exit

    manifest = json.loads(
        (output / "batch-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == manifest_status
    assert manifest["document_count"] == 1
    assert manifest["sistory_downloads"][0]["menu_path"] == "1/7/397/407"
    assert manifest["counts"]["recovered"] == 1
    assert manifest["list_person"]["scope"] == "folder"
    assert len(manifest["list_person"]["outputs"]) == 1
    bundle = output / "Downloaded menu" / "01 - publication"
    assert (bundle / "document.xml").is_file()
    assert (bundle / "diagnostics.json").is_file()
    assert not (bundle / "listPerson.xml").exists()
    assert (output / "Downloaded menu" / "listPerson.xml").is_file()
