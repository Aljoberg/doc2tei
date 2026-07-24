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
import re
import sys
import time
import traceback
from typing import cast
import unicodedata
import xml.etree.ElementTree as ET

import engine
from type_decs import BatchStatus, ListPersonScope

from .helpers import (
    build_list_org,
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
from .tei_header import Measure, Meeting, SourceBibl, TEIHeader

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx"})
BATCH_MANIFEST_NAME = "batch-manifest.json"
BUNDLE_STATUS_NAME = "status.json"
MAX_BUNDLE_PATH_LENGTH = 240
MAX_BUNDLE_COMPONENT_LENGTH = 100
DESIRED_COMPONENT_TITLE_BUDGET = 64
DEFAULT_CORPUS_CODE = "SI"
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
_CORPUS_CODE_RE = re.compile(r"[A-Za-z]{2}(?:-[A-Za-z0-9]+)*\Z")
_LEADING_INDEX_RE = re.compile(r"^\s*0*(\d+)\s*(?:[.)]\s*|-\s+)")
_INDEXED_FOLDER_RE = re.compile(r"^\s*0*(\d+)\s*(?:[.)]\s*|-\s+)(.+?)\s*$")
_YEAR_RANGE_RE = re.compile(
    r"\s*\(\s*(?:18|19|20)\d{2}\s*[-–—]\s*(?:18|19|20)\d{2}\s*\)\s*$"
)
_MONTH_NUMBERS = {
    "januar": 1,
    "januarja": 1,
    "januara": 1,
    "sijecnja": 1,
    "january": 1,
    "februar": 2,
    "februarja": 2,
    "februara": 2,
    "veljace": 2,
    "february": 2,
    "marec": 3,
    "marca": 3,
    "marta": 3,
    "ozujka": 3,
    "march": 3,
    "april": 4,
    "aprila": 4,
    "travnja": 4,
    "maj": 5,
    "maja": 5,
    "svibnja": 5,
    "may": 5,
    "junij": 6,
    "junija": 6,
    "juna": 6,
    "lipnja": 6,
    "june": 6,
    "julij": 7,
    "julija": 7,
    "jula": 7,
    "srpnja": 7,
    "july": 7,
    "avgust": 8,
    "avgusta": 8,
    "kolovoza": 8,
    "august": 8,
    "september": 9,
    "septembra": 9,
    "rujna": 9,
    "oktober": 10,
    "oktobra": 10,
    "listopada": 10,
    "october": 10,
    "november": 11,
    "novembra": 11,
    "studenoga": 11,
    "december": 12,
    "decembra": 12,
    "prosinca": 12,
}
_TEXTUAL_DATE_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s*\.?\s*"
    r"(?:[-–—]\s*\d{1,2}\s*\.\s*)?"
    r"(?P<month>[^\W\d_]+)\.?\s*"
    r"(?P<year>(?:18|19|20)\d{2})?",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(
    r"(?<!\d)((?:18|19|20)\d{2})[-./](0?[1-9]|1[0-2])[-./]"
    r"(0?[1-9]|[12]\d|3[01])(?!\d)"
)
_DMY_DATE_RE = re.compile(
    r"(?<!\d)(0?[1-9]|[12]\d|3[01])[.](0?[1-9]|1[0-2])[.]" r"((?:18|19|20)\d{2})(?!\d)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_corpus_code(value: str) -> str:
    """Return a ParlaMint-compatible upper-case country/region code."""

    code = value.strip().upper()
    if not _CORPUS_CODE_RE.fullmatch(code):
        raise ValueError("corpus code must be an ISO-style code such as SI or ES-CT")
    return code


def _corpus_prefix(code: str) -> str:
    return f"ParlaMint-{normalize_corpus_code(code)}"


def _ascii_slug(value: str, *, fallback: str = "corpus") -> str:
    """Convert free text to the ASCII letters/numbers/hyphens file subset."""

    replacements = str.maketrans(
        {
            "đ": "d",
            "Đ": "D",
            "ð": "d",
            "Ð": "D",
            "ł": "l",
            "Ł": "L",
        }
    )
    decomposed = unicodedata.normalize("NFKD", value.translate(replacements))
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or fallback


def _serialized_folder_component(value: str) -> str:
    """Serialize ``1. sklic (1947-1950)`` as ``sklic-01``.

    Other leading catalogue numbers are moved to the end as well, so mirrored
    corpus directories never begin with a source-system ordering index.
    """

    indexed = _INDEXED_FOLDER_RE.match(value)
    if indexed:
        number = int(indexed.group(1))
        label = _YEAR_RANGE_RE.sub("", indexed.group(2)).strip()
        return f"{_ascii_slug(label, fallback='corpus')}-{number:02d}"
    return _ascii_slug(value)


def _month_key(value: str) -> str:
    return _ascii_slug(value, fallback="").replace("-", "")


def _extract_iso_date(value: str) -> str | None:
    """Best-effort first transcript date from a source title or header text."""

    candidates: list[tuple[int, str]] = []
    for iso in _ISO_DATE_RE.finditer(value):
        try:
            parsed = (
                datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
                .date()
                .isoformat()
            )
            candidates.append((iso.start(), parsed))
        except ValueError:
            continue

    for numeric in _DMY_DATE_RE.finditer(value):
        try:
            parsed = (
                datetime(
                    int(numeric.group(3)),
                    int(numeric.group(2)),
                    int(numeric.group(1)),
                )
                .date()
                .isoformat()
            )
            candidates.append((numeric.start(), parsed))
        except ValueError:
            continue

    for match in _TEXTUAL_DATE_RE.finditer(value):
        month = _MONTH_NUMBERS.get(_month_key(match.group("month")))
        if month is None:
            continue
        year_text = match.group("year")
        if not year_text:
            # Ranges often repeat the month but put the year only after the
            # second date: "14. februarja - 16. februarja 1949".
            nearby = re.search(
                r"(?:18|19|20)\d{2}", value[match.end() : match.end() + 64]
            )
            year_text = nearby.group(0) if nearby else None
        if not year_text:
            continue
        try:
            parsed = (
                datetime(int(year_text), month, int(match.group("day")))
                .date()
                .isoformat()
            )
            candidates.append((match.start(), parsed))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _short_source_slug(value: str, maximum: int = 28) -> str:
    cleaned = _LEADING_INDEX_RE.sub("", value, count=1)
    slug = _ascii_slug(cleaned, fallback="document")
    if len(slug) <= maximum:
        return slug
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug[: maximum - len(digest) - 1].rstrip('-')}-{digest}"


def _short_folder_slug(value: str, maximum: int = 28) -> str:
    slug = _serialized_folder_component(value)
    if len(slug) <= maximum:
        return slug
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug[: maximum - len(digest) - 1].rstrip('-')}-{digest}"


