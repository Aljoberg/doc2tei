# i am NOT going to be blamed for a stupid oversight that makes the whole thing not work
# here's a 600 line batch parser, fuck you

from __future__ import annotations

from collections.abc import Callable, Iterable, MutableMapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from typing import cast
import xml.etree.ElementTree as ET

import engine
from type_decs import BatchStatus

from .parser import (
    LoadedConfig,
    ParseDiagnostics,
    ParseResult,
    load_config,
    parse_document,
)
from .tei_header import SourceBibl, TEIHeader


SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx"})
BATCH_MANIFEST_NAME = "batch-manifest.json"
BUNDLE_STATUS_NAME = "status.json"
MAX_BUNDLE_PATH_LENGTH = 240
MAX_BUNDLE_COMPONENT_LENGTH = 100
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class BatchJob:
    source: str
    bundle: str


@dataclass(frozen=True)
class BatchOptions:
    config: str
    pretty: bool = False
    xml_declaration: bool = False
    write_list_person: bool = True
    include_wikidata: bool = False
    wikidata_timeout: float = 20.0
    page_workers: int | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class BatchItemResult:
    source: str
    output: str
    status: BatchStatus
    elapsed_seconds: float
    chunk_count: int = 0
    recovery_count: int = 0
    message: str = ""
    warning_count: int = 0

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["elapsed_seconds"] = round(self.elapsed_seconds, 3)
        return value


def normalize_extensions(extensions: Iterable[str] | None) -> frozenset[str]:
    if extensions is None:
        return SUPPORTED_EXTENSIONS
    normalized: set[str] = set()
    for extension in extensions:
        cleaned = extension.strip().casefold()
        if cleaned:
            normalized.add(cleaned if cleaned.startswith(".") else f".{cleaned}")
    return frozenset(normalized or SUPPORTED_EXTENSIONS)


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _unique_bundle_path(relative: Path, used: set[str], suffix: str) -> Path:
    candidate = relative
    key = candidate.as_posix().casefold()
    if key not in used:
        used.add(key)
        return candidate

    candidate = relative.with_name(f"{relative.name}-{suffix}")
    number = 2
    while candidate.as_posix().casefold() in used:
        candidate = relative.with_name(f"{relative.name}-{suffix}-{number}")
        number += 1
    used.add(candidate.as_posix().casefold())
    return candidate


def _safe_bundle_component(value: str) -> str:
    cleaned = "".join(
        "-" if ord(character) < 32 or character in '<>:"/\\|?*' else character
        for character in value
    ).rstrip(" .")
    cleaned = cleaned or "document"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) <= MAX_BUNDLE_COMPONENT_LENGTH:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]
    prefix_length = MAX_BUNDLE_COMPONENT_LENGTH - len(digest) - 1
    return f"{cleaned[:prefix_length].rstrip(' .-')}-{digest}"


def _safe_relative_bundle(relative: Path, source: Path, output: Path) -> Path:
    safe = Path(*(_safe_bundle_component(part) for part in relative.parts))
    longest_output = output / safe / "diagnostics.json"
    if len(str(longest_output)) <= MAX_BUNDLE_PATH_LENGTH:
        return safe
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
    stem = _safe_bundle_component(source.stem)
    suffix = f"-{digest}"
    fixed = output / "_long-paths" / suffix / "diagnostics.json"
    available = MAX_BUNDLE_PATH_LENGTH - len(str(fixed))
    maximum_stem = max(8, min(available, MAX_BUNDLE_COMPONENT_LENGTH - len(suffix)))
    return Path("_long-paths") / f"{stem[:maximum_stem].rstrip(' .-')}-{digest}"


