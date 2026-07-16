"""Public helpers for building and running doc2tei configurations."""

from .config import Rule, rule, rule_group
from .extractors import (
    CharacterPDFExtractor,
    LineRecord,
    PageRange,
    RunRecord,
    WordPDFExtractor,
)
from .helpers import SpeakerUtteranceHook
from .parser import (
    LoadedConfig,
    ParseDiagnostics,
    ParseResult,
    load_config,
    parse_document,
)
from .selectors import AllOf, AnyOf, Attribute, Between, LineStart, Metadata, Not, Text
from .tei_header import (
    Change,
    Funder,
    Measure,
    Meeting,
    Person,
    RespStmt,
    Setting,
    SourceBibl,
    TEIHeader,
    fill_counts,
)

__all__ = [
    "AllOf",
    "AnyOf",
    "Attribute",
    "Between",
    "Change",
    "CharacterPDFExtractor",
    "Funder",
    "LineRecord",
    "LineStart",
    "LoadedConfig",
    "Measure",
    "Meeting",
    "Metadata",
    "Not",
    "PageRange",
    "ParseDiagnostics",
    "ParseResult",
    "Person",
    "RespStmt",
    "Rule",
    "RunRecord",
    "Setting",
    "SourceBibl",
    "SpeakerUtteranceHook",
    "TEIHeader",
    "Text",
    "WordPDFExtractor",
    "fill_counts",
    "load_config",
    "parse_document",
    "rule",
    "rule_group",
]
