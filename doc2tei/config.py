from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Rule:
    """Named rule specification accepted alongside the legacy rule dictionaries."""

    name: str
    test: Callable[[Any], bool] | str
    action: Callable[[Any], Any] | None = None
    append_func: Callable[[Any], Any] | None = None
    after_append: Callable[[], Any] | None = None
    alignment: Any | None = None

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"test": self.test}
        for key in ("action", "append_func", "after_append", "alignment"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


def rule(
    name: str,
    test: Callable[[Any], bool] | str,
    *,
    action: Callable[[Any], Any] | None = None,
    append: Callable[[Any], Any] | None = None,
    after: Callable[[], Any] | None = None,
    alignment: Any | None = None,
) -> Rule:
    return Rule(
        name=name,
        test=test,
        action=action,
        append_func=append,
        after_append=after,
        alignment=alignment,
    )


def rule_group(*items: Rule, run_immediate: Callable[[], Any] | None = None):
    """Build an ordered rule group while retaining readable rule names."""
    group: OrderedDict[str, Any] = OrderedDict()
    if run_immediate is not None:
        group["run_immediate"] = run_immediate
    for item in items:
        if item.name in group:
            raise ValueError(f"duplicate rule name: {item.name}")
        group[item.name] = item
    return group
