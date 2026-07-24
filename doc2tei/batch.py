# i am NOT going to be blamed for a stupid oversight that makes the whole thing not work
# here's a 600 line batch parser, fuck you

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache, partial
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
from type_decs import BatchStatus, ListPersonScope

from .helpers import (
    TEI_NAMESPACE,
    XINCLUDE_NAMESPACE,
    build_list_person,
    build_tei_corpus,
    merge_speaker_mappings,
)
from .parser import (
    LoadedConfig,
    ParseDiagnostics,
    ParseResult,
    load_config,
    parse_document,
)
from .tei_header import Measure, SourceBibl, TEIHeader

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx"})
BATCH_MANIFEST_NAME = "batch-manifest.json"
BUNDLE_STATUS_NAME = "status.json"
MAX_BUNDLE_PATH_LENGTH = 240
MAX_BUNDLE_COMPONENT_LENGTH = 100
# ``_atomic_path`` writes each output to a sibling temp file first. That temp
# name is longer than the final one, so every path budget reserves this many
# characters to keep the *transient* path within MAX_BUNDLE_PATH_LENGTH too --
# otherwise a bundle that just fits still fails when its temp file is created.
_ATOMIC_TEMP_RESERVE = 16
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
    # The nested output folder shared by every document in a source directory;
    # this is where the folder-scoped listPerson.xml is written.
    group: str
    # Unique, path-safe leaf identifying this document within its group. The
    # TEI transcription is written to ``<group>/documents/<title>.xml`` and the
    # JSON sidecars to ``<group>/metadata/<title>/``.
    title: str


def document_path(job: BatchJob) -> Path:
    """TEI transcription output for a job."""

    return Path(job.group) / "documents" / f"{job.title}.xml"


def metadata_dir(job: BatchJob) -> Path:
    """Folder holding a job's data/diagnostics/status JSON sidecars."""

    return Path(job.group) / "metadata" / job.title


def document_list_person_path(job: BatchJob) -> Path:
    """Per-document listPerson output (only written for ``document`` scope)."""

    return Path(job.group) / "documents" / f"{job.title}.listPerson.xml"


@dataclass(frozen=True)
class BatchOptions:
    config: str
    pretty: bool = False
    xml_declaration: bool = False
    write_list_person: bool = True
    list_person_scope: ListPersonScope = "document"
    include_wikidata: bool = False
    wikidata_timeout: float = 20.0
    page_workers: int | None = None
    overwrite: bool = False
    emit_corpus: bool = False
    corpus_language: str = "sl"


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


def _safe_title(name: str, group_dir: Path, source: Path, used: set[str]) -> str:
    """Pick a Windows-path-safe, per-group-unique document title.

    Every sidecar lives under ``<group>/metadata/<title>/`` and the longest of
    those (``diagnostics.json``) governs the length budget. When the natural
    name would overflow ``MAX_BUNDLE_PATH_LENGTH`` the title is truncated and a
    source-derived hash keeps it unique -- the whole bundle stays nested inside
    its group instead of being relocated to a flat sibling folder.
    """

    base = _safe_bundle_component(name or "document")
    # The "x" placeholder stands in for the (not yet known) title; removing its
    # single character leaves the fixed overhead of the longest child path. The
    # temp reserve keeps the transient ``.diagnostics.json.<token>.tmp`` write
    # -- not just the final file -- within the limit.
    overhead = (
        len(str(group_dir / "metadata" / "x" / "diagnostics.json"))
        - 1
        + _ATOMIC_TEMP_RESERVE
    )
    available = max(8, MAX_BUNDLE_PATH_LENGTH - overhead)
    # Reserve a little room for a "-2"/"-3" collision suffix appended below.
    budget = max(8, available - 4)
    if len(base) > budget:
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
        prefix = base[: max(1, budget - len(digest) - 1)].rstrip(" .-")
        base = f"{prefix}-{digest}"
    candidate = base
    number = 2
    while candidate.casefold() in used:
        candidate = f"{base}-{number}"
        number += 1
    used.add(candidate.casefold())
    return candidate


