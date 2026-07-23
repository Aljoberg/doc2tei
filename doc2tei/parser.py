from __future__ import annotations

from collections.abc import Iterable, Mapping
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import importlib.util
import inspect
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Callable, cast, Literal
from os.path import basename
import xml.etree.ElementTree as ET

import engine
from engine import PDFChunk, WordChunk
from type_decs import (
    Chunk,
    ChunkExtractor,
    CosmeticAnnotations,
    Logger,
    OnPop,
    RecoveryHandler,
    RuleCallback,
)

from .config import Rule as ConfigRule
from .helpers import build_list_person
from .tei_header import TEIHeader, fill_counts


@dataclass(frozen=True)
class NormalizedRule:
    test: Callable[[Chunk], bool] | Literal["_else"]
    action: RuleCallback | None = None
    append_func: RuleCallback | None = None
    after_append: Callable[[], None] | None = None
    alignment: object | None = None


@dataclass(frozen=True)
class CompiledRuleGroup:
    """Immutable execution plan for a rule group.

    Config mappings are declarative and remain unchanged while a document is
    parsed, so validating and normalizing their callbacks once avoids doing the
    same work for every chunk.
    """

    run_immediate: Callable[[], None] | None
    rules: tuple[tuple[str, NormalizedRule], ...]


@dataclass(frozen=True)
class LoadedConfig:
    module: ModuleType
    path: Path
    config: Mapping[str, object]
    cosmetic_annotations: CosmeticAnnotations
    get_chunks: ChunkExtractor
    log: Logger


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
    unmatched_samples: list[dict[str, object]] = field(default_factory=list)
    recovery_counts: Counter[str] = field(default_factory=Counter)
    recovery_samples: list[dict[str, object]] = field(default_factory=list)
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
            sample: dict[str, object] = {
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

    def recover(
        self, stage: str, error: BaseException | str, chunk: Chunk | None = None
    ) -> None:
        """Record a fail-soft conversion event without aborting the document."""
        self.recovery_counts[stage] += 1
        if len(self.recovery_samples) >= self.max_samples:
            return
        message = str(error)
        if not isinstance(error, str):
            message = f"{type(error).__name__}: {message}"
        sample: dict[str, object] = {"stage": stage, "message": message[:500]}
        if chunk is not None:
            sample["text"] = chunk.text
            if isinstance(chunk, PDFChunk):
                sample["page"] = chunk.page_num + 1
        self.recovery_samples.append(sample)

    def as_dict(self) -> dict[str, object]:
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
            "recovery_counts": dict(sorted(self.recovery_counts.items())),
            "recovery_samples": self.recovery_samples,
        }


