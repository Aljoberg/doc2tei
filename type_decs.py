from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import (
    Callable,
    Literal,
    NotRequired,
    Protocol,
    TYPE_CHECKING,
    TypeAlias,
    TypedDict,
    TypeVar,
)
import xml.etree.ElementTree as ET

if TYPE_CHECKING:
    # Runtime imports here would create cycles: these modules all consume the
    # declarations below. Forward references let type checkers resolve the
    # concrete models without coupling the type layer to their implementations.
    # python-docx additionally stays out of PDF-only process startup.
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
    from doc2tei.extractors import LineRecord
    from doc2tei.tei_header import TEIHeader
    from engine import PDFChunk, PDFPageContext, StackEntry, WordChunk


class Chunk(Protocol):
    """Minimum text-chunk interface shared by Word and PDF parsing."""

    x: float
    y: float
    text: str
    bold: bool | None
    italic: bool | None
    # A hyphen, gap, or line break may require a leading output space.
    space_before: bool


ChunkT = TypeVar("ChunkT", bound=Chunk)


class ChunkExtractor(Protocol):
    def __call__(self, filename: str) -> Iterable[Chunk]: ...


class Logger(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> None: ...


class ResultWithData(Protocol):
    data: dict[str, object]


RuleCallback: TypeAlias = Callable[[Chunk], None]
RecoveryHandler: TypeAlias = Callable[[str, BaseException, Chunk | None], None]
SelectorTest: TypeAlias = Callable[[Chunk], bool]
SpeakerIdentifier: TypeAlias = Callable[[str], str]
LocalizedText: TypeAlias = Mapping[str, str]
BatchStatus: TypeAlias = Literal["ok", "recovered", "failed", "skipped"]
ListPersonScope: TypeAlias = Literal["document", "folder", "corpus"]
SIstoryDownloadStatus: TypeAlias = Literal["ok", "partial", "failed"]


WikidataValue = TypedDict(
    "WikidataValue",
    {
        "type": str,
        "value": str,
        "datatype": str,
        "xml:lang": str,
    },
    total=False,
)


WikidataBinding: TypeAlias = dict[str, WikidataValue]
WikidataFetcher: TypeAlias = Callable[[str], list[WikidataBinding]]


class ExtractedWord(TypedDict):
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    fontname: str
    size: float


LineFilter: TypeAlias = Callable[["LineRecord", "PDFPageContext"], bool]
StopTest: TypeAlias = LineFilter
PageEnricher: TypeAlias = Callable[["PDFPageContext", list["LineRecord"]], None]
LineEnricher: TypeAlias = Callable[
    ["PDFPageContext", "LineRecord", int, list["LineRecord"]],
    dict[str, object] | None,
]
LineBreakTest: TypeAlias = Callable[[float, float], bool]
PageErrorHandler: TypeAlias = Callable[[int, Exception], None]


WordRunTest: TypeAlias = Callable[["WordChunk"], bool | None]
WordAction: TypeAlias = Callable[["WordChunk"], None]
WordAppendFunc: TypeAlias = Callable[["WordChunk"], None]
WordAfterPush: TypeAlias = Callable[[], None]
RunImmediate: TypeAlias = Callable[[], None]
OnPop: TypeAlias = Callable[["StackEntry"], None]
OnEnd: TypeAlias = Callable[..., None]


class WordRule(TypedDict):
    alignment: NotRequired[WD_PARAGRAPH_ALIGNMENT]
    test: WordRunTest | Literal["_else"]
    action: NotRequired[WordAction]
    append_func: NotRequired[WordAppendFunc]
    after_append: NotRequired[WordAfterPush]


WordRuleGroup: TypeAlias = dict[str, WordRule | RunImmediate]

# the <TEI> root and the (descendant) element parsed content is appended into
DocumentFactory: TypeAlias = "Callable[[], tuple[ET.Element, ET.Element]]"
# a built <teiHeader>, a TEIHeader spec, or a callable producing either
TEIHeaderSpec: TypeAlias = (
    "TEIHeader | ET.Element | Callable[[], TEIHeader | ET.Element]"
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
    recover_errors: NotRequired[bool]
    tei_header: NotRequired[TEIHeaderSpec]
    document: NotRequired[DocumentFactory]


PDFRunTest: TypeAlias = Callable[["PDFChunk"], bool | None]
PDFAction: TypeAlias = Callable[["PDFChunk"], None]
PDFAppendFunc: TypeAlias = Callable[["PDFChunk"], None]
PDFAfterPush: TypeAlias = Callable[[], None]


class PDFRule(TypedDict):
    test: PDFRunTest | Literal["_else"]
    action: NotRequired[PDFAction]
    append_func: NotRequired[PDFAppendFunc]
    after_append: NotRequired[PDFAfterPush]


PDFRuleGroup: TypeAlias = dict[str, PDFRule | RunImmediate]


class PDFConfig(TypedDict):
    mode: Literal["pdf"]
    rules: PDFRuleGroup
    on_pop: NotRequired[OnPop]
    on_start: NotRequired[OnEnd]
    on_end: NotRequired[OnEnd]
    debug: bool
    auto_xml_ids: NotRequired[bool]
    recover_errors: NotRequired[bool]
    merge_nearby_runs: NotRequired[bool]
    page_workers: NotRequired[int]
    tei_header: NotRequired[TEIHeaderSpec]
    document: NotRequired[DocumentFactory]


Rule: TypeAlias = WordRule | PDFRule
RuleGroup: TypeAlias = WordRuleGroup | PDFRuleGroup
Action: TypeAlias = Callable[[Chunk], None]


class WordCosmeticAnnotation(TypedDict):
    test: WordRunTest
    tag: ET.Element
    append_func: NotRequired[WordAppendFunc]


WordCosmeticAnnotations: TypeAlias = dict[str, WordCosmeticAnnotation]


class PDFCosmeticAnnotation(TypedDict):
    test: PDFRunTest
    tag: ET.Element
    append_func: NotRequired[PDFAppendFunc]


PDFCosmeticAnnotations: TypeAlias = dict[str, PDFCosmeticAnnotation]

CosmeticAnnotations: TypeAlias = WordCosmeticAnnotations | PDFCosmeticAnnotations
