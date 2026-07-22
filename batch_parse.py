"""Command-line entry point for parallel doc2tei batch conversion."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from doc2tei.batch import (
    BATCH_MANIFEST_NAME,
    BatchItemResult,
    BatchOptions,
    automatic_document_workers,
    batch_counts,
    discover_batch_jobs,
    run_batch,
    utc_now,
    write_batch_manifest,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert PDF/DOCX files into independent TEI output bundles, "
            "continuing after per-document failures"
        )
    )
    parser.add_argument("inputs", nargs="+", help="input files or directories")
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="root directory for per-document output bundles",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help="configuration file (defaults to examples/general-config/config.py)",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.include_wikidata and args.no_list_person:
        parser.error("--include-wikidata cannot be used with --no-list-person")

    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        parser.error(f"config file does not exist: {config}")
    output_root = Path(args.output_dir).expanduser().resolve()
    jobs, warnings = discover_batch_jobs(
        args.inputs,
        output_root,
        recursive=args.recursive,
        extensions=args.extension,
    )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not jobs:
        parser.error("no supported documents were found")

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
        include_wikidata=args.include_wikidata,
        wikidata_timeout=args.wikidata_timeout,
        page_workers=page_workers,
        overwrite=args.overwrite,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / BATCH_MANIFEST_NAME
    started_at = utc_now()
    manifest: dict[str, object] = {
        "status": "running",
        "started_at": started_at,
        "config": str(config),
        "output": str(output_root),
        "document_workers": document_workers,
        "page_workers": "config" if page_workers is None else page_workers,
        "document_count": len(jobs),
        "discovery_warnings": warnings,
        "items": [],
    }
    write_batch_manifest(manifest_path, manifest)

    observed: list[BatchItemResult] = []
    checkpoint_interval = max(1, min(50, len(jobs) // 100 or 1))

    def on_result(result: BatchItemResult, current: int, total: int) -> None:
        observed.append(result)
        if current == total or current % checkpoint_interval == 0:
            manifest["items"] = [item.as_dict() for item in observed]
            manifest["counts"] = batch_counts(observed)
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

    counts = batch_counts(results)
    manifest.update(
        status="failed" if counts["failed"] else "complete",
        completed_at=utc_now(),
        counts=counts,
        # Restore deterministic discovery order in the final manifest.
        items=[item.as_dict() for item in results],
    )
    write_batch_manifest(manifest_path, manifest)
    if not args.quiet:
        print(
            "Finished: "
            + ", ".join(f"{name}={count}" for name, count in counts.items()),
            flush=True,
        )
        print(f"Manifest: {manifest_path}", flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