def _component_stem(source: Path, group_labels: Sequence[str], code: str) -> str:
    """Build a ParlaMint-style component stem without changing header titles."""

    date = _extract_iso_date(source.stem) or "undated"
    suffixes: list[str] = []
    if group_labels:
        suffixes.append(_short_folder_slug(group_labels[-1]))
    source_index = _LEADING_INDEX_RE.match(source.stem)
    if source_index:
        suffixes.append(f"{int(source_index.group(1)):02d}")
    else:
        suffixes.append(_short_source_slug(source.stem))
    suffix = f"-{'-'.join(suffixes)}" if suffixes else ""
    return f"{_corpus_prefix(code)}_{date}{suffix}"


@dataclass(frozen=True)
class BatchJob:
    source: str
    # The nested output folder shared by every document in a source directory;
    # this is where the document component itself is written.
    group: str
    # Unique ParlaMint-style leaf identifying this document within its group.
    # The original source title remains unchanged in the document teiHeader.
    title: str
    # Original source-folder labels, retained after their filesystem-safe
    # serialisation so corpus and component headers can describe their term.
    group_labels: tuple[str, ...] = ()
    # Mirrored audit folder outside the corpus tree. ``None`` retains the
    # historical in-group location for manually constructed BatchJob objects.
    metadata_group: str | None = None


def document_path(job: BatchJob) -> Path:
    """TEI transcription output for a job."""

    return Path(job.group) / f"{job.title}.xml"


