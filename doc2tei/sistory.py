"""Adapter for the bundled ``sistory-dl`` submodule."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import importlib.util
from pathlib import Path
from types import ModuleType
import sys
from urllib.parse import unquote, urlsplit

from type_decs import SIstoryDownloadStatus


DEFAULT_SISTORY_DL_DIRECTORY = Path(__file__).parents[1] / "sistory-dl"
STAT_FIELDS = (
    "folders",
    "publications",
    "files_found",
    "downloaded",
    "renamed",
    "skipped",
    "failed",
)


@dataclass(frozen=True)
class SIstoryDownloadResult:
    menu_path: str
    output: str
    status: SIstoryDownloadStatus
    stats: dict[str, int]
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_sistory_menu_path(value: str) -> str:
    """Accept a bare menu path or a complete SIstory menu URL."""

    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        if parsed.hostname not in {"sistory.si", "www.sistory.si"}:
            raise ValueError(f"Not a SIstory menu URL: {value}")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            menu_index = parts.index("menu")
        except ValueError as error:
            raise ValueError(f"Not a SIstory menu URL: {value}") from error
        parts = parts[menu_index + 1 :]
    else:
        parts = [unquote(part) for part in candidate.strip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError(f"Invalid SIstory menu path: {value}")
    return "/".join(parts)


@lru_cache(maxsize=None)
def _load_sistory_module(directory: str) -> ModuleType:
    root = Path(directory)
    entry_point = root / "main.py"
    if not entry_point.is_file():
        raise FileNotFoundError(
            f"SIstory downloader not found at {entry_point}. "
            "Run 'git submodule update --init sistory-dl'."
        )
    module_name = "doc2tei_bundled_sistory_dl"
    spec = importlib.util.spec_from_file_location(module_name, entry_point)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load SIstory downloader from {entry_point}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def sistory_filesystem_path(
    path: str | Path,
    *,
    downloader_directory: str | Path = DEFAULT_SISTORY_DL_DIRECTORY,
) -> Path:
    """Delegate Windows long-path handling to the bundled downloader."""

    value = Path(path).expanduser().absolute()
    try:
        module = _load_sistory_module(str(Path(downloader_directory).resolve()))
        return module.filesystem_path(value)
    except Exception:
        # Cache discovery must still work when the downloader cannot be loaded.
        return value


def download_sistory_menu(
    menu_path: str,
    output_directory: str | Path,
    *,
    downloader_directory: str | Path = DEFAULT_SISTORY_DL_DIRECTORY,
    dry_run: bool = False,
) -> SIstoryDownloadResult:
    """Download one menu and return structured, non-throwing acquisition status."""

    output = Path(output_directory).expanduser().resolve()
    try:
        normalized = normalize_sistory_menu_path(menu_path)
    except ValueError as error:
        return SIstoryDownloadResult(
            menu_path=menu_path,
            output=str(output),
            status="failed",
            stats={name: 0 for name in STAT_FIELDS},
            message=str(error),
        )

    try:
        module = _load_sistory_module(str(Path(downloader_directory).resolve()))
        options_type = getattr(module, "Options")
        run = getattr(module, "run")
        options = options_type(
            root_segments=normalized.split("/"),
            output_directory=output,
            dry_run=dry_run,
        )
        raw_stats = asdict(run(options))
        stats = {name: int(raw_stats.get(name, 0)) for name in STAT_FIELDS}
        failed = stats.get("failed", 0)
        return SIstoryDownloadResult(
            menu_path=normalized,
            output=str(output),
            status="partial" if failed else "ok",
            stats=stats,
            message=(f"{failed} download operation(s) failed" if failed else ""),
        )
    except Exception as error:
        return SIstoryDownloadResult(
            menu_path=normalized,
            output=str(output),
            status="failed",
            stats={name: 0 for name in STAT_FIELDS},
            message=f"{type(error).__name__}: {error}"[:500],
        )
