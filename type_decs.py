from __future__ import annotations

from typing import Callable, NotRequired, TypedDict, Literal, TYPE_CHECKING, TypeAlias
import xml.etree.ElementTree as ET
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

if TYPE_CHECKING:
    # only needed for type hints
    # actually importing them would create a cycle (engine needs type decs, type decs needs engine)
    from engine import WordChunk, PDFChunk, Chunk, StackEntry
    from doc2tei.tei_header import TEIHeader


WordRunTest = Callable[["WordChunk"], bool | None]
WordAction = Callable[["WordChunk"], None]
WordAppendFunc = Callable[["WordChunk"], None]
WordAfterPush = Callable[[], None]
RunImmediate = Callable[[], None]
OnPop = Callable[["StackEntry"], None]
OnEnd = Callable[..., None]


class WordRule(TypedDict):
    alignment: NotRequired[WD_PARAGRAPH_ALIGNMENT]
    test: WordRunTest | Literal["_else"]
    action: NotRequired[WordAction]
    append_func: NotRequired[WordAppendFunc]
    after_append: NotRequired[WordAfterPush]


WordRuleGroup = dict[str, WordRule | RunImmediate]

# the <TEI> root and the (descendant) element parsed content is appended into
DocumentFactory: TypeAlias = (
    "Callable[[], tuple[ET.Element[str], ET.Element[str]]]"
)
# a built <teiHeader>, a TEIHeader spec, or a callable producing either
TEIHeaderSpec: TypeAlias = (
    "TEIHeader | ET.Element[str] | Callable[[], TEIHeader | ET.Element[str]]"
)


class WordConfig(TypedDict):
    mode: Literal["word"]
    alignments: dict[str, WordRuleGroup]
    on_pop: NotRequired[OnPop]
    on_start: NotRequired[OnEnd]
    on_end: NotRequired[OnEnd]
    debug: bool
    route_alignment: NotRequired[Callable[["Chunk"], str]]
    auto_xml_ids: NotRequired[bool]
    tei_header: NotRequired[TEIHeaderSpec]
    document: NotRequired[DocumentFactory]


PDFRunTest = Callable[["PDFChunk"], bool | None]
PDFAction = Callable[["PDFChunk"], None]
PDFAppendFunc = Callable[["PDFChunk"], None]
PDFAfterPush = Callable[[], None]


class PDFRule(TypedDict):
    test: PDFRunTest | Literal["_else"]
    action: NotRequired[PDFAction]
    append_func: NotRequired[PDFAppendFunc]
    after_append: NotRequired[PDFAfterPush]


PDFRuleGroup = dict[str, PDFRule | RunImmediate]


class PDFConfig(TypedDict):
    mode: Literal["pdf"]
    rules: PDFRuleGroup
    on_pop: NotRequired[OnPop]
    on_start: NotRequired[OnEnd]
    on_end: NotRequired[OnEnd]
    debug: bool
    auto_xml_ids: NotRequired[bool]
    tei_header: NotRequired[TEIHeaderSpec]
    document: NotRequired[DocumentFactory]


Rule = WordRule | PDFRule
RuleGroup = WordRuleGroup | PDFRuleGroup
Action = Callable[["Chunk"], None]


class WordCosmeticAnnotation(TypedDict):
    test: WordRunTest
    tag: ET.Element[str]
    append_func: NotRequired[WordAppendFunc]


WordCosmeticAnnotations = dict[str, WordCosmeticAnnotation]


class PDFCosmeticAnnotation(TypedDict):
    test: PDFRunTest
    tag: ET.Element[str]
    append_func: NotRequired[PDFAppendFunc]


PDFCosmeticAnnotations = dict[str, PDFCosmeticAnnotation]

CosmeticAnnotations = WordCosmeticAnnotations | PDFCosmeticAnnotations
