"""Command-line entry point for parallel doc2tei batch conversion."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys

from doc2tei.batch import (
    BATCH_MANIFEST_NAME,
    BatchItemResult,
    BatchJob,
    BatchOptions,
    automatic_document_workers,
    batch_counts,
    default_metadata_root,
    discover_batch_jobs,
    normalize_corpus_code,
    run_batch,
    utc_now,
    write_batch_corpus_outputs,
    write_batch_list_person_outputs,
    write_batch_manifest,
)
from doc2tei.sistory import (
    DEFAULT_SISTORY_DL_DIRECTORY,
    SIstoryDownloadResult,
    download_sistory_menu,
    normalize_sistory_menu_path,
    sistory_filesystem_path,
)

DEFAULT_CONFIG = Path(__file__).parent / "examples" / "general-config" / "config.py"


def nonnegative_integer(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def corpus_code(value: str) -> str:
    try:
        return normalize_corpus_code(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert PDF/DOCX files into independent TEI output bundles, "
            "continuing after per-document failures"
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="input files or directories (optional with --sistory-menu)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="root corpus directory for TEI XML deliverables",
    )
    parser.add_argument(
        "--metadata-dir",
        help=(
            "audit root for diagnostics, data, status, debug logs, manifest, and "
            "the default SIstory cache (default: sibling OUTPUT_DIR-metadata)"
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help="configuration file (defaults to examples/general-config/config.py)",
    )
    parser.add_argument(
        "--sistory-menu",
        action="append",
        metavar="PATH",
        help=(
            "download a SIstory menu path before parsing; repeat for multiple "
            "menus (example: 1/7/397/407)"
        ),
    )
    parser.add_argument(
        "--sistory-download-dir",
        help=(
            "persistent SIstory download cache (default: "
            "METADATA_DIR/_sistory-downloads)"
        ),
    )
    parser.add_argument(
        "--sistory-dl-path",
        default=str(DEFAULT_SISTORY_DL_DIRECTORY),
        help="path to the sistory-dl checkout/submodule",
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=nonnegative_integer,
        default=0,
        help="parallel documents; 0 chooses up to four workers automatically",
    )
    parser.add_argument(
        "--page-workers",
        type=nonnegative_integer,
        help=(
            "page-extraction workers inside each document; by default the config "
            "is used for one document and 1 is forced for parallel documents"
        ),
    )
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="search input directories recursively (default: enabled)",
    )
    parser.add_argument(
        "--extension",
        action="append",
        help="accepted extension; repeat as needed (default: .pdf and .docx)",
    )
    parser.add_argument(
        "--no-list-person",
        action="store_true",
        help="do not generate listPerson.xml",
    )
    parser.add_argument(
        "--list-person-scope",
        choices=("document", "folder", "corpus"),
        default="document",
        help=(
            "generate one listPerson per document, source folder, or complete "
            "batch corpus (default: document)"
        ),
    )
    parser.add_argument(
        "--emit-corpus-xml",
        action="store_true",
        help=(
            "emit a teiCorpus for every recursive folder level; each corpus "
            "directly XIncludes all document components in its subtree"
        ),
    )
    parser.add_argument(
        "--corpus-lang",
        default="sl",
        help="xml:lang for the emitted corpus headers (default: sl)",
    )
    parser.add_argument(
        "--corpus-code",
        type=corpus_code,
        default="SI",
        help="ISO country/region code used in ParlaMint filenames (default: SI)",
    )
    parser.add_argument(
        "--include-wikidata",
        action="store_true",
        help="best-effort Wikidata enrichment for listPerson.xml",
    )
    parser.add_argument(
        "--wikidata-timeout",
        type=positive_float,
        default=20.0,
        help="per-request Wikidata read timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent XML outputs (uses more time and peak memory)",
    )
    parser.add_argument(
        "--xml-declaration",
        action="store_true",
        help="include XML declarations",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="reprocess unchanged completed bundles",
    )
    parser.add_argument(
        "--reuse-workers",
        action="store_true",
        help="reuse worker processes for speed instead of recycling after each file",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress lines")
    return parser


def _progress(result: BatchItemResult, current: int, total: int) -> None:
    label = Path(result.source).name
    detail = f" - {result.message}" if result.message else ""
    print(
        f"[{current}/{total}] {result.status.upper():9} {label} "
        f"({result.elapsed_seconds:.1f}s){detail}",
        flush=True,
    )


def _sistory_cache_directory(base: Path, normalized_menu_path: str) -> Path:
    readable = re.sub(r"[^\w.-]+", "-", normalized_menu_path.replace("/", "-")).strip(
        "-."
    )
    readable = readable[:80].rstrip("-.") or "menu"
    digest = hashlib.sha1(normalized_menu_path.encode("utf-8")).hexdigest()[:8]
    return base / f"{readable}-{digest}"


def _download_sistory_inputs(
    menu_paths: list[str],
    download_base: Path,
    downloader_directory: Path,
) -> tuple[list[Path], list[SIstoryDownloadResult]]:
    roots: list[Path] = []
    results: list[SIstoryDownloadResult] = []
    seen_menus: set[str] = set()
    for menu_path in menu_paths:
        try:
            normalized = normalize_sistory_menu_path(menu_path)
            if normalized in seen_menus:
                continue
            seen_menus.add(normalized)
            cache = _sistory_cache_directory(download_base, normalized)
        except ValueError as error:
            result = SIstoryDownloadResult(
                menu_path=menu_path,
                output=str(download_base),
                status="failed",
                stats={},
                message=str(error),
            )
        else:
            result = download_sistory_menu(
                normalized,
                cache,
                downloader_directory=downloader_directory,
            )
            # A failed refresh can still leave a useful prior cache. Discovery
            # below will parse any complete files already present there.
            if cache.is_dir():
                roots.append(
                    sistory_filesystem_path(
                        cache,
                        downloader_directory=downloader_directory,
                    )
                )
        results.append(result)
        if result.status != "ok":
            print(
                f"warning: SIstory {result.menu_path}: {result.message}",
                file=sys.stderr,
            )
    return roots, results


def _deduplicate_jobs(jobs: list[BatchJob]) -> list[BatchJob]:
    unique: dict[str, BatchJob] = {}
    for job in jobs:
        key = os.path.normcase(str(Path(job.source).resolve()))
        unique.setdefault(key, job)
    return list(unique.values())


def _migrate_legacy_artifact(source: Path, destination: Path) -> None:
    """Move an old in-corpus audit artifact without risking the batch run."""

    try:
        if os.path.normcase(str(source.absolute())) == os.path.normcase(
            str(destination.absolute())
        ):
            return
        if not source.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = destination
        if target.exists():
            stem, suffix = (
                (target.stem, target.suffix) if source.is_file() else (target.name, "")
            )
            number = 1
            while target.exists():
                marker = "legacy" if number == 1 else f"legacy-{number}"
                target = destination.with_name(f"{stem}-{marker}{suffix}")
                number += 1
        shutil.move(str(source), str(target))
    except OSError:
        # Audit migration is housekeeping. New output still belongs at the
        # destination even if an old file is locked or otherwise immovable.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.inputs and not args.sistory_menu:
        parser.error("provide an input file/directory or --sistory-menu")
    if args.include_wikidata and args.no_list_person:
        parser.error("--include-wikidata cannot be used with --no-list-person")

    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        parser.error(f"config file does not exist: {config}")
    output_root = Path(args.output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    metadata_root = (
        Path(args.metadata_dir).expanduser().resolve()
        if args.metadata_dir
        else default_metadata_root(output_root)
    )
    metadata_root.mkdir(parents=True, exist_ok=True)
    manifest_path = metadata_root / BATCH_MANIFEST_NAME
    _migrate_legacy_artifact(
        output_root / BATCH_MANIFEST_NAME,
        manifest_path,
    )
    started_at = utc_now()
    manifest: dict[str, object] = {
        "status": "acquiring",
        "started_at": started_at,
        "config": str(config),
        "output": str(output_root),
        "metadata_output": str(metadata_root),
        "list_person": {
            "enabled": not args.no_list_person,
            "scope": args.list_person_scope,
            "outputs": [],
        },
        "list_org": {
            "enabled": args.emit_corpus_xml and not args.no_list_person,
            "outputs": [],
        },
        "corpus": {
            "enabled": args.emit_corpus_xml,
            "code": args.corpus_code,
            "outputs": [],
        },
        "items": [],
    }
    write_batch_manifest(manifest_path, manifest)

    sistory_results: list[SIstoryDownloadResult] = []
    sistory_roots: list[Path] = []
    if args.sistory_menu:
        download_base = (
            Path(args.sistory_download_dir).expanduser().resolve()
            if args.sistory_download_dir
            else metadata_root / "_sistory-downloads"
        )
        if not args.sistory_download_dir:
            _migrate_legacy_artifact(
                output_root / "_sistory-downloads",
                download_base,
            )
        downloader_directory = Path(args.sistory_dl_path).expanduser().resolve()
        try:
            sistory_roots, sistory_results = _download_sistory_inputs(
                args.sistory_menu,
                download_base,
                downloader_directory,
            )
        except KeyboardInterrupt:
            manifest["status"] = "interrupted"
            manifest["completed_at"] = utc_now()
            write_batch_manifest(manifest_path, manifest)
            return 130
        manifest["sistory_downloads"] = [result.as_dict() for result in sistory_results]
        write_batch_manifest(manifest_path, manifest)

    jobs: list[BatchJob] = []
    warnings: list[str] = []
    if args.inputs:
        local_output = output_root / "local" if sistory_roots else output_root
        local_metadata = metadata_root / "local" if sistory_roots else metadata_root
        local_jobs, local_warnings = discover_batch_jobs(
            args.inputs,
            local_output,
            metadata_root=local_metadata,
            recursive=args.recursive,
            extensions=args.extension,
            corpus_code=args.corpus_code,
        )
        jobs.extend(local_jobs)
        warnings.extend(local_warnings)
    if sistory_roots:
        sistory_output = output_root / "sistory" if args.inputs else output_root
        sistory_metadata = metadata_root / "sistory" if args.inputs else metadata_root
        downloaded_jobs, downloaded_warnings = discover_batch_jobs(
            sistory_roots,
            sistory_output,
            metadata_root=sistory_metadata,
            recursive=True,
            extensions=args.extension,
            corpus_code=args.corpus_code,
        )
        jobs.extend(downloaded_jobs)
        warnings.extend(downloaded_warnings)
    jobs = _deduplicate_jobs(jobs)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not jobs:
        manifest.update(
            status="failed",
            completed_at=utc_now(),
            document_count=0,
            discovery_warnings=warnings,
            message="no supported documents were found",
        )
        write_batch_manifest(manifest_path, manifest)
        print("error: no supported documents were found", file=sys.stderr)
        return 1

    requested_workers = args.workers or automatic_document_workers(len(jobs))
    document_workers = max(1, min(requested_workers, len(jobs)))
    page_workers = args.page_workers
    if page_workers is None and document_workers > 1:
        page_workers = 1
    if document_workers > 1 and page_workers != 1:
        print(
            "warning: document and page parallelism are both enabled; this may "
            "oversubscribe CPU and memory",
            file=sys.stderr,
        )

    options = BatchOptions(
        config=str(config),
        pretty=args.pretty,
        xml_declaration=args.xml_declaration,
        write_list_person=not args.no_list_person,
        list_person_scope=args.list_person_scope,
        include_wikidata=args.include_wikidata,
        wikidata_timeout=args.wikidata_timeout,
        page_workers=page_workers,
        overwrite=args.overwrite,
        emit_corpus=args.emit_corpus_xml,
        corpus_language=args.corpus_lang,
        corpus_code=args.corpus_code,
    )
    manifest.update(
        status="running",
        document_workers=document_workers,
        page_workers="config" if page_workers is None else page_workers,
        document_count=len(jobs),
        list_person_scope=args.list_person_scope,
        corpus_code=args.corpus_code,
        discovery_warnings=warnings,
    )
    write_batch_manifest(manifest_path, manifest)

    observed: list[BatchItemResult] = []
    checkpoint_interval = max(1, min(50, len(jobs) // 100 or 1))

    def on_result(result: BatchItemResult, current: int, total: int) -> None:
        observed.append(result)
        if current == total or current % checkpoint_interval == 0:
            manifest["items"] = [item.as_dict() for item in observed]
            manifest["counts"] = batch_counts(observed)
            manifest["warning_count"] = sum(item.warning_count for item in observed)
            write_batch_manifest(manifest_path, manifest)
        if not args.quiet:
            _progress(result, current, total)

    try:
        results = run_batch(
            jobs,
            options,
            workers=document_workers,
            recycle_workers=not args.reuse_workers,
            on_result=on_result,
        )
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["completed_at"] = utc_now()
        write_batch_manifest(manifest_path, manifest)
        return 130

    # With --emit-corpus-xml each standalone corpus owns flat listPerson/listOrg
    # aggregates for its subtree, so the alternative scoped layout is skipped.
    list_person_paths: list[Path] = []
    list_person_error = ""
    list_org_paths: list[Path] = []
    corpus_paths: list[Path] = []
    corpus_error = ""
    list_person_scope = "recursive" if args.emit_corpus_xml else args.list_person_scope

    if args.emit_corpus_xml:
        try:
            if not args.quiet:
                print("Building recursive corpus output(s)", flush=True)
            corpus_paths, list_person_paths, list_org_paths = (
                write_batch_corpus_outputs(
                    jobs,
                    output_root,
                    options,
                )
            )
        except Exception as error:
            corpus_error = f"{type(error).__name__}: {error}"[:500]
            print(
                f"error: corpus generation failed: {corpus_error}",
                file=sys.stderr,
            )
    else:
        try:
            if not args.quiet and not args.no_list_person:
                print(
                    f"Building listPerson output(s): {args.list_person_scope}",
                    flush=True,
                )
            list_person_paths = write_batch_list_person_outputs(
                jobs,
                output_root,
                options,
            )
        except Exception as error:
            list_person_error = f"{type(error).__name__}: {error}"[:500]
            print(
                f"error: listPerson generation failed: {list_person_error}",
                file=sys.stderr,
            )

    counts = batch_counts(results)
    acquisition_failed = any(result.status != "ok" for result in sistory_results)
    final_status = (
        "failed"
        if counts["failed"] or list_person_error or corpus_error
        else "incomplete" if acquisition_failed else "complete"
    )
    warning_count = sum(item.warning_count for item in results)
    manifest.update(
        status=final_status,
        completed_at=utc_now(),
        counts=counts,
        warning_count=warning_count,
        list_person={
            "enabled": not args.no_list_person,
            "scope": list_person_scope,
            "outputs": [str(path) for path in list_person_paths],
            **({"error": list_person_error} if list_person_error else {}),
        },
        list_org={
            "enabled": args.emit_corpus_xml and not args.no_list_person,
            "outputs": [str(path) for path in list_org_paths],
        },
        corpus={
            "enabled": args.emit_corpus_xml,
            "code": args.corpus_code,
            "outputs": [str(path) for path in corpus_paths],
            **({"error": corpus_error} if corpus_error else {}),
        },
        # Restore deterministic discovery order in the final manifest.
        items=[item.as_dict() for item in results],
    )
    write_batch_manifest(manifest_path, manifest)
    if not args.quiet:
        summary = ", ".join(f"{name}={count}" for name, count in counts.items())
        if warning_count:
            summary += f", warnings={warning_count}"
        print(
            f"Finished: {summary}",
            flush=True,
        )
        if list_person_paths:
            print(
                f"listPerson: scope={list_person_scope}, "
                f"files={len(list_person_paths)}",
                flush=True,
            )
        if list_org_paths:
            print(f"listOrg: files={len(list_org_paths)}", flush=True)
        if corpus_paths:
            print(f"corpus: files={len(corpus_paths)}", flush=True)
        print(f"Manifest: {manifest_path}", flush=True)
    return (
        1
        if counts["failed"] or acquisition_failed or list_person_error or corpus_error
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