def _safe_relative_group(relative: Path, output: Path) -> Path:
    """Keep source-folder grouping even when document bundles need flattening."""

    if not relative.parts:
        return Path()
    safe = Path(*(_safe_bundle_component(part) for part in relative.parts))
    fit = MAX_BUNDLE_PATH_LENGTH - _ATOMIC_TEMP_RESERVE
    if len(str(output / safe / "listPerson.xml")) <= fit:
        return safe
    digest = hashlib.sha1(relative.as_posix().encode("utf-8")).hexdigest()[:12]
    name = _safe_bundle_component(relative.name or "folder")
    maximum = MAX_BUNDLE_COMPONENT_LENGTH - len(digest) - 1
    return Path("_groups") / f"{name[:maximum].rstrip(' .-')}-{digest}"


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
        output_is_nested = output != specification and _is_within(output, specification)
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
    used_groups: set[str] = set()
    group_paths: dict[str, Path] = {}
    used_titles: dict[str, set[str]] = {}
    for source, relative in sorted(
        candidates,
        key=lambda item: (item[1].as_posix().casefold(), str(item[0]).casefold()),
    ):
        source_key = os.path.normcase(str(source))
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        group_relative = relative.parent
        # The display path can collide when two separately supplied source
        # directories have the same basename. Physical parent identity keeps
        # those conferences distinct while still assigning readable paths.
        group_key = os.path.normcase(str(source.parent))
        unique_group = group_paths.get(group_key)
        if unique_group is None:
            safe_group = _safe_relative_group(group_relative, output)
            group_suffix = hashlib.sha1(
                group_relative.as_posix().encode("utf-8")
            ).hexdigest()[:10]
            unique_group = _unique_bundle_path(
                safe_group,
                used_groups,
                group_suffix,
            )
            group_paths[group_key] = unique_group
        group_dir = output / unique_group
        titles = used_titles.setdefault(unique_group.as_posix().casefold(), set())
        title = _safe_title(relative.name or "document", group_dir, source, titles)
        jobs.append(BatchJob(str(source), str(group_dir), title))
    return jobs, warnings


def automatic_document_workers(document_count: int) -> int:
    """Choose conservative document concurrency for mixed-size collections."""

    if document_count <= 0:
        return 1
    available = max(1, (os.cpu_count() or 1) - 1)
    return min(document_count, available, 4)


def _atomic_path(path: Path) -> Path:
    # A short random token keeps the temp name (and thus the total path) close
    # to the final one; see _ATOMIC_TEMP_RESERVE. os.urandom is enough to avoid
    # clashes between concurrent writers and stale temps from a crashed run.
    return path.with_name(f".{path.name}.{os.urandom(4).hex()}.tmp")


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


def _required_outputs(job: BatchJob) -> list[Path]:
    # listPerson scope is a cheap post-processing choice over data.json. It is
    # deliberately not part of completion so changing scopes never reparses a
    # PDF; write_batch_list_person_outputs repairs the selected sidecars.
    meta = metadata_dir(job)
    return [
        document_path(job),
        meta / "diagnostics.json",
        meta / "data.json",
    ]


