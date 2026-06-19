from __future__ import annotations

from typing import Callable, NotRequired, TypedDict, Literal, Any, TYPE_CHECKING
import xml.etree.ElementTree as ET
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

if TYPE_CHECKING:
    # only needed for type hints; importing at runtime would create a cycle
    # (engine imports type_decs for StackEntry/Action)
    from engine import WordChunk, PDFChunk, Chunk, StackEntry


WordRunTest = Callable[["WordChunk"], bool | None]
WordAction = Callable[["WordChunk"], Any]
WordAppendFunc = Callable[["WordChunk"], Any]
WordAfterPush = Callable[[], Any]
RunImmediate = Callable[[], Any]
OnPop = Callable[["StackEntry"], Any]
OnEnd = Callable[[], Any]


class WordRule(TypedDict):
    alignment: NotRequired[WD_PARAGRAPH_ALIGNMENT]
    test: WordRunTest | Literal["_else"]
    action: NotRequired[WordAction]
    append_func: NotRequired[WordAppendFunc]
    after_append: NotRequired[WordAfterPush]


WordRuleGroup = dict[str, WordRule | RunImmediate]


class WordConfig(TypedDict):
    mode: Literal["word"]
    alignments: dict[str, WordRuleGroup]
    on_pop: NotRequired[OnPop]
    on_end: NotRequired[OnEnd]
    debug: bool


PDFRunTest = Callable[["PDFChunk"], bool | None]
PDFAction = Callable[["PDFChunk"], Any]
PDFAppendFunc = Callable[["PDFChunk"], Any]
PDFAfterPush = Callable[[], Any]


class PDFRule(TypedDict):
    test: PDFRunTest | Literal["_else"]
    action: NotRequired[PDFAction]
    append_func: NotRequired[PDFAppendFunc]
    after_append: NotRequired[PDFAfterPush]


PDFRuleGroup = dict[str, PDFRule | RunImmediate]


class PDFConfig(TypedDict):
    mode: Literal["pdf"]
    alignments: dict[str, PDFRuleGroup]
    on_pop: NotRequired[OnPop]
    on_end: NotRequired[OnEnd]
    debug: bool


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
