from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import importlib.util
import inspect
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterable, Mapping, cast
import xml.etree.ElementTree as ET

import engine
from engine import Chunk, PDFChunk, WordChunk

from .config import Rule as ConfigRule


@dataclass(frozen=True)
class LoadedConfig:
    module: ModuleType
    path: Path
    config: Mapping[str, Any]
    cosmetic_annotations: Mapping[str, Any]
    get_chunks: Any
    log: Any


@dataclass
class ParseDiagnostics:
    """Small, serializable audit trail for tuning document configurations."""

    input: str
    config: str
    chunk_count: int = 0
    unmatched_count: int = 0
    rule_counts: Counter[str] = field(default_factory=Counter)
    page_counts: Counter[str] = field(default_factory=Counter)
    font_counts: Counter[str] = field(default_factory=Counter)
    font_size_counts: Counter[str] = field(default_factory=Counter)
    unmatched_samples: list[dict[str, Any]] = field(default_factory=list)
    max_samples: int = 25

    def observe(self, chunk: Chunk, matched_rule: str | None) -> None:
        self.chunk_count += 1
        if isinstance(chunk, PDFChunk):
            self.page_counts[str(chunk.page_num + 1)] += 1
            self.font_counts[self._font_label(chunk)] += 1
            self.font_size_counts[f"{chunk.font_size:.2f}"] += 1

        if matched_rule is not None:
            self.rule_counts[matched_rule] += 1
            return

        self.unmatched_count += 1
        if len(self.unmatched_samples) < self.max_samples:
            sample: dict[str, Any] = {
                "text": chunk.text,
                "x": round(float(chunk.x), 2),
                "y": round(float(chunk.y), 2),
            }
            if isinstance(chunk, PDFChunk):
                sample.update(
                    page=chunk.page_num + 1,
                    font_size=round(chunk.font_size, 2),
                    line_text=chunk.line_text,
                )
            self.unmatched_samples.append(sample)

    @staticmethod
    def _font_label(chunk: PDFChunk) -> str:
        style = []
        if chunk.bold:
            style.append("bold")
        if chunk.italic:
            style.append("italic")
        suffix = "+".join(style) if style else "regular"
        return f"{chunk.font_name} ({suffix})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "config": self.config,
            "chunk_count": self.chunk_count,
            "unmatched_count": self.unmatched_count,
            "rule_counts": dict(sorted(self.rule_counts.items())),
            "page_counts": dict(
                sorted(self.page_counts.items(), key=lambda item: int(item[0]))
            ),
            "font_counts": dict(sorted(self.font_counts.items())),
            "font_size_counts": dict(sorted(self.font_size_counts.items())),
            "unmatched_samples": self.unmatched_samples,
        }