def _completed_status(
    job: BatchJob,
    fingerprint: dict[str, object],
    options: BatchOptions,
) -> dict[str, object] | None:
    if options.overwrite:
        return None
    status_path = metadata_dir(job) / BUNDLE_STATUS_NAME
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
    complete = all(path.is_file() for path in _required_outputs(job))
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
    job: BatchJob,
    options: BatchOptions,
) -> None:
    meta = metadata_dir(job)
    meta.mkdir(parents=True, exist_ok=True)
    document = document_path(job)
    document.parent.mkdir(parents=True, exist_ok=True)
    _atomic_result_write(
        document,
        lambda path: result.write_xml(
            path,
            xml_declaration=options.xml_declaration,
            pretty=options.pretty,
        ),
    )
    _atomic_result_write(meta / "diagnostics.json", result.write_diagnostics)
    _atomic_result_write(meta / "data.json", result.write_data)
    list_person = document_list_person_path(job)
    if options.write_list_person and options.list_person_scope == "document":
        _atomic_result_write(
            list_person,
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
        list_person.unlink(missing_ok=True)


def _bundle_speaker_mapping(job: BatchJob) -> Mapping[str, str]:
    try:
        data = json.loads((metadata_dir(job) / "data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    mapping = data.get("speakers") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {
        reference: label
        for reference, label in mapping.items()
        if isinstance(reference, str) and isinstance(label, str)
    }


def _write_list_person_mapping(
    destination: Path,
    mapping: Mapping[str, str],
    options: BatchOptions,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    def writer(path: Path) -> None:
        root = build_list_person(
            mapping,
            include_wikidata=options.include_wikidata,
            wikidata_workers=4,
            wikidata_timeout=options.wikidata_timeout,
        )
        if options.pretty:
            ET.indent(root, space="  ")
        content = ET.tostring(
            root,
            encoding="utf-8",
            xml_declaration=options.xml_declaration,
        )
        path.write_bytes(content + (b"\n" if options.pretty else b""))

    _atomic_result_write(destination, writer)


def write_batch_list_person_outputs(
    jobs: Sequence[BatchJob],
    output_root: str | Path,
    options: BatchOptions,
) -> list[Path]:
    """Write the selected listPerson layout and remove stale alternative scopes."""

    root = Path(output_root).expanduser().resolve()
    document_paths = {document_list_person_path(job): job for job in jobs}
    folder_paths = {Path(job.group) / "listPerson.xml" for job in jobs}
    grouped: dict[Path, list[BatchJob]] = {}
    if options.write_list_person:
        if options.list_person_scope == "document":
            grouped = {
                destination: [job] for destination, job in document_paths.items()
            }
        elif options.list_person_scope == "folder":
            for job in jobs:
                group = Path(job.group)
                grouped.setdefault(group / "listPerson.xml", []).append(job)
        elif options.list_person_scope == "corpus":
            grouped[root / "listPerson.xml"] = list(jobs)
        else:
            raise ValueError(
                f"unsupported listPerson scope: {options.list_person_scope}"
            )

    for destination, group_jobs in grouped.items():
        # Document-scoped files were already produced beside each parse. This
        # fallback only repairs a missing sidecar, avoiding duplicate Wikidata
        # requests during normal runs.
        if (
            options.list_person_scope == "document"
            and destination.is_file()
            and destination not in folder_paths
        ):
            continue
        mapping = merge_speaker_mappings(
            _bundle_speaker_mapping(job) for job in group_jobs
        )
        _write_list_person_mapping(destination, mapping, options)

    desired = {path.resolve() for path in grouped}
    candidates = {root / "listPerson.xml"}
    candidates.update(document_paths)
    candidates.update(folder_paths)
    for candidate in candidates:
        if candidate.resolve() not in desired:
            candidate.unlink(missing_ok=True)
    return sorted(desired, key=lambda path: str(path).casefold())


def _corpus_path(directory: Path) -> Path:
    """Where a folder's ``teiCorpus`` file is written, named after the folder."""

    name = _safe_bundle_component(directory.name or "corpus")
    destination = directory / f"{name}.xml"
    fit = MAX_BUNDLE_PATH_LENGTH - _ATOMIC_TEMP_RESERVE
    if len(str(destination)) <= fit:
        return destination
    digest = hashlib.sha1(str(directory).encode("utf-8")).hexdigest()[:10]
    available = fit - len(str(directory / ".xml")) - len(digest) - 1
    name = f"{name[: max(1, available)].rstrip(' .-')}-{digest}"
    return directory / f"{name}.xml"


def _corpus_id(directory: Path) -> str:
    return engine.sanitize_xml_id(directory.name or "corpus", prefix="corpus")


def _document_counts(document_file: Path) -> dict[str, int]:
    """Best-effort speech/word counts read from a written document's extent."""

    try:
        root = ET.parse(document_file).getroot()
    except (OSError, ET.ParseError):
        return {}
    namespace = {"tei": "http://www.tei-c.org/ns/1.0"}
    counts: dict[str, int] = {}
    for measure in root.findall(
        "tei:teiHeader/tei:fileDesc/tei:extent/tei:measure", namespace
    ):
        unit = measure.get("unit")
        quantity = measure.get("quantity")
        if unit and quantity and quantity.isdigit():
            counts[unit] = counts.get(unit, 0) + int(quantity)
    return counts


@dataclass
class _CorpusNode:
    """One folder in the recursive corpus tree."""

    directory: Path
    documents: list[BatchJob] = field(default_factory=list)
    children: list["_CorpusNode"] = field(default_factory=list)


def _build_corpus_tree(
    jobs: Sequence[BatchJob], output_root: str | Path
) -> _CorpusNode:
    """Assemble the folder tree of corpora rooted at ``output_root``.

    Every folder that holds documents -- directly or somewhere below it --
    becomes a corpus node, so a folder of sub-folders is itself a corpus whose
    members are the sub-folder corpora. A folder with both loose documents and
    sub-folders carries both.
    """

    root = Path(output_root).expanduser().resolve()
    direct: dict[Path, list[BatchJob]] = {}
    for job in jobs:
        direct.setdefault(Path(job.group), []).append(job)

    directories: set[Path] = {root}
    for group in direct:
        directory = group
        directories.add(directory)
        while directory != root and root in directory.parents:
            directory = directory.parent
            directories.add(directory)

    nodes = {
        directory: _CorpusNode(
            directory,
            sorted(direct.get(directory, []), key=lambda job: job.title.casefold()),
        )
        for directory in directories
    }
    for directory, node in nodes.items():
        parent = nodes.get(directory.parent)
        if parent is not None and directory != root:
            parent.children.append(node)
    for node in nodes.values():
        node.children.sort(key=lambda child: child.directory.name.casefold())
    return nodes[root]


def _write_tei_xml(path: Path, *, root: ET.Element, options: BatchOptions) -> None:
    if options.pretty:
        ET.indent(root, space="  ")
    content = ET.tostring(
        root, encoding="utf-8", xml_declaration=options.xml_declaration
    )
    path.write_bytes(content + (b"\n" if options.pretty else b""))


def _corpus_header(
    node: _CorpusNode, options: BatchOptions, subtree: Mapping[str, int]
) -> ET.Element:
    """A skeleton corpus ``teiHeader`` titled after the folder, with aggregates.

    The parser fills each document's own header; here we only sum the already
    computed speech/word counts over the whole subtree and title the corpus
    after its folder, so the result is a valid, reviewable header ready to be
    enriched by hand.
    """

    texts = subtree.get("texts", 0)
    measures = [
        Measure(unit="texts", quantity=str(texts), texts={"": f"{texts} texts"})
    ]
    for unit in ("speeches", "words"):
        if subtree.get(unit):
            measures.append(
                Measure(
                    unit=unit,
                    quantity=str(subtree[unit]),
                    texts={"": f"{subtree[unit]} {unit}"},
                )
            )
    language = options.corpus_language
    title = node.directory.name or "Corpus"
    return TEIHeader(
        tei_id=_corpus_id(node.directory),
        language=language,
        main_titles={language: title},
        measures=measures,
    ).build()


def _build_corpus_element(
    node: _CorpusNode, options: BatchOptions, subtree: Mapping[str, int]
) -> ET.Element:
    document_hrefs = [
        document_path(job).relative_to(node.directory).as_posix()
        for job in node.documents
    ]
    child_hrefs = [
        _corpus_path(child.directory).relative_to(node.directory).as_posix()
        for child in node.children
    ]
    list_person_hrefs = ["listPerson.xml"] if options.write_list_person else []
    return build_tei_corpus(
        document_hrefs + child_hrefs,
        header=_corpus_header(node, options, subtree),
        list_person_hrefs=list_person_hrefs,
        corpus_id=_corpus_id(node.directory),
        language=options.corpus_language,
    )


def _write_corpus_list_person(
    destination: Path, node: _CorpusNode, options: BatchOptions
) -> None:
    """A recursive ``listPerson``: this folder's speakers plus child lists.

    Speakers merged from documents held directly in this folder are inlined;
    each child corpus contributes its own ``listPerson.xml`` by XInclude, so the
    whole tree resolves to one nested speaker list.
    """

    direct = merge_speaker_mappings(
        _bundle_speaker_mapping(job) for job in node.documents
    )

    def writer(path: Path) -> None:
        if direct or not node.children:
            root = build_list_person(
                direct,
                include_wikidata=options.include_wikidata,
                wikidata_workers=4,
                wikidata_timeout=options.wikidata_timeout,
            )
        else:
            root = ET.Element("listPerson", {"xmlns": TEI_NAMESPACE})
        if node.children:
            root.set("xmlns:xi", XINCLUDE_NAMESPACE)
            for child in node.children:
                href = (
                    (child.directory / "listPerson.xml")
                    .relative_to(node.directory)
                    .as_posix()
                )
                ET.SubElement(root, "xi:include", {"href": href})
        _write_tei_xml(path, root=root, options=options)

    _atomic_result_write(destination, writer)


def _emit_corpus_node(
    node: _CorpusNode,
    options: BatchOptions,
    counts: Mapping[Path, Mapping[str, int]],
    corpus_files: list[Path],
    list_person_files: list[Path],
) -> Counter[str]:
    """Emit ``listPerson`` and ``teiCorpus`` for ``node`` and its descendants.

    Children are emitted first so the returned subtree totals (texts, speeches,
    words) are available for this node's aggregated ``<extent>``.
    """

    subtree: Counter[str] = Counter()
    subtree["texts"] += len(node.documents)
    for job in node.documents:
        subtree.update(counts.get(document_path(job), {}))
    for child in node.children:
        subtree.update(
            _emit_corpus_node(child, options, counts, corpus_files, list_person_files)
        )

    if options.write_list_person:
        list_person = node.directory / "listPerson.xml"
        list_person.parent.mkdir(parents=True, exist_ok=True)
        _write_corpus_list_person(list_person, node, options)
        list_person_files.append(list_person)

    corpus = _build_corpus_element(node, options, subtree)
    destination = _corpus_path(node.directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_result_write(
        destination, partial(_write_tei_xml, root=corpus, options=options)
    )
    corpus_files.append(destination)
    return subtree


def write_batch_corpus_outputs(
    jobs: Sequence[BatchJob],
    output_root: str | Path,
    options: BatchOptions,
) -> tuple[list[Path], list[Path]]:
    """Emit a recursive ``teiCorpus`` tree mirroring the output folders.

    Every folder holding documents (directly or in a sub-folder) gets a
    ``teiCorpus`` XML named after it that XIncludes its own documents and each
    child corpus, plus a recursive ``listPerson.xml`` (this folder's merged
    speakers followed by an XInclude of each child list). Nothing is produced
    unless ``options.emit_corpus`` is set. Returns
    ``(corpus_files, list_person_files)``.
    """

    if not options.emit_corpus or not jobs:
        return [], []
    root_node = _build_corpus_tree(jobs, output_root)
    counts = {document_path(job): _document_counts(document_path(job)) for job in jobs}
    corpus_files: list[Path] = []
    list_person_files: list[Path] = []
    _emit_corpus_node(root_node, options, counts, corpus_files, list_person_files)

    def by_path(path: Path) -> str:
        return str(path).casefold()

    return sorted(corpus_files, key=by_path), sorted(list_person_files, key=by_path)


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
    meta = metadata_dir(job)
    meta.mkdir(parents=True, exist_ok=True)
    _atomic_json(meta / BUNDLE_STATUS_NAME, status)


def process_batch_job(job: BatchJob, options: BatchOptions) -> BatchItemResult:
    """Convert one job; always attempt a reviewable bundle on parser failure."""

    started = time.perf_counter()
    output = document_path(job)
    meta = metadata_dir(job)
    source = Path(job.source)
    config = Path(options.config)
    meta.mkdir(parents=True, exist_ok=True)
    fingerprint = _job_fingerprint(job, options)
    previous = _completed_status(job, fingerprint, options)
    if previous is not None:
        return BatchItemResult(
            source=str(source),
            output=str(output),
            status="skipped",
            elapsed_seconds=time.perf_counter() - started,
            chunk_count=int(previous.get("chunk_count", 0)),
            recovery_count=int(previous.get("recovery_count", 0)),
            message="unchanged completed bundle",
            warning_count=int(previous.get("warning_count", 0)),
        )

    debug_temporary = _atomic_path(meta / "debug.log")
    final_result: BatchItemResult
    try:
        with debug_temporary.open("w", encoding="utf-8") as debug_stream:
            try:
                loaded = load_config(config)
                _configure_page_workers(loaded, options.page_workers)
                with redirect_stdout(debug_stream), redirect_stderr(debug_stream):
                    parsed = parse_document(source, config=loaded)
                    _write_result_bundle(parsed, job, options)
                recoveries = _result_recovery_count(parsed)
                warnings = _result_warning_count(parsed)
                status: BatchStatus = "recovered" if recoveries else "ok"
                final_result = BatchItemResult(
                    source=str(source),
                    output=str(output),
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
                _write_result_bundle(fallback, job, options)
                final_result = BatchItemResult(
                    source=str(source),
                    output=str(output),
                    status="recovered",
                    elapsed_seconds=time.perf_counter() - started,
                    recovery_count=_result_recovery_count(fallback),
                    message=f"{type(error).__name__}: {error}"[:500],
                )

        debug_path = meta / "debug.log"
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
            output=str(output),
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
    output = document_path(job)
    source = Path(job.source)
    config = Path(options.config)
    try:
        metadata_dir(job).mkdir(parents=True, exist_ok=True)
        fallback = _failure_parse_result(
            source,
            config,
            f"worker process failed: {type(error).__name__}: {error}",
        )
        _write_result_bundle(fallback, job, options)
        result = BatchItemResult(
            source=str(source),
            output=str(output),
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
            output=str(output),
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
