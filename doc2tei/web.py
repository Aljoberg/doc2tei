"""Process and artifact helpers shared by the local Streamlit interface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import cast

from type_decs import PipelineRequest, PipelineRun, UploadedDocument

from .batch import BATCH_MANIFEST_NAME, normalize_corpus_code
from .sistory import normalize_sistory_menu_path

SUPPORTED_UPLOAD_SUFFIXES = frozenset({".pdf", ".docx"})
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def parse_lines(value: str) -> tuple[str, ...]:
    """Return trimmed, nonempty lines while preserving their order."""

    return tuple(
        line.strip().strip('"')
        for line in value.splitlines()
        if line.strip().strip('"')
    )


def validate_pipeline_request(request: PipelineRequest) -> list[str]:
    """Validate a UI request before it starts an external batch process."""

    errors: list[str] = []
    if not request.local_inputs and not request.uploads and not request.sistory_menus:
        errors.append("Choose at least one local path, upload, or SIstory menu.")
    if not request.config.is_file():
        errors.append(f"Config file does not exist: {request.config}")
    for source in request.local_inputs:
        if not source.exists():
            errors.append(f"Input does not exist: {source}")
        elif (
            source.is_file()
            and source.suffix.casefold() not in SUPPORTED_UPLOAD_SUFFIXES
        ):
            errors.append(f"Unsupported input file: {source}")
    for upload in request.uploads:
        if Path(upload.name).suffix.casefold() not in SUPPORTED_UPLOAD_SUFFIXES:
            errors.append(f"Unsupported upload: {upload.name}")
    for menu in request.sistory_menus:
        try:
            normalize_sistory_menu_path(menu)
        except ValueError as error:
            errors.append(f"Invalid SIstory menu {menu!r}: {error}")
    if request.workers < 0:
        errors.append("Document workers cannot be negative.")
    if request.page_workers is not None and request.page_workers < 0:
        errors.append("Page workers cannot be negative.")
    if request.include_wikidata and not request.write_list_person:
        errors.append("Wikidata enrichment requires person-list generation.")
    try:
        normalize_corpus_code(request.corpus_code)
    except ValueError as error:
        errors.append(str(error))
    if not request.corpus_language.strip():
        errors.append("Corpus language cannot be empty.")

    output = request.output_root.resolve()
    metadata = request.metadata_root.resolve()
    if (
        output == metadata
        or _is_within(output, metadata)
        or _is_within(metadata, output)
    ):
        errors.append(
            "Corpus and audit directories must be separate, non-nested locations."
        )
    return errors


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _safe_upload_name(name: str) -> str:
    basename = Path(name.replace("\\", "/")).name
    cleaned = _UNSAFE_FILENAME_RE.sub("-", basename).rstrip(" .")
    return cleaned or "document.pdf"


def write_uploads(
    uploads: Sequence[UploadedDocument],
    directory: Path,
) -> list[Path]:
    """Persist uploaded documents with safe, collision-free filenames."""

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    used: set[str] = set()
    for upload in uploads:
        safe_name = _safe_upload_name(upload.name)
        candidate = Path(safe_name)
        number = 2
        while candidate.name.casefold() in used:
            candidate = Path(f"{Path(safe_name).stem}-{number}{Path(safe_name).suffix}")
            number += 1
        used.add(candidate.name.casefold())
        destination = directory / candidate
        destination.write_bytes(upload.data)
        written.append(destination)
    return written


def build_batch_command(
    request: PipelineRequest,
    project_root: Path,
    *,
    upload_directory: Path | None = None,
) -> list[str]:
    """Translate a validated web request into the canonical batch CLI."""

    command = [
        sys.executable,
        "-u",
        str(project_root / "batch_parse.py"),
        "--output-dir",
        str(request.output_root),
        "--metadata-dir",
        str(request.metadata_root),
        "--config",
        str(request.config),
        "--workers",
        str(request.workers),
        "--list-person-scope",
        request.list_person_scope,
        "--corpus-lang",
        request.corpus_language,
        "--corpus-code",
        normalize_corpus_code(request.corpus_code),
    ]
    if request.page_workers is not None:
        command.extend(("--page-workers", str(request.page_workers)))
    if not request.recursive:
        command.append("--no-recursive")
    if not request.write_list_person:
        command.append("--no-list-person")
    if request.emit_corpus:
        command.append("--emit-corpus-xml")
    if request.include_wikidata:
        command.append("--include-wikidata")
    if request.pretty:
        command.append("--pretty")
    if request.xml_declaration:
        command.append("--xml-declaration")
    if request.overwrite:
        command.append("--overwrite")
    if request.reuse_workers:
        command.append("--reuse-workers")
    if request.sistory_download_root is not None:
        command.extend(("--sistory-download-dir", str(request.sistory_download_root)))
    for menu in request.sistory_menus:
        command.extend(("--sistory-menu", normalize_sistory_menu_path(menu)))
    command.extend(str(path) for path in request.local_inputs)
    if upload_directory is not None:
        command.append(str(upload_directory))
    return command


def launch_pipeline(request: PipelineRequest, project_root: Path) -> PipelineRun:
    """Start the batch CLI and redirect its complete output to an audit log."""

    errors = validate_pipeline_request(request)
    if errors:
        raise ValueError("\n".join(errors))

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_directory = request.metadata_root / "_ui" / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    upload_directory: Path | None = None
    if request.uploads:
        upload_directory = run_directory / "uploads"
        write_uploads(request.uploads, upload_directory)

    command = build_batch_command(
        request,
        project_root,
        upload_directory=upload_directory,
    )
    log_path = run_directory / "pipeline.log"
    environment = os.environ.copy()
    environment.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    with log_path.open("ab", buffering=0) as log_stream:
        if os.name == "nt":
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=environment,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                ),
            )
        else:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=environment,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    return PipelineRun(
        run_id=run_id,
        process=process,
        command=tuple(command),
        output_root=request.output_root,
        metadata_root=request.metadata_root,
        manifest_path=request.metadata_root / BATCH_MANIFEST_NAME,
        log_path=log_path,
        started_at_ns=time.time_ns(),
    )


def stop_pipeline(run: PipelineRun, timeout: float = 5.0) -> None:
    """Stop the batch parent and its worker processes."""

    if run.process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(run.process.pid), "/T", "/F"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(run.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        run.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        run.process.kill()


def read_manifest(run: PipelineRun) -> dict[str, object] | None:
    """Read this run's manifest, ignoring an older manifest at the same path."""

    try:
        stat = run.manifest_path.stat()
        if stat.st_mtime_ns + 1_000_000_000 < run.started_at_ns:
            return None
        value = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def manifest_counts(manifest: Mapping[str, object] | None) -> dict[str, int]:
    counts = manifest.get("counts") if manifest is not None else None
    if not isinstance(counts, Mapping):
        return {}
    return {
        str(name): value for name, value in counts.items() if isinstance(value, int)
    }