@dataclass
class ParseResult:
    root: ET.Element
    diagnostics: ParseDiagnostics
    data: dict[str, object] = field(default_factory=dict)

    @staticmethod
    def _xml_bytes(
        root: ET.Element,
        *,
        xml_declaration: bool = False,
        pretty: bool = False,
    ) -> bytes:
        serializable = root
        if pretty:
            # Work on a copy: indentation adds whitespace nodes, and callers
            # may still need the original mixed-content tree afterward.
            serializable = deepcopy(root)
            ET.indent(serializable, space="  ")
        return ET.tostring(
            serializable,
            encoding="utf-8",
            xml_declaration=xml_declaration,
        )

    def to_bytes(self, *, xml_declaration: bool = False, pretty: bool = False) -> bytes:
        return self._xml_bytes(
            self.root,
            xml_declaration=xml_declaration,
            pretty=pretty,
        )

    def write_xml(
        self,
        path: str | Path,
        *,
        xml_declaration: bool = False,
        pretty: bool = False,
    ) -> None:
        content = self.to_bytes(
            xml_declaration=xml_declaration,
            pretty=pretty,
        )
        Path(path).write_bytes(content + (b"\n" if pretty else b""))

    def write_diagnostics(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.diagnostics.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )

    def write_data(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )

    def write_list_person(
        self,
        path: str | Path,
        *,
        data_key: str = "speakers",
        xml_declaration: bool = False,
        pretty: bool = False,
        include_wikidata: bool = False,
        wikidata_workers: int = 4,
        wikidata_timeout: float = 20.0,
    ) -> None:
        mapping = self.data.get(data_key)
        safe_mapping = (
            {
                str(key): str(value)
                for key, value in mapping.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if isinstance(mapping, Mapping)
            else {}
        )
        root = build_list_person(
            safe_mapping,
            include_wikidata=include_wikidata,
            wikidata_workers=wikidata_workers,
            wikidata_timeout=wikidata_timeout,
        )
        content = self._xml_bytes(
            root,
            xml_declaration=xml_declaration,
            pretty=pretty,
        )
        Path(path).write_bytes(content + (b"\n" if pretty else b""))


def load_config(path: str | Path) -> LoadedConfig:
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
    config_value = getattr(module, "CONFIG")
    if not isinstance(config_value, Mapping):
        raise TypeError(f"CONFIG in {config_path} must be a mapping")
    config = cast(Mapping[str, object], config_value)
    mode = config.get("mode")
    required_rule_key = "rules" if mode == "pdf" else "alignments"
    if mode not in ("pdf", "word"):
        raise ValueError(f"config {config_path} has invalid CONFIG['mode']: {mode!r}")
    if required_rule_key not in config:
        raise ValueError(f"config {config_path} has no CONFIG[{required_rule_key!r}]")

    annotations = cast(CosmeticAnnotations, getattr(module, "COSMETIC_ANNOTATIONS"))
    extractor = cast(ChunkExtractor, getattr(module, "get_chunks"))

    def quiet_log(*_args: object, **_kwargs: object) -> None:
        return None

    logger = cast(Logger, getattr(module, "log", quiet_log))
    return LoadedConfig(
        module=module,
        path=config_path,
        config=config,
        cosmetic_annotations=annotations,
        get_chunks=extractor,
        log=logger,
    )


def _callback(value: object, name: str) -> RuleCallback | None:
    if value is None:
        return None
    if not callable(value):
        raise TypeError(f"rule {name} must be callable")
    return cast(RuleCallback, value)


def _after_callback(value: object) -> Callable[[], None] | None:
    if value is None:
        return None
    if not callable(value):
        raise TypeError("rule after_append must be callable")
    return cast(Callable[[], None], value)


def _normalize_rule(rule: object) -> NormalizedRule:
    if isinstance(rule, ConfigRule):
        mapping = rule.as_mapping()
    elif isinstance(rule, Mapping):
        mapping = cast(Mapping[str, object], rule)
    else:
        raise TypeError(f"rule must be a Rule or mapping, got {type(rule).__name__}")

    test_value = mapping.get("test")
    if test_value == "_else":
        test: Callable[[Chunk], bool] | Literal["_else"] = "_else"
    elif callable(test_value):
        test = cast(Callable[[Chunk], bool], test_value)
    else:
        raise TypeError("rule test must be callable or '_else'")
    return NormalizedRule(
        test=test,
        action=_callback(mapping.get("action"), "action"),
        append_func=_callback(mapping.get("append_func"), "append_func"),
        after_append=_after_callback(mapping.get("after_append")),
        alignment=mapping.get("alignment"),
    )


def _compile_group(
    group: Mapping[str, object],
    recover: RecoveryHandler | None = None,
) -> CompiledRuleGroup:
    """Validate a config rule group once and retain its declared order."""

    immediate_value = group.get("run_immediate")
    run_immediate = (
        cast(Callable[[], None], immediate_value) if callable(immediate_value) else None
    )
    rules: list[tuple[str, NormalizedRule]] = []
    for key, rule_spec in group.items():
        # Callable entries are the legacy spelling for immediate hooks. Keep
        # accepting and skipping them exactly as the former hot loop did.
        if key == "run_immediate" or callable(rule_spec):
            continue
        try:
            rules.append((key, _normalize_rule(rule_spec)))
        except Exception as error:
            if recover is None:
                raise
            recover(f"rule.{key}.compile", error, None)
    return CompiledRuleGroup(run_immediate, tuple(rules))


def _get_center_point(chunk: Chunk, log: Logger | None) -> float:
    if isinstance(chunk, WordChunk):
        center = chunk.x + chunk.w / 2
        if log is not None:
            log(f"center of container: {center}")
        return center
    return -1


def _apply_rule(rule: NormalizedRule, chunk: Chunk) -> None:
    if rule.action is not None:
        rule.action(chunk)
    if rule.append_func is not None:
        rule.append_func(chunk)
    else:
        engine.append(chunk)
    if rule.after_append is not None:
        rule.after_append()


def _match_group(
    group_name: str | None,
    group: CompiledRuleGroup,
    chunk: Chunk,
    log: Logger | None,
    recover: RecoveryHandler | None = None,
) -> str | None:
    if group.run_immediate is not None:
        try:
            group.run_immediate()
        except Exception as error:
            if recover is None:
                raise
            recover("rule.run_immediate", error, chunk)
    if not chunk.text or chunk.text == "\n":
        return None

    fallback: tuple[str, NormalizedRule] | None = None
    for key, rule in group.rules:
        if (
            rule.alignment is not None
            and isinstance(chunk, WordChunk)
            and chunk.paragraph.alignment != rule.alignment
        ):
            continue
        test = rule.test
        if test == "_else":
            fallback = (key, rule)
            continue
        try:
            matches = bool(callable(test) and test(chunk))
        except Exception as error:
            if recover is None:
                raise
            recover(f"rule.{key}.test", error, chunk)
            continue
        if matches:
            if log is not None:
                log(f"{key}: {chunk.text}")
            try:
                _apply_rule(rule, chunk)
            except Exception as error:
                if recover is None:
                    raise
                recover(f"rule.{key}.apply", error, chunk)
                _recover_chunk(chunk, f"rule {key} failed")
                return "RECOVERED"
            return f"{group_name}.{key}" if group_name else key

    if fallback is not None:
        key, rule = fallback
        if log is not None:
            log(f"{key} (fallback): {chunk.text}")
        try:
            _apply_rule(rule, chunk)
        except Exception as error:
            if recover is None:
                raise
            recover(f"rule.{key}.apply", error, chunk)
            _recover_chunk(chunk, f"fallback rule {key} failed")
            return "RECOVERED"
        return f"{group_name}.{key}" if group_name else key
    return None


def _recover_chunk(chunk: Chunk, reason: str) -> None:
    """Best-effort containment for a chunk whose configured rule failed."""
    try:
        while len(engine.stack) > 1 and engine.stack[-1].cosmetic:
            engine.pop()
    except Exception:
        # A cosmetic close callback must not prevent raw text recovery.
        engine.on_pop = None

    current = next(
        (entry for entry in reversed(engine.stack) if not entry.cosmetic), None
    )
    try:
        if current is not None and getattr(chunk, "is_line_start", False):
            if current.element.tag == "div":
                engine.push("p", type="unparsed")
            elif current.element.tag == "u":
                engine.push("seg", type="unparsed")
        engine.append_comment(f"doc2tei recovery: {reason}; source text preserved")
        engine.append(chunk, should_annotate=[])
    except Exception:
        # Last resort: append XML-safe source text directly to the live buffer.
        text = engine.xml_safe_text(chunk.text).strip()
        if text:
            if chunk.space_before and engine.children:
                engine.children.append(" ")
            engine.children.append(text)


def _alignment_groups(config: Mapping[str, object]) -> Mapping[str, object]:
    value = config["alignments"]
    if not isinstance(value, Mapping):
        raise TypeError("CONFIG['alignments'] must be a mapping")
    return cast(Mapping[str, object], value)


def _pdf_rules(config: Mapping[str, object]) -> Mapping[str, object]:
    value = config["rules"]
    if not isinstance(value, Mapping):
        raise TypeError("CONFIG['rules'] must be a mapping")
    return cast(Mapping[str, object], value)


def _alignment_group(
    alignments: Mapping[str, object],
    router: object,
    chunk: Chunk,
    log: Logger | None,
) -> str | None:
    if callable(router):
        selected = cast(Callable[[Chunk], str], router)(chunk)
        if selected not in alignments:
            raise KeyError(f"route_alignment returned unknown group: {selected!r}")
        return selected
    center = _get_center_point(chunk, log)
    if 4550 < center < 6560 and "center" in alignments:
        return "center"
    if 2500 < center < 3660 and "left" in alignments:
        if log is not None:
            log("Chunk is on left side")
        return "left"
    if 7820 < center < 8460 and "right" in alignments:
        if log is not None:
            log("Chunk is on right side")
        return "right"
    if "_else" in alignments:
        if log is not None:
            log("Chunk is not on left nor right side")
        return "_else"
    return None


def _make_document(
    factory: object,
) -> tuple[ET.Element, ET.Element] | None:
    if factory is None:
        return None
    if not callable(factory):
        raise TypeError("CONFIG['document'] must be callable")
    document = cast("Callable[[], tuple[ET.Element, ET.Element]]", factory)()
    root, content = document
    if content is not root and content not in root.iter():
        raise ValueError(
            "CONFIG['document'] content element must be a descendant of the root"
        )
    return document


def _install_header(spec: object) -> ET.Element | None:
    """Resolve CONFIG['tei_header'] and insert it as the root's first child."""
    if spec is None:
        return None
    if callable(spec):
        spec = cast(Callable[[], object], spec)()
    if isinstance(spec, TEIHeader):
        for key, value in spec.tei_attributes().items():
            engine.root.set(key, value)
        header = spec.build()
    elif isinstance(spec, ET.Element):
        header = spec
    else:
        raise TypeError(
            "CONFIG['tei_header'] must be a TEIHeader, an Element, "
            "or a callable returning one"
        )
    engine.root.insert(0, header)
    return header


def _call_hook(callback: object, result: ParseResult) -> None:
    if not callable(callback):
        return
    hook = cast(Callable[..., None], callback)
    signature = inspect.signature(hook)
    accepts_result = any(
        parameter.kind
        in (
            parameter.POSITIONAL_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.VAR_POSITIONAL,
        )
        for parameter in signature.parameters.values()
    )
    hook(result) if accepts_result else hook()


def _sanitize_tree(root: ET.Element, diagnostics: ParseDiagnostics) -> None:
    """Make text, attributes, and IDs serializable and deterministically unique."""
    used_ids: set[str] = set()
    id_map: dict[str, str] = {}
    id_keys = ("xml:id", "{http://www.w3.org/XML/1998/namespace}id")

    for element in root.iter():
        if element.text is not None:
            element.text = engine.xml_safe_text(element.text)
        if element.tail is not None:
            element.tail = engine.xml_safe_text(element.tail)
        for key, value in list(element.attrib.items()):
            element.set(key, engine.xml_safe_text(value))
        for key in id_keys:
            raw_id = element.attrib.get(key)
            if raw_id is None:
                continue
            base_id = engine.sanitize_xml_id(raw_id)
            unique_id = base_id
            suffix = 2
            while unique_id in used_ids:
                unique_id = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(unique_id)
            id_map.setdefault(raw_id, unique_id)
            if unique_id != raw_id:
                diagnostics.recover(
                    "xml.id", f"repaired xml:id {raw_id!r} as {unique_id!r}"
                )
                element.set(key, unique_id)

    for element in root.iter():
        for key in ("who", "target", "corresp"):
            value = element.attrib.get(key)
            if not value:
                continue
            tokens: list[str] = []
            changed = False
            for token in value.split():
                if not token.startswith("#"):
                    tokens.append(token)
                    continue
                raw_id = token[1:]
                safe_id = id_map.get(raw_id, engine.sanitize_xml_id(raw_id))
                replacement = f"#{safe_id}"
                tokens.append(replacement)
                changed = changed or replacement != token
            if changed:
                diagnostics.recover(
                    "xml.reference", f"repaired {key} reference {value!r}"
                )
                element.set(key, " ".join(tokens))


def _append_conversion_notes(result: ParseResult) -> None:
    """Expose recoveries and non-fatal review warnings in the TEI header."""
    recovery_messages = [
        f"{sample['stage']}: {sample['message']}"
        for sample in result.diagnostics.recovery_samples
    ]
    exported = result.data.get("recoveries")
    if isinstance(exported, list):
        recovery_messages.extend(str(message) for message in exported)
    warnings = result.data.get("warnings")
    warning_messages = (
        [str(message) for message in warnings] if isinstance(warnings, list) else []
    )
    note_groups = (
        ("conversionRecovery", recovery_messages),
        ("conversionWarning", warning_messages),
    )
    note_groups = tuple(
        (note_type, list(dict.fromkeys(message for message in messages if message)))
        for note_type, messages in note_groups
    )
    if not any(messages for _, messages in note_groups):
        return

    header = result.root.find("teiHeader")
    if header is None:
        header = TEIHeader().build()
        result.root.insert(0, header)
    file_desc = header.find("fileDesc")
    if file_desc is None:
        return
    notes_stmt = file_desc.find("notesStmt")
    if notes_stmt is None:
        notes_stmt = ET.Element("notesStmt")
        source_desc = file_desc.find("sourceDesc")
        index = (
            list(file_desc).index(source_desc)
            if source_desc is not None
            else len(file_desc)
        )
        file_desc.insert(index, notes_stmt)
    for note_type, messages in note_groups:
        for index, message in enumerate(messages, start=1):
            note = ET.SubElement(notes_stmt, "note", type=note_type, n=str(index))
            note.text = engine.xml_safe_text(message)


def parse_document(
    input_path: str | Path,
    *,
    config: str | Path | LoadedConfig,
    chunks: Iterable[Chunk] | None = None,
) -> ParseResult:
    """Parse one document and return XML, diagnostics and config-produced data."""

    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"input file does not exist: {source}")

    recover_errors = bool(loaded.config.get("recover_errors", False))
    diagnostics = ParseDiagnostics(input=str(source), config=str(loaded.path))

    def recover(stage: str, error: BaseException, chunk: Chunk | None = None) -> None:
        diagnostics.recover(stage, error, chunk)

    try:
        document = _make_document(loaded.config.get("document"))
    except Exception as error:
        if not recover_errors:
            raise
        recover("document", error)
        document = None

    engine.reset(document=document)
    engine.COSMETIC_ANNOTATIONS = loaded.cosmetic_annotations
    on_pop = loaded.config.get("on_pop")
    engine.on_pop = cast(OnPop, on_pop) if callable(on_pop) else None
    engine.filename = basename(input_path)
    engine.auto_xml_ids = bool(loaded.config.get("auto_xml_ids", False))
    try:
        header = _install_header(loaded.config.get("tei_header"))
    except Exception as error:
        if not recover_errors:
            raise
        recover("tei_header", error)
        header = TEIHeader(main_titles={"": source.stem}).build()
        engine.root.insert(0, header)

    result = ParseResult(root=engine.root, diagnostics=diagnostics)
    try:
        _call_hook(loaded.config.get("on_start"), result)
    except Exception as error:
        if not recover_errors:
            raise
        recover("hook.on_start", error)

    debug_log = loaded.log if bool(loaded.config.get("debug", False)) else None
    rule_recovery = recover if recover_errors else None
    compiled_groups: dict[int, CompiledRuleGroup] = {}

    def compiled(group: Mapping[str, object]) -> CompiledRuleGroup:
        key = id(group)
        plan = compiled_groups.get(key)
        if plan is None:
            plan = _compile_group(group, rule_recovery)
            compiled_groups[key] = plan
        return plan

    is_pdf = loaded.config.get("mode") == "pdf"
    pdf_group: CompiledRuleGroup | None = None
    alignment_groups: Mapping[str, object] = {}
    alignment_router: object = None
    if is_pdf:
        try:
            pdf_group = compiled(_pdf_rules(loaded.config))
        except Exception as error:
            if not recover_errors:
                raise
            recover("rules.compile", error)
            # An invalid rule container must not abort a lossless conversion;
            # unmatched chunks will fall through to the raw append path.
            pdf_group = CompiledRuleGroup(None, ())
    else:
        alignment_router = loaded.config.get("route_alignment")
        try:
            alignment_groups = _alignment_groups(loaded.config)
        except Exception as error:
            if not recover_errors:
                raise
            recover("alignments.compile", error)
    stream = chunks if chunks is not None else loaded.get_chunks(str(source))
    iterator = iter(stream)
    while True:
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        except Exception as error:
            if not recover_errors:
                raise
            recover("extractor", error)
            break

        try:
            if debug_log is not None:
                debug_log("-- parsing text --")
            matched = None
            if is_pdf:
                assert pdf_group is not None
                matched = _match_group(
                    None,
                    pdf_group,
                    chunk,
                    debug_log,
                    rule_recovery,
                )
            else:
                group_name = _alignment_group(
                    alignment_groups, alignment_router, chunk, debug_log
                )
                if group_name is None:
                    group_value = None
                else:
                    group_value = alignment_groups[group_name]
                if group_value is not None:
                    if not isinstance(group_value, Mapping):
                        raise TypeError(
                            f"alignment group {group_name!r} must be a mapping"
                        )
                    group = cast(Mapping[str, object], group_value)
                    matched = _match_group(
                        group_name,
                        compiled(group),
                        chunk,
                        debug_log,
                        rule_recovery,
                    )
            if matched is None:
                engine.append(chunk)
        except Exception as error:
            if not recover_errors:
                raise
            recover("parser.chunk", error, chunk)
            _recover_chunk(chunk, "parser chunk processing failed")
            matched = "RECOVERED"

        diagnostics.observe(chunk, matched)
        if debug_log is not None:
            try:
                debug_log(
                    f"chunk.text={chunk.text!r}, x={chunk.x}, "
                    f"y={chunk.y}, chunk={chunk!r}"
                )
                debug_log("\n")
            except Exception as error:
                if not recover_errors:
                    raise
                recover("logger", error, chunk)

    if diagnostics.chunk_count == 0 and recover_errors:
        diagnostics.recover(
            "extractor.no_text",
            "No machine-readable text chunks were extracted; an empty TEI skeleton was retained.",
        )

    while len(engine.stack) > 1:
        previous_depth = len(engine.stack)
        try:
            engine.pop()
        except Exception as error:
            if not recover_errors:
                raise
            recover("stack.close", error)
            engine.on_pop = None
            if len(engine.stack) == previous_depth:
                # The pop failed before changing the stack; retry without hook.
                engine.pop()
    try:
        engine.commit_children(engine.stack[0])
    except Exception as error:
        if not recover_errors:
            raise
        recover("stack.commit", error)

    if header is not None:
        # tag usage / extent counts only exist now; runs before on_end so a
        # config can still inspect or override them there
        try:
            fill_counts(engine.root)
        except Exception as error:
            if not recover_errors:
                raise
            recover("tei_header.counts", error)

    try:
        _call_hook(loaded.config.get("on_end"), result)
    except Exception as error:
        if not recover_errors:
            raise
        recover("hook.on_end", error)
    if recover_errors:
        _sanitize_tree(result.root, diagnostics)
        _append_conversion_notes(result)
    return result