@dataclass
class ParseResult:
    root: ET.Element[str]
    diagnostics: ParseDiagnostics
    data: dict[str, Any] = field(default_factory=dict)

    def to_bytes(self, *, xml_declaration: bool = False) -> bytes:
        return ET.tostring(self.root, encoding="utf-8", xml_declaration=xml_declaration)

    def write_xml(self, path: str | Path, *, xml_declaration: bool = False) -> None:
        Path(path).write_bytes(self.to_bytes(xml_declaration=xml_declaration))

    def write_diagnostics(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.diagnostics.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def write_data(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_config(path: str | Path = "config.py") -> LoadedConfig:
    """Load a config from an explicit path without depending on import cwd."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"config file does not exist: {config_path}")
    module_name = f"doc2tei_user_config_{abs(hash(config_path))}"
    spec = importlib.util.spec_from_file_location(module_name, config_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load config: {config_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(config_path.parent))
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        sys.path.pop(0)

    required = ("CONFIG", "COSMETIC_ANNOTATIONS", "get_chunks")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise ValueError(f"config {config_path} is missing: {', '.join(missing)}")
    config = getattr(module, "CONFIG")
    if "alignments" not in config:
        raise ValueError(f"config {config_path} has no CONFIG['alignments']")
    return LoadedConfig(
        module=module,
        path=config_path,
        config=config,
        cosmetic_annotations=getattr(module, "COSMETIC_ANNOTATIONS"),
        get_chunks=getattr(module, "get_chunks"),
        log=getattr(module, "log", lambda *_args, **_kwargs: None),
    )


def _rule_mapping(rule: Any) -> Mapping[str, Any]:
    if isinstance(rule, ConfigRule):
        return rule.as_mapping()
    if isinstance(rule, Mapping):
        return rule
    raise TypeError(f"rule must be a Rule or mapping, got {type(rule).__name__}")


def _get_center_point(chunk: Chunk, log: Any) -> float:
    if isinstance(chunk, WordChunk):
        center = chunk.x + chunk.w / 2
        log(f"center of container: {center}")
        return center
    return -1


def _apply_rule(rule: Mapping[str, Any], chunk: Chunk) -> None:
    action = rule.get("action")
    if callable(action):
        action(chunk)
    append_func = rule.get("append_func")
    if callable(append_func):
        append_func(chunk)
    else:
        engine.append(chunk)
    after_append = rule.get("after_append")
    if callable(after_append):
        after_append()


def _match_group(
    group_name: str,
    group: Mapping[str, Any],
    chunk: Chunk,
    log: Any,
) -> str | None:
    run_immediate = group.get("run_immediate")
    if callable(run_immediate):
        run_immediate()
    if not chunk.text or chunk.text == "\n":
        return None

    fallback: tuple[str, Mapping[str, Any]] | None = None
    for key, rule_spec in group.items():
        if key == "run_immediate" or callable(rule_spec):
            continue
        rule = _rule_mapping(rule_spec)
        if "test" not in rule:
            continue
        if (
            "alignment" in rule
            and isinstance(chunk, WordChunk)
            and chunk.paragraph.alignment != rule["alignment"]
        ):
            continue
        test = rule["test"]
        if test == "_else":
            fallback = (key, rule)
            continue
        if callable(test) and test(chunk):
            log(f"{key}: {chunk.text}")
            _apply_rule(rule, chunk)
            return f"{group_name}.{key}"

    if fallback is not None:
        key, rule = fallback
        log(f"{key} (fallback): {chunk.text}")
        _apply_rule(rule, chunk)
        return f"{group_name}.{key}"
    return None


def _alignment_group(config: Mapping[str, Any], chunk: Chunk, log: Any) -> str | None:
    alignments = config["alignments"]
    router = config.get("route_alignment")
    if callable(router):
        selected = router(chunk)
        if selected not in alignments:
            raise KeyError(f"route_alignment returned unknown group: {selected!r}")
        return cast(str, selected)
    if isinstance(chunk, PDFChunk):
        return "any"

    center = _get_center_point(chunk, log)
    if 4550 < center < 6560 and "center" in alignments:
        return "center"
    if 2500 < center < 3660 and "left" in alignments:
        log("Chunk is on left side")
        return "left"
    if 7820 < center < 8460 and "right" in alignments:
        log("Chunk is on right side")
        return "right"
    if "_else" in alignments:
        log("Chunk is not on left nor right side")
        return "_else"
    return None


def _call_hook(callback: Any, result: ParseResult) -> None:
    if not callable(callback):
        return
    signature = inspect.signature(callback)
    accepts_result = any(
        parameter.kind
        in (
            parameter.POSITIONAL_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.VAR_POSITIONAL,
        )
        for parameter in signature.parameters.values()
    )
    callback(result) if accepts_result else callback()


def parse_document(
    input_path: str | Path,
    *,
    config: str | Path | LoadedConfig = "config.py",
    chunks: Iterable[Chunk] | None = None,
) -> ParseResult:
    """Parse one document and return XML, diagnostics and config-produced data."""

    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"input file does not exist: {source}")
    engine.reset()
    engine.COSMETIC_ANNOTATIONS = loaded.cosmetic_annotations
    engine.on_pop = loaded.config.get("on_pop")
    diagnostics = ParseDiagnostics(input=str(source), config=str(loaded.path))
    result = ParseResult(root=engine.root, diagnostics=diagnostics)
    _call_hook(loaded.config.get("on_start"), result)

    stream = chunks if chunks is not None else loaded.get_chunks(str(source))
    for chunk in stream:
        loaded.log("-- parsing text --")
        group_name = _alignment_group(loaded.config, chunk, loaded.log)
        matched = None
        if group_name is not None:
            group = loaded.config["alignments"][group_name]
            matched = _match_group(group_name, group, chunk, loaded.log)
        if matched is None:
            engine.append(chunk)
        diagnostics.observe(chunk, matched)
        loaded.log(
            f"chunk.text={chunk.text!r}, x={chunk.x}, y={chunk.y}, chunk={chunk!r}"
        )
        loaded.log("\n")

    while len(engine.stack) > 1:
        engine.pop()
    engine.commit_children(engine.stack[0])

    _call_hook(loaded.config.get("on_end"), result)
    return result