def discover_batch_jobs(
    inputs: Sequence[str | Path],
    output_root: str | Path,
    *,
    recursive: bool = True,
    extensions: Iterable[str] | None = None,
) -> tuple[list[BatchJob], list[str]]:
    """Discover supported documents and assign collision-free output bundles."""

    output = Path(output_root).expanduser().resolve()
    allowed = normalize_extensions(extensions)
    specifications = [Path(value).expanduser().resolve() for value in inputs]
    multiple_inputs = len(specifications) > 1
    candidates: list[tuple[Path, Path]] = []
    warnings: list[str] = []

    for specification in specifications:
        if not specification.exists():
            warnings.append(f"input does not exist: {specification}")
            continue
        if specification.is_file():
            if specification.suffix.casefold() not in allowed:
                warnings.append(f"unsupported input type: {specification}")
                continue
            relative = (
                Path(specification.parent.name) / specification.stem
                if multiple_inputs
                else Path(specification.stem)
            )
            candidates.append((specification, relative))
            continue
        if not specification.is_dir():
            warnings.append(
                f"input is not a regular file or directory: {specification}"
            )
            continue

        iterator = specification.rglob("*") if recursive else specification.glob("*")
        prefix = Path(specification.name) if multiple_inputs else Path()
        output_is_nested = output != specification and _is_within(
            output, specification
        )
        for source in iterator:
            resolved = source.resolve()
            if (
                not source.is_file()
                or source.suffix.casefold() not in allowed
                or (output_is_nested and _is_within(resolved, output))
            ):
                continue
            relative = prefix / source.relative_to(specification).with_suffix("")
            candidates.append((resolved, relative))

    jobs: list[BatchJob] = []
    seen_sources: set[str] = set()
    used_bundles: set[str] = set()
    for source, relative in sorted(
        candidates,
        key=lambda item: (item[1].as_posix().casefold(), str(item[0]).casefold()),
    ):
        source_key = os.path.normcase(str(source))
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        safe_relative = _safe_relative_bundle(
            relative if relative.name else Path("document"),
            source,
            output,
        )
        unique_relative = _unique_bundle_path(
            safe_relative,
            used_bundles,
            source.suffix.removeprefix(".").casefold() or "document",
        )
        jobs.append(BatchJob(str(source), str(output / unique_relative)))
    return jobs, warnings


def automatic_document_workers(document_count: int) -> int:
    """Choose conservative document concurrency for mixed-size collections."""

    if document_count <= 0:
        return 1
    available = max(1, (os.cpu_count() or 1) - 1)
    return min(document_count, available, 4)


def _atomic_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")


def _atomic_json(path: Path, value: object) -> None:
    temporary = _atomic_path(path)
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_result_write(path: Path, writer: Callable[[Path], None]) -> None:
    temporary = _atomic_path(path)
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _file_identity(path: Path) -> dict[str, object]:
    try:
        stat = path.stat()
        return {
            "path": str(path),
            "exists": True,
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
        }
    except OSError:
        return {"path": str(path), "exists": False}


@lru_cache(maxsize=1)
def _implementation_identity() -> dict[str, dict[str, object]]:
    package = Path(__file__).parent
    files = {
        "batch": Path(__file__),
        "engine": Path(engine.__file__),
        "extractors": package / "extractors.py",
        "helpers": package / "helpers.py",
        "parser": package / "parser.py",
        "tei_header": package / "tei_header.py",
        "types": package.parent / "type_decs.py",
    }
    return {name: _file_identity(path.resolve()) for name, path in files.items()}


def _job_fingerprint(job: BatchJob, options: BatchOptions) -> dict[str, object]:
    source = Path(job.source)
    config = Path(options.config)
    return {
        "source": _file_identity(source),
        "config": _file_identity(config),
        "implementation": _implementation_identity(),
        "options": {
            "pretty": options.pretty,
            "xml_declaration": options.xml_declaration,
            "write_list_person": options.write_list_person,
            "include_wikidata": options.include_wikidata,
            "wikidata_timeout": options.wikidata_timeout,
            "page_workers": options.page_workers,
        },
    }


def _required_outputs(bundle: Path, options: BatchOptions) -> list[Path]:
    required = [
        bundle / "document.xml",
        bundle / "diagnostics.json",
        bundle / "data.json",
    ]
    if options.write_list_person:
        required.append(bundle / "listPerson.xml")
    return required


def _completed_status(
    bundle: Path,
    fingerprint: dict[str, object],
    options: BatchOptions,
) -> dict[str, object] | None:
    if options.overwrite:
        return None
    status_path = bundle / BUNDLE_STATUS_NAME
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(status, dict):
        return None
    if status.get("status") not in {"ok", "recovered"}:
        return None
    if status.get("fingerprint") != fingerprint:
        return None
    complete = all(path.is_file() for path in _required_outputs(bundle, options))
    return status if complete else None


def _configure_page_workers(loaded: LoadedConfig, workers: int | None) -> None:
    if workers is None:
        return
    if isinstance(loaded.config, MutableMapping):
        cast(MutableMapping[str, object], loaded.config)["page_workers"] = workers


def _result_recovery_count(result: ParseResult) -> int:
    diagnostic_count = sum(result.diagnostics.recovery_counts.values())
    exported = result.data.get("recoveries")
    exported_count = len(exported) if isinstance(exported, list) else 0
    # Configs often mirror parser recoveries into result.data for serialization.
    # Taking the larger count avoids reporting the same recovery twice.
    return max(diagnostic_count, exported_count)


def _result_warning_count(result: ParseResult) -> int:
    exported = result.data.get("warnings")
    return len(exported) if isinstance(exported, list) else 0