def manifest_items(
    manifest: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    items = manifest.get("items") if manifest is not None else None
    if not isinstance(items, list):
        return []
    return [cast(dict[str, object], item) for item in items if isinstance(item, dict)]


def manifest_artifacts(manifest: Mapping[str, object] | None) -> list[Path]:
    """Return existing XML artifacts named by the manifest in stable order."""

    candidates: list[str] = []
    for item in manifest_items(manifest):
        output = item.get("output")
        if isinstance(output, str):
            candidates.append(output)
    if manifest is not None:
        for section_name in ("list_person", "list_org", "corpus"):
            section = manifest.get(section_name)
            if not isinstance(section, Mapping):
                continue
            outputs = section.get("outputs")
            if isinstance(outputs, list):
                candidates.extend(value for value in outputs if isinstance(value, str))

    artifacts: list[Path] = []
    seen: set[str] = set()
    for value in candidates:
        path = Path(value)
        key = os.path.normcase(str(path.resolve()))
        if key not in seen and path.is_file() and path.suffix.casefold() == ".xml":
            seen.add(key)
            artifacts.append(path)
    return artifacts


def log_tail(path: Path, max_bytes: int = 64_000) -> str:
    """Read a bounded UTF-8 tail without loading an unbounded run log."""

    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - max_bytes)
            prefix = b""
            if start:
                stream.seek(start - 1)
                prefix = stream.read(1)
            else:
                stream.seek(0)
            data = stream.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if start and prefix != b"\n":
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text
    return text.rstrip()


def command_display(command: Sequence[str]) -> str:
    """Render a copyable command with conservative quoting."""

    return (
        subprocess.list2cmdline(command)
        if os.name == "nt"
        else " ".join(_shell_quote(value) for value in command)
    )


def _shell_quote(value: str) -> str:
    if value and all(character.isalnum() or character in "/._-" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
