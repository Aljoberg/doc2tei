from __future__ import annotations

from typing import Callable, NotRequired, TypedDict, Literal, Any, TYPE_CHECKING
import xml.etree.ElementTree as ET
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

if TYPE_CHECKING:
    # only needed for type hints; importing at runtime would create a cycle
    # (engine imports type_decs for StackEntry/Action)
    from engine import WordChunk, PDFChunk, Chunk, StackEntry


# i have yet to rewrite the following paragraph
# ai wrote it and i cannot be asked to document the whole fucking config
# + it's subject to change still

# --- rule config types ---
#
# the config is a dict of zones; each zone is a dict of named rules, checked
# IN ORDER (dict order matters, first match wins). a rule either matches whole
# paragraphs ("test") or single runs ("test_run"); the literal "_else" instead
# of a callable makes that rule the fallback for when no other rule in the
# zone matched.
#
# paragraph rules are tried first; if one fires it handles the whole
# paragraph. otherwise every (non-empty) run is classified by the run rules
# on its own.
#
# when a rule fires:
#   1. "action" runs (pops/pushes stack elements; omit for "just append here")
#   2. "append_func" appends the content (default: append the matched run(s))
#   3. "after_push" runs (for updating state like visited_time)
#
# a zone can also have a "run_immediate" callable which runs every time a
# paragraph lands in that zone, before any rule is checked.


WordRunTest = Callable[["WordChunk"], bool | None]
WordAction = Callable[["WordChunk"], Any]
WordAppendFunc = Callable[["WordChunk"], Any]
WordAfterPush = Callable[[], Any]
RunImmediate = Callable[[], Any]
OnPop = Callable[["StackEntry"], Any]


class WordRule(TypedDict, total=False):
    alignment: WD_PARAGRAPH_ALIGNMENT
    test: WordRunTest | Literal["_else"]
    action: WordAction
    append_func: WordAppendFunc
    after_append: WordAfterPush


WordRuleGroup = dict[str, WordRule | RunImmediate]


class WordConfig(TypedDict):
    mode: Literal["word"]
    alignments: dict[str, WordRuleGroup]
    on_pop: NotRequired[OnPop]


PDFRunTest = Callable[["PDFChunk"], bool | None]
PDFAction = Callable[["PDFChunk"], Any]
PDFAppendFunc = Callable[["PDFChunk"], Any]
PDFAfterPush = Callable[[], Any]


class PDFRule(TypedDict, total=False):
    test: PDFRunTest | Literal["_else"]
    action: PDFAction
    append_func: PDFAppendFunc
    after_append: PDFAfterPush


PDFRuleGroup = dict[str, PDFRule | RunImmediate]


class PDFConfig(TypedDict):
    mode: Literal["pdf"]
    alignments: dict[str, PDFRuleGroup]
    on_pop: NotRequired[OnPop]
    # parse_text's running-header skip band (ymin, ymax); None opts out
    header: NotRequired[tuple[float, float]]


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