def _write_result_bundle(
    result: ParseResult,
    bundle: Path,
    options: BatchOptions,
) -> None:
    _atomic_result_write(
        bundle / "document.xml",
        lambda path: result.write_xml(
            path,
            xml_declaration=options.xml_declaration,
            pretty=options.pretty,
        ),
    )
    _atomic_result_write(bundle / "diagnostics.json", result.write_diagnostics)
    _atomic_result_write(bundle / "data.json", result.write_data)
    if options.write_list_person:
        _atomic_result_write(
            bundle / "listPerson.xml",
            lambda path: result.write_list_person(
                path,
                xml_declaration=options.xml_declaration,
                pretty=options.pretty,
                include_wikidata=options.include_wikidata,
                # Documents already run concurrently. Keep network fan-out
                # bounded instead of multiplying it by another four threads.
                wikidata_workers=1,
                wikidata_timeout=options.wikidata_timeout,
            ),
        )
    else:
        (bundle / "listPerson.xml").unlink(missing_ok=True)


def _failure_parse_result(
    source: Path,
    config: Path,
    error: BaseException | str,
) -> ParseResult:
    message = str(error)
    if not isinstance(error, str):
        message = f"{type(error).__name__}: {message}"

    root, content = engine.default_document()
    header = TEIHeader(
        main_titles={"": source.stem},
        source=SourceBibl(titles={"": source.name}),
    ).build()
    file_desc = header.find("fileDesc")
    if file_desc is not None:
        notes = ET.Element("notesStmt")
        note = ET.SubElement(notes, "note", type="conversionRecovery", n="1")
        note.text = f"batch.document: {message}"
        source_desc = file_desc.find("sourceDesc")
        index = (
            list(file_desc).index(source_desc)
            if source_desc is not None
            else len(file_desc)
        )
        file_desc.insert(index, notes)
    root.insert(0, header)
    content.append(ET.Comment("doc2tei: source document could not be parsed"))
    paragraph = ET.SubElement(content, "p", type="unparsed")
    paragraph.text = (
        "The source document could not be converted. Its path and the recovery "
        "reason are retained in the diagnostics and TEI header."
    )

    diagnostics = ParseDiagnostics(input=str(source), config=str(config))
    diagnostics.recover("batch.document", message)
    return ParseResult(
        root=root,
        diagnostics=diagnostics,
        data={
            "recoveries": [f"batch.document: {message}"],
            "source": str(source),
        },
    )


def _write_status(
    job: BatchJob,
    fingerprint: dict[str, object],
    result: BatchItemResult,
) -> None:
    status = result.as_dict()
    status.update(
        {
            "completed_at": utc_now(),
            "fingerprint": fingerprint,
        }
    )
    _atomic_json(Path(job.bundle) / BUNDLE_STATUS_NAME, status)


def process_batch_job(job: BatchJob, options: BatchOptions) -> BatchItemResult:
    """Convert one job; always attempt a reviewable bundle on parser failure."""

    started = time.perf_counter()
    bundle = Path(job.bundle)
    source = Path(job.source)
    config = Path(options.config)
    bundle.mkdir(parents=True, exist_ok=True)
    fingerprint = _job_fingerprint(job, options)
    previous = _completed_status(bundle, fingerprint, options)
    if previous is not None:
        return BatchItemResult(
            source=str(source),
            output=str(bundle),
            status="skipped",
            elapsed_seconds=time.perf_counter() - started,
            chunk_count=int(previous.get("chunk_count", 0)),
            recovery_count=int(previous.get("recovery_count", 0)),
            message="unchanged completed bundle",
            warning_count=int(previous.get("warning_count", 0)),
        )

    debug_temporary = _atomic_path(bundle / "debug.log")
    final_result: BatchItemResult
    try:
        with debug_temporary.open("w", encoding="utf-8") as debug_stream:
            try:
                loaded = load_config(config)
                _configure_page_workers(loaded, options.page_workers)
                with redirect_stdout(debug_stream), redirect_stderr(debug_stream):
                    parsed = parse_document(source, config=loaded)
                    _write_result_bundle(parsed, bundle, options)
                recoveries = _result_recovery_count(parsed)
                warnings = _result_warning_count(parsed)
                status: BatchStatus = "recovered" if recoveries else "ok"
                final_result = BatchItemResult(
                    source=str(source),
                    output=str(bundle),
                    status=status,
                    elapsed_seconds=time.perf_counter() - started,
                    chunk_count=parsed.diagnostics.chunk_count,
                    recovery_count=recoveries,
                    message=(
                        "completed with recoveries"
                        if recoveries
                        else "completed with warnings" if warnings else ""
                    ),
                    warning_count=warnings,
                )
            except Exception as error:
                traceback.print_exc(file=debug_stream)
                fallback = _failure_parse_result(source, config, error)
                _write_result_bundle(fallback, bundle, options)
                final_result = BatchItemResult(
                    source=str(source),
                    output=str(bundle),
                    status="recovered",
                    elapsed_seconds=time.perf_counter() - started,
                    recovery_count=_result_recovery_count(fallback),
                    message=f"{type(error).__name__}: {error}"[:500],
                )

        debug_path = bundle / "debug.log"
        if debug_temporary.stat().st_size:
            os.replace(debug_temporary, debug_path)
        else:
            debug_temporary.unlink(missing_ok=True)
            debug_path.unlink(missing_ok=True)
        _write_status(job, fingerprint, final_result)
        return final_result
    except Exception as error:
        debug_temporary.unlink(missing_ok=True)
        failed = BatchItemResult(
            source=str(source),
            output=str(bundle),
            status="failed",
            elapsed_seconds=time.perf_counter() - started,
            message=f"{type(error).__name__}: {error}"[:500],
        )
        try:
            _write_status(job, fingerprint, failed)
        except Exception:
            pass
        return failed


