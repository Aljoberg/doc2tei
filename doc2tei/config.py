from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Generic, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    # annotation-only: keeps python-docx out of PDF-only process startup
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

from type_decs import ChunkT


@dataclass(frozen=True)
class Rule(Generic[ChunkT]):
    """Named rule specification accepted alongside the legacy rule dictionaries."""

    name: str
    test: Callable[[ChunkT], bool] | Literal["_else"]
    action: Callable[[ChunkT], None] | None = None
    append_func: Callable[[ChunkT], None] | None = None
    after_append: Callable[[], None] | None = None
    alignment: "WD_PARAGRAPH_ALIGNMENT | None" = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"test": self.test}
        for key in ("action", "append_func", "after_append", "alignment"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


def rule(
    name: str,
    test: Callable[[ChunkT], bool] | Literal["_else"],
    *,
    action: Callable[[ChunkT], None] | None = None,
    append: Callable[[ChunkT], None] | None = None,
    after: Callable[[], None] | None = None,
    alignment: "WD_PARAGRAPH_ALIGNMENT | None" = None,
) -> Rule[ChunkT]:
    return Rule(
        name=name,
        test=test,
        action=action,
        append_func=append,
        after_append=after,
        alignment=alignment,
    )


def rule_group(
    *items: Rule[ChunkT], run_immediate: Callable[[], None] | None = None
) -> OrderedDict[str, object]:
    """Build an ordered rule group while retaining readable rule names."""
    group: OrderedDict[str, object] = OrderedDict()
    if run_immediate is not None:
        group["run_immediate"] = run_immediate
    for item in items:
        if item.name in group:
            raise ValueError(f"duplicate rule name: {item.name}")
        group[item.name] = item
    return group
