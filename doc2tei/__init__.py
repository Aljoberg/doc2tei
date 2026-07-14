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
from .parser import LoadedConfig, ParseDiagnostics, ParseResult, load_config, parse_document
from .selectors import AllOf, AnyOf, Attribute, Between, LineStart, Metadata, Not, Text

__all__ = [
    "AllOf",
    "AnyOf",
    "Attribute",
    "Between",
    "CharacterPDFExtractor",
    "LineRecord",
    "LineStart",
    "LoadedConfig",
    "Metadata",
    "Not",
    "PageRange",
    "ParseDiagnostics",
    "ParseResult",
    "Rule",
    "RunRecord",
    "SpeakerUtteranceHook",
    "Text",
    "WordPDFExtractor",
    "load_config",
    "parse_document",
    "rule",
    "rule_group",
]