def _recover_worker_failure(
    job: BatchJob,
    options: BatchOptions,
    error: BaseException,
) -> BatchItemResult:
    """Create a fallback bundle if a worker process itself terminates."""

    started = time.perf_counter()
    bundle = Path(job.bundle)
    source = Path(job.source)
    config = Path(options.config)
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        fallback = _failure_parse_result(
            source,
            config,
            f"worker process failed: {type(error).__name__}: {error}",
        )
        _write_result_bundle(fallback, bundle, options)
        result = BatchItemResult(
            source=str(source),
            output=str(bundle),
            status="recovered",
            elapsed_seconds=time.perf_counter() - started,
            recovery_count=_result_recovery_count(fallback),
            message=f"worker process failed: {type(error).__name__}: {error}"[:500],
        )
        _write_status(job, _job_fingerprint(job, options), result)
        return result
    except Exception as fallback_error:
        return BatchItemResult(
            source=str(source),
            output=str(bundle),
            status="failed",
            elapsed_seconds=time.perf_counter() - started,
            message=(
                f"worker failed ({type(error).__name__}: {error}); fallback failed "
                f"({type(fallback_error).__name__}: {fallback_error})"
            )[:500],
        )


def run_batch(
    jobs: Sequence[BatchJob],
    options: BatchOptions,
    *,
    workers: int,
    recycle_workers: bool = True,
    on_result: Callable[[BatchItemResult, int, int], None] | None = None,
) -> list[BatchItemResult]:
    """Run jobs in isolated processes and return results in discovery order."""

    worker_count = max(1, min(workers, len(jobs))) if jobs else 1
    completed = 0
    by_source: dict[str, BatchItemResult] = {}

    def record(result: BatchItemResult) -> None:
        nonlocal completed
        completed += 1
        by_source[result.source] = result
        if on_result is not None:
            on_result(result, completed, len(jobs))

    if worker_count == 1:
        for job in jobs:
            record(process_batch_job(job, options))
    else:
        executor_options: dict[str, object] = {"max_workers": worker_count}
        if recycle_workers and sys.version_info >= (3, 11):
            executor_options["max_tasks_per_child"] = 1
        with ProcessPoolExecutor(**executor_options) as executor:
            remaining = iter(jobs)
            pending: dict[Future[BatchItemResult], BatchJob] = {}
            exhausted = False

            def submit_one() -> None:
                nonlocal exhausted
                try:
                    job = next(remaining)
                except StopIteration:
                    exhausted = True
                    return
                try:
                    future = executor.submit(process_batch_job, job, options)
                except Exception as error:
                    record(_recover_worker_failure(job, options, error))
                else:
                    pending[future] = job

            # Keep only a small queue ahead of the active workers. Thousands
            # of paths should not become thousands of live Future objects.
            queue_limit = worker_count * 2
            while not exhausted and len(pending) < queue_limit:
                submit_one()
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    job = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        result = _recover_worker_failure(job, options, error)
                    record(result)
                while not exhausted and len(pending) < queue_limit:
                    submit_one()

    return [by_source[job.source] for job in jobs]


def write_batch_manifest(path: str | Path, manifest: dict[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(destination, manifest)


def batch_counts(results: Sequence[BatchItemResult]) -> dict[str, int]:
    counts = {status: 0 for status in ("ok", "recovered", "failed", "skipped")}
    for result in results:
        counts[result.status] += 1
    return counts
