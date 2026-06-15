from __future__ import annotations

from typing import Callable, TypedDict, Literal, Any, TYPE_CHECKING
import xml.etree.ElementTree as ET
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

if TYPE_CHECKING:
    # only needed for type hints; importing at runtime would create a cycle
    # (engine imports type_decs for StackEntry/Action)
    from engine import WordChunk, PDFChunk


class StackEntry(TypedDict):
    element: ET.Element[str]
    children: list[str | ET.Element[str]]
    last_elem: ET.Element[str] | None


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

# ParaTest = Callable[[float, float, int, int, Paragraph], bool | None]
WordRunTest = Callable[["WordChunk"], bool | None]
WordAction = Callable[["WordChunk"], None]
WordAppendFunc = Callable[["WordChunk"], Any]
WordAfterPush = Callable[[], None]
RunImmediate = Callable[[], None]


class WordRule(TypedDict, total=False):
    alignment: WD_PARAGRAPH_ALIGNMENT
    test_run: WordRunTest | Literal["_else"]
    action: WordAction
    append_func: WordAppendFunc
    after_append: WordAfterPush


WordRuleGroup = dict[str, WordRule | RunImmediate]


class WordConfig(TypedDict):
    mode: Literal["word"]
    alignments: dict[str, WordRuleGroup]


PDFRunTest = Callable[["PDFChunk"], bool | None]
PDFAction = Callable[["PDFChunk"], None]
PDFAppendFunc = Callable[["PDFChunk"], Any]
PDFAfterPush = Callable[[], None]
RunImmediate = Callable[[], None]


class PDFRule(TypedDict, total=False):
    test_run: PDFRunTest | Literal["_else"]
    action: PDFAction
    append_func: PDFAppendFunc
    after_append: PDFAfterPush


PDFRuleGroup = dict[str, PDFRule | RunImmediate]


class PDFConfig(TypedDict):
    mode: Literal["pdf"]
    alignments: dict[str, PDFRuleGroup]


Rule = WordRule | PDFRule
RuleGroup = WordRuleGroup | PDFRuleGroup
Config = WordConfig | PDFConfig