def metadata_dir(job: BatchJob) -> Path:
    """Folder holding a job's data/diagnostics/status JSON sidecars."""

    parent = (
        Path(job.metadata_group)
        if job.metadata_group is not None
        else Path(job.group) / "metadata"
    )
    return parent / job.title


def default_metadata_root(output_root: str | Path) -> Path:
    """Return the sibling audit root used for a corpus output by default."""

    output = Path(output_root).expanduser().resolve()
    return output.parent / f"{output.name or 'output'}-metadata"


def document_list_person_path(job: BatchJob) -> Path:
    """Per-document listPerson output (only written for ``document`` scope)."""

    return Path(job.group) / f"{job.title}-listPerson.xml"


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
    corpus_code: str = DEFAULT_CORPUS_CODE


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


def _filesystem_path(path: str | Path, *, force: bool = False) -> Path:
    """Use the Win32 extended prefix when a path exceeds legacy MAX_PATH."""

    value = Path(path).expanduser().absolute()
    if os.name != "nt":
        return value
    raw = str(value)
    if raw.startswith("\\\\?\\") or (len(raw) < 248 and not force):
        return value
    if raw.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{raw[2:]}")
    return Path(f"\\\\?\\{raw}")


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


def _safe_title(
    name: str,
    group_dir: Path,
    source: Path,
    used: set[str],
    metadata_group: Path | None = None,
) -> str:
    """Pick a Windows-path-safe, per-group-unique document title.

    Components live directly under ``<group>`` while JSON sidecars live in a
    separate mirrored audit tree when ``metadata_group`` is supplied. The
    longest possible output governs the budget. A source-derived hash keeps a
    truncated title unique.
    """

    base = _safe_bundle_component(name or "document")
    # The "x" placeholder stands in for the (not yet known) title; removing its
    # single character leaves the fixed overhead of the longest child path. The
    # temp reserve keeps the transient ``.diagnostics.json.<token>.tmp`` write
    # -- not just the final file -- within the limit.
    audit_group = metadata_group or group_dir / "metadata"
    overhead = (
        max(
            len(str(group_dir / "x-listPerson.xml")) - 1,
            len(str(group_dir / "x.xml")) - 1,
            len(str(audit_group / "x" / "diagnostics.json")) - 1,
        )
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


def _safe_relative_group(
    relative: Path,
    output: Path,
    metadata_root: Path | None = None,
) -> Path:
    """Serialize corpus folders while preserving their top-level source root."""

    if not relative.parts:
        return Path()
    fit = MAX_BUNDLE_PATH_LENGTH - _ATOMIC_TEMP_RESERVE
    audit_root = metadata_root or default_metadata_root(output)
    title_placeholder = "x" * DESIRED_COMPONENT_TITLE_BUDGET
    first_name = _safe_bundle_component(_serialized_folder_component(relative.parts[0]))
    # Reserve one compact descendant component. This makes the serialized
    # top-level source folder stable whether it contains direct documents,
    # nested folders, or both.
    root_placeholders = (
        output / "x" / ("y" * 8) / f"{title_placeholder}-listPerson.xml",
        output / "x" / ("y" * 8) / f"{title_placeholder}.xml",
        audit_root / "x" / ("y" * 8) / title_placeholder / "diagnostics.json",
    )
    root_available = max(
        8,
        min(fit - len(str(path)) + 1 for path in root_placeholders),
    )
    root_maximum = min(MAX_BUNDLE_COMPONENT_LENGTH, root_available)
    if len(first_name) > root_maximum:
        root_digest = hashlib.sha1(relative.parts[0].encode("utf-8")).hexdigest()[:12]
        if root_maximum <= len(root_digest):
            first_name = root_digest[:root_maximum]
        else:
            prefix = first_name[: root_maximum - len(root_digest) - 1].rstrip(" .-")
            first_name = f"{prefix}-{root_digest}" if prefix else root_digest

    safe = Path(
        first_name,
        *(
            _safe_bundle_component(_serialized_folder_component(part))
            for part in relative.parts[1:]
        ),
    )
    minimum_bundles = (
        output / safe / f"{title_placeholder}-listPerson.xml",
        output / safe / f"{title_placeholder}.xml",
        audit_root / safe / title_placeholder / "diagnostics.json",
    )
    if all(len(str(path)) <= fit for path in minimum_bundles):
        return safe
    if len(relative.parts) == 1:
        return Path(first_name)

    digest = hashlib.sha1(relative.as_posix().encode("utf-8")).hexdigest()[:12]
    name = _safe_bundle_component(
        _serialized_folder_component(relative.name or "folder")
    )
    placeholders = (
        output / first_name / "x" / f"{title_placeholder}-listPerson.xml",
        output / first_name / "x" / f"{title_placeholder}.xml",
        audit_root / first_name / "x" / title_placeholder / "diagnostics.json",
    )
    available = max(
        8,
        min(fit - len(str(path)) + 1 for path in placeholders),
    )
    maximum = min(MAX_BUNDLE_COMPONENT_LENGTH, available)
    if maximum <= len(digest):
        leaf = digest[:maximum]
    else:
        prefix = name[: maximum - len(digest) - 1].rstrip(" .-")
        leaf = f"{prefix}-{digest}" if prefix else digest
    return Path(first_name, leaf)


def discover_batch_jobs(
    inputs: Sequence[str | Path],
    output_root: str | Path,
    *,
    metadata_root: str | Path | None = None,
    recursive: bool = True,
    extensions: Iterable[str] | None = None,
    corpus_code: str = DEFAULT_CORPUS_CODE,
) -> tuple[list[BatchJob], list[str]]:
    """Discover documents and assign collision-free bundles below source roots.

    Every supplied directory is retained as a top-level output folder. This
    makes ``output_root`` a neutral container rather than silently turning it
    into a corpus of its own.
    """

    output = Path(output_root).expanduser().resolve()
    metadata_output = (
        Path(metadata_root).expanduser().resolve()
        if metadata_root is not None
        else default_metadata_root(output)
    )
    code = normalize_corpus_code(corpus_code)
    allowed = normalize_extensions(extensions)
    specifications: list[Path] = []
    for value in inputs:
        ordinary = Path(value).expanduser().absolute()
        specifications.append(
            _filesystem_path(
                ordinary,
                force=ordinary.is_dir(),
            )
        )

    physical_roots: dict[str, Path] = {}
    for specification in specifications:
        root = specification.parent if specification.is_file() else specification
        physical_roots.setdefault(os.path.normcase(str(root)), root)
    root_prefixes: dict[str, Path] = {}
    used_prefixes: set[str] = set()
    for root_key, root in sorted(
        physical_roots.items(),
        key=lambda item: str(item[1]).casefold(),
    ):
        base_name = root.name or "input"
        prefix = Path(base_name)
        serialized_key = _serialized_folder_component(base_name).casefold()
        if serialized_key in used_prefixes:
            digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
            prefix = Path(f"{base_name}-{digest}")
            serialized_key = _serialized_folder_component(prefix.name).casefold()
        number = 2
        candidate = prefix
        while serialized_key in used_prefixes:
            candidate = Path(f"{prefix.name}-{number}")
            serialized_key = _serialized_folder_component(candidate.name).casefold()
            number += 1
        used_prefixes.add(serialized_key)
        root_prefixes[root_key] = candidate

    candidates: list[tuple[Path, Path, tuple[str, ...]]] = []
    warnings: list[str] = []

    for specification in specifications:
        if not specification.exists():
            warnings.append(f"input does not exist: {specification}")
            continue
        if specification.is_file():
            if specification.suffix.casefold() not in allowed:
                warnings.append(f"unsupported input type: {specification}")
                continue
            parent_key = os.path.normcase(str(specification.parent))
            relative = root_prefixes[parent_key] / specification.stem
            candidates.append(
                (
                    specification,
                    relative,
                    (specification.parent.name or "input",),
                )
            )
            continue
        if not specification.is_dir():
            warnings.append(
                f"input is not a regular file or directory: {specification}"
            )
            continue

        iterator = specification.rglob("*") if recursive else specification.glob("*")
        specification_key = os.path.normcase(str(specification))
        prefix = root_prefixes[specification_key]
        excluded_roots = []
        for excluded in (output, metadata_output):
            filesystem_root = _filesystem_path(excluded, force=True)
            if filesystem_root != specification and _is_within(
                filesystem_root, specification
            ):
                excluded_roots.append(filesystem_root)
        for source in iterator:
            resolved = _filesystem_path(source)
            if (
                not resolved.is_file()
                or source.suffix.casefold() not in allowed
                or any(_is_within(resolved, root) for root in excluded_roots)
            ):
                continue
            source_relative = source.relative_to(specification).with_suffix("")
            relative = prefix / source_relative
            labels = (
                specification.name or "input",
                *source_relative.parent.parts,
            )
            candidates.append((resolved, relative, labels))

    jobs: list[BatchJob] = []
    seen_sources: set[str] = set()
    used_groups: set[str] = set()
    group_paths: dict[str, Path] = {}
    used_titles: dict[str, set[str]] = {}
    for source, relative, labels in sorted(
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
            safe_group = _safe_relative_group(
                group_relative,
                output,
                metadata_output,
            )
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
        metadata_group = metadata_output / unique_group
        titles = used_titles.setdefault(unique_group.as_posix().casefold(), set())
        group_labels = labels
        natural_title = _component_stem(source, group_labels, code)
        title = _safe_title(
            natural_title,
            group_dir,
            source,
            titles,
            metadata_group,
        )
        jobs.append(
            BatchJob(
                source=str(source),
                group=str(group_dir),
                title=title,
                group_labels=group_labels,
                metadata_group=str(metadata_group),
            )
        )
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
            "corpus_language": options.corpus_language,
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


def _status_count(status: Mapping[str, object], key: str) -> int:
    value = status.get(key)
    return value if isinstance(value, int) else 0


def _enrich_document_header(
    root: ET.Element,
    job: BatchJob,
    options: BatchOptions,
) -> None:
    """Add safely inferred corpus/date metadata without replacing source titles."""

    root.set("xml:id", job.title)
    if options.corpus_language and not root.get("xml:lang"):
        root.set("xml:lang", options.corpus_language)
    header = root.find("teiHeader")
    if header is None:
        return

    title_stmt = header.find("fileDesc/titleStmt")
    labels = [label.strip() for label in job.group_labels if label.strip()]
    if title_stmt is not None and labels:
        # The config deliberately preserves the complete source filename as the
        # main title. Reuse its normally empty subordinate title for corpus
        # context instead of changing that established title.
        subtitle = next(
            (
                title
                for title in title_stmt.findall("title")
                if title.get("type") == "sub" and not (title.text or "").strip()
            ),
            None,
        )
        if subtitle is not None:
            subtitle.text = " / ".join(labels)

        meeting_label = next(
            (
                label
                for label in reversed(labels)
                if re.search(
                    r"\b(?:sklic|mandat|zasedanje|seja|sednica|session)\b",
                    label,
                    re.IGNORECASE,
                )
            ),
            None,
        )
        existing = {
            (meeting.text or "").strip() for meeting in title_stmt.findall("meeting")
        }
        if meeting_label and meeting_label not in existing:
            attrs: dict[str, str] = {}
            number = re.match(r"\s*0*(\d+)", meeting_label)
            if number:
                attrs["n"] = str(int(number.group(1)))
            if re.search(r"\b(?:sklic|mandat)\b", meeting_label, re.IGNORECASE):
                attrs["ana"] = "#parla.term"
            elif re.search(
                r"\b(?:zasedanje|seja|sednica|session)\b",
                meeting_label,
                re.IGNORECASE,
            ):
                attrs["ana"] = "#parla.meeting"
            meeting = ET.Element("meeting", attrs)
            meeting.text = meeting_label
            insert_at = len(title_stmt)
            for index, child in enumerate(title_stmt):
                if child.tag not in {"title", "meeting"}:
                    insert_at = index
                    break
            title_stmt.insert(insert_at, meeting)

    date = _extract_iso_date(Path(job.source).stem)
    if not date:
        for time_element in root.findall(".//time[@type='date']"):
            date = _extract_iso_date(" ".join(time_element.itertext()))
            if date:
                break
    if not date:
        return
    date_paths = (
        "fileDesc/sourceDesc/bibl/date",
        "profileDesc/settingDesc/setting/date",
    )
    for path in date_paths:
        element = header.find(path)
        if element is None or element.get("when"):
            continue
        element.set("when", date)
        if not (element.text or "").strip():
            element.text = date


def _write_result_bundle(
    result: ParseResult,
    job: BatchJob,
    options: BatchOptions,
) -> None:
    _enrich_document_header(result.root, job, options)
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


def _folder_list_person_path(
    group: Path,
    root: Path,
    options: BatchOptions,
) -> Path:
    return _corpus_artifact_path(group, root, options, "-listPerson")


def write_batch_list_person_outputs(
    jobs: Sequence[BatchJob],
    output_root: str | Path,
    options: BatchOptions,
) -> list[Path]:
    """Write the selected listPerson layout and remove stale alternative scopes."""

    root = Path(output_root).expanduser().resolve()
    document_paths = {document_list_person_path(job): job for job in jobs}
    folder_paths = {
        _folder_list_person_path(Path(job.group), root, options) for job in jobs
    }
    grouped: dict[Path, list[BatchJob]] = {}
    if options.write_list_person:
        if options.list_person_scope == "document":
            grouped = {
                destination: [job] for destination, job in document_paths.items()
            }
        elif options.list_person_scope == "folder":
            for job in jobs:
                group = Path(job.group)
                destination = _folder_list_person_path(group, root, options)
                grouped.setdefault(destination, []).append(job)
        elif options.list_person_scope == "corpus":
            grouped[_corpus_artifact_path(root, root, options, "-listPerson")] = list(
                jobs
            )
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
    candidates = {
        root / "listPerson.xml",
        _corpus_artifact_path(root, root, options, "-listPerson"),
    }
    candidates.update(document_paths)
    candidates.update(folder_paths)
    candidates.update(Path(job.group) / "listPerson.xml" for job in jobs)
    for candidate in candidates:
        if candidate.resolve() not in desired:
            candidate.unlink(missing_ok=True)
    return sorted(desired, key=lambda path: str(path).casefold())


def _corpus_owner(directory: Path, root: Path) -> Path:
    """Directory that owns a corpus's root and metadata files."""

    return root if directory == root else directory.parent


def _corpus_stem(
    directory: Path,
    root: Path,
    options: BatchOptions,
) -> str:
    prefix = _corpus_prefix(options.corpus_code)
    if directory == root:
        return prefix
    return f"{prefix}-{_serialized_folder_component(directory.name)}"


def _corpus_artifact_path(
    directory: Path,
    root: Path,
    options: BatchOptions,
    suffix: str = "",
) -> Path:
    """Path for a corpus root or one of its ParlaMint metadata files."""

    owner = _corpus_owner(directory, root)
    stem = _safe_bundle_component(_corpus_stem(directory, root, options))
    destination = owner / f"{stem}{suffix}.xml"
    fit = MAX_BUNDLE_PATH_LENGTH - _ATOMIC_TEMP_RESERVE
    if len(str(destination)) <= fit:
        return destination
    digest = hashlib.sha1(str(directory).encode("utf-8")).hexdigest()[:10]
    fixed = len(str(owner / f"-{digest}{suffix}.xml"))
    available = max(8, min(MAX_BUNDLE_COMPONENT_LENGTH, fit - fixed))
    shortened = f"{stem[:available].rstrip(' .-')}-{digest}"
    return owner / f"{shortened}{suffix}.xml"


def _corpus_path(
    directory: Path,
    root: Path,
    options: BatchOptions,
) -> Path:
    return _corpus_artifact_path(directory, root, options)


def _corpus_id(
    directory: Path,
    root: Path,
    options: BatchOptions,
) -> str:
    return engine.sanitize_xml_id(
        _corpus_stem(directory, root, options),
        prefix="corpus",
    )


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
    label: str
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
    labels: dict[Path, str] = {root: root.name or "Corpus"}
    for job in jobs:
        group = Path(job.group).resolve()
        direct.setdefault(group, []).append(job)
        try:
            relative_parts = group.relative_to(root).parts
        except ValueError:
            relative_parts = ()
        if len(relative_parts) == len(job.group_labels):
            directory = root
            for part, label in zip(relative_parts, job.group_labels):
                directory /= part
                labels.setdefault(directory, label)
        elif job.group_labels:
            if relative_parts:
                labels.setdefault(root / relative_parts[0], job.group_labels[0])
            labels.setdefault(group, job.group_labels[-1])

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
            labels.get(directory, directory.name or "Corpus"),
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
    node: _CorpusNode,
    root: Path,
    options: BatchOptions,
    subtree: Mapping[str, int],
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
    title = node.label or "Corpus"
    meetings: list[Meeting] = []
    if re.search(
        r"\b(?:sklic|mandat|zasedanje|seja|sednica|session)\b",
        title,
        re.IGNORECASE,
    ):
        number = re.match(r"\s*0*(\d+)", title)
        term = bool(re.search(r"\b(?:sklic|mandat)\b", title, re.IGNORECASE))
        meetings.append(
            Meeting(
                text=title,
                n=str(int(number.group(1))) if number else "",
                ana="#parla.term" if term else "#parla.meeting",
            )
        )
    return TEIHeader(
        tei_id=_corpus_id(node.directory, root, options),
        language=language,
        main_titles={language: title},
        meetings=meetings,
        measures=measures,
    ).build()


def _build_corpus_element(
    node: _CorpusNode,
    root: Path,
    options: BatchOptions,
    subtree: Mapping[str, int],
    documents: Sequence[BatchJob],
) -> ET.Element:
    owner = _corpus_owner(node.directory, root)
    document_hrefs = [
        document_path(job).relative_to(owner).as_posix() for job in documents
    ]
    lists = (
        [
            _corpus_artifact_path(
                node.directory,
                root,
                options,
                "-listPerson",
            )
            .relative_to(owner)
            .as_posix()
        ]
        if options.write_list_person
        else []
    )
    orgs = (
        [
            _corpus_artifact_path(
                node.directory,
                root,
                options,
                "-listOrg",
            )
            .relative_to(owner)
            .as_posix()
        ]
        if options.write_list_person
        else []
    )
    return build_tei_corpus(
        document_hrefs,
        header=_corpus_header(node, root, options, subtree),
        list_person_hrefs=lists,
        list_org_hrefs=orgs,
        corpus_id=_corpus_id(node.directory, root, options),
        language=options.corpus_language,
    )


def _write_particdesc_list(
    destination: Path,
    options: BatchOptions,
    *,
    build_root: Callable[[], ET.Element],
) -> None:
    """Write one flat, deduplicated ``listPerson`` or ``listOrg``."""

    def writer(path: Path) -> None:
        _write_tei_xml(path, root=build_root(), options=options)

    _atomic_result_write(destination, writer)


def _emit_corpus_node(
    node: _CorpusNode,
    root: Path,
    options: BatchOptions,
    counts: Mapping[Path, Mapping[str, int]],
    corpus_files: list[Path],
    list_person_files: list[Path],
    list_org_files: list[Path],
) -> tuple[Counter[str], dict[str, str], list[BatchJob]]:
    """Emit the lists and ``teiCorpus`` for ``node`` and its descendants.

    Children are emitted first so the returned subtree totals (texts, speeches,
    words) are available for this node's aggregated ``<extent>``.
    """

    subtree: Counter[str] = Counter()
    mappings: list[Mapping[str, str]] = [
        _bundle_speaker_mapping(job) for job in node.documents
    ]
    documents = list(node.documents)
    subtree["texts"] += len(node.documents)
    for job in node.documents:
        subtree.update(counts.get(document_path(job), {}))
    for child in node.children:
        child_counts, child_mapping, child_documents = _emit_corpus_node(
            child,
            root,
            options,
            counts,
            corpus_files,
            list_person_files,
            list_org_files,
        )
        subtree.update(child_counts)
        mappings.append(child_mapping)
        documents.extend(child_documents)

    merged = merge_speaker_mappings(mappings)

    if options.write_list_person:
        owner = _corpus_owner(node.directory, root)
        owner.mkdir(parents=True, exist_ok=True)

        def build_person(merged: Mapping[str, str] = merged) -> ET.Element:
            return build_list_person(
                merged,
                include_wikidata=options.include_wikidata,
                wikidata_workers=4,
                wikidata_timeout=options.wikidata_timeout,
            )

        list_person = _corpus_artifact_path(
            node.directory,
            root,
            options,
            "-listPerson",
        )
        _write_particdesc_list(
            list_person,
            options,
            build_root=build_person,
        )
        list_person_files.append(list_person)

        list_org = _corpus_artifact_path(
            node.directory,
            root,
            options,
            "-listOrg",
        )
        _write_particdesc_list(
            list_org,
            options,
            build_root=lambda merged=merged: build_list_org(merged),
        )
        list_org_files.append(list_org)

    corpus = _build_corpus_element(node, root, options, subtree, documents)
    destination = _corpus_path(node.directory, root, options)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_result_write(
        destination, partial(_write_tei_xml, root=corpus, options=options)
    )
    corpus_files.append(destination)
    return subtree, merged, documents


def write_batch_corpus_outputs(
    jobs: Sequence[BatchJob],
    output_root: str | Path,
    options: BatchOptions,
) -> tuple[list[Path], list[Path], list[Path]]:
    """Emit a forest of standalone corpora below a neutral output container.

    Every top-level source folder is an independent root corpus; ``output_root``
    itself is only their container. A subcorpus keeps its document components
    directly inside its directory, while its corpus root and ParlaMint-named
    metadata files are owned by the parent directory. Every corpus directly
    XIncludes all document components in its subtree; corpus roots and metadata
    lists never XInclude child corpus artifacts. Nothing is produced unless
    ``options.emit_corpus`` is set. Returns ``(corpus_files,
    list_person_files, list_org_files)``.
    """

    if not options.emit_corpus or not jobs:
        return [], [], []
    container = _build_corpus_tree(jobs, output_root)
    root = container.directory
    counts = {document_path(job): _document_counts(document_path(job)) for job in jobs}
    corpus_files: list[Path] = []
    list_person_files: list[Path] = []
    list_org_files: list[Path] = []
    for corpus_root in container.children:
        _emit_corpus_node(
            corpus_root,
            root,
            options,
            counts,
            corpus_files,
            list_person_files,
            list_org_files,
        )

    if container.documents:
        # Public discovery preserves input roots, so normal batches never put
        # documents directly in the neutral container. Keep a lossless fallback
        # for manually constructed jobs and third-party library integrations.
        loose_documents = _CorpusNode(
            directory=container.directory,
            label=container.label,
            documents=container.documents,
        )
        _emit_corpus_node(
            loose_documents,
            root,
            options,
            counts,
            corpus_files,
            list_person_files,
            list_org_files,
        )

    # Reconcile exact files owned by the corpus generator. This removes stale
    # generic artifacts and metadata lists disabled with --no-list-person,
    # without deleting source or document files.
    nodes: list[_CorpusNode] = []
    pending = [container]
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(node.children)
    desired = {
        path.resolve() for path in (*corpus_files, *list_person_files, *list_org_files)
    }
    protected = {document_path(job).resolve() for job in jobs}
    candidates: set[Path] = set()
    for node in nodes:
        candidates.update(
            {
                node.directory / "listPerson.xml",
                node.directory / "listOrg.xml",
                node.directory
                / f"{_safe_bundle_component(node.directory.name or 'corpus')}.xml",
                _corpus_artifact_path(
                    node.directory,
                    root,
                    options,
                ),
                _corpus_artifact_path(
                    node.directory,
                    root,
                    options,
                    "-listPerson",
                ),
                _corpus_artifact_path(
                    node.directory,
                    root,
                    options,
                    "-listOrg",
                ),
            }
        )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in desired and resolved not in protected:
            candidate.unlink(missing_ok=True)

    def by_path(path: Path) -> str:
        return str(path).casefold()

    return (
        sorted(corpus_files, key=by_path),
        sorted(list_person_files, key=by_path),
        sorted(list_org_files, key=by_path),
    )


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
            chunk_count=_status_count(previous, "chunk_count"),
            recovery_count=_status_count(previous, "recovery_count"),
            message="unchanged completed bundle",
            warning_count=_status_count(previous, "warning_count"),
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
        if recycle_workers and sys.version_info >= (3, 11):
            executor = ProcessPoolExecutor(
                max_workers=worker_count,
                max_tasks_per_child=1,
            )
        else:
            executor = ProcessPoolExecutor(max_workers=worker_count)
        with executor:
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
