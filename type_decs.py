from typing import Callable, TypedDict, Literal, Any
import xml.etree.ElementTree as ET
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT


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

ParaTest = Callable[[int, int, int, int, Paragraph], bool | None]
RunTest = Callable[[int, int, int, int, Run], bool | None]
Action = Callable[[int, int, int, int, Paragraph], None]
AppendFunc = Callable[[int, int, int, int, Paragraph, int], Any]
AfterPush = Callable[[], None]
RunImmediate = Callable[[], None]


class Rule(TypedDict, total=False):
    alignment: WD_PARAGRAPH_ALIGNMENT  # required paragraph alignment, if set
    test: ParaTest | Literal["_else"]
    test_run: RunTest | Literal["_else"]
    action: Action
    append_func: AppendFunc
    after_push: AfterPush


RuleGroup = dict[str, Rule | RunImmediate]
Config = dict[str, RuleGroup]
