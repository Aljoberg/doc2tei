from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Pattern


Test = Callable[[Any], bool]


def _matches(test: Any, value: Any) -> bool:
    return bool(test(value)) if callable(test) else value == test


@dataclass(frozen=True)
class Between:
    minimum: float | None = None
    maximum: float | None = None
    inclusive: bool = False

    def __call__(self, value: float) -> bool:
        lower = True if self.minimum is None else (
            value >= self.minimum if self.inclusive else value > self.minimum
        )
        upper = True if self.maximum is None else (
            value <= self.maximum if self.inclusive else value < self.maximum
        )
        return lower and upper


@dataclass(frozen=True)
class Attribute:
    name: str
    test: Any

    def __call__(self, chunk: Any) -> bool:
        return _matches(self.test, getattr(chunk, self.name))


@dataclass(frozen=True)
class Text:
    pattern: str | Pattern[str] | None = None
    equals: str | None = None
    starts_with: str | None = None
    ends_with: str | None = None
    source: str = "chunk"
    flags: int = 0
    normalize_space: bool = False

    def __call__(self, chunk: Any) -> bool:
        text = chunk.text if self.source == "chunk" else chunk.line_chunk.text
        if self.normalize_space:
            text = re.sub(r"\s+", " ", text).strip()
        if self.equals is not None and text != self.equals:
            return False
        if self.starts_with is not None and not text.startswith(self.starts_with):
            return False
        if self.ends_with is not None and not text.endswith(self.ends_with):
            return False
        if self.pattern is not None and re.search(self.pattern, text, self.flags) is None:
            return False
        return True


class LineStart:
    def __call__(self, chunk: Any) -> bool:
        if hasattr(chunk, "is_line_start"):
            return bool(chunk.is_line_start)
        return chunk is chunk.line_chunk.runs[0]


@dataclass(frozen=True)
class Metadata:
    key: str
    test: Any = True
    source: str = "chunk"
    default: Any = None

    def __call__(self, chunk: Any) -> bool:
        if self.source == "page":
            context = getattr(chunk, "page_context", None)
            metadata = context.metadata if context is not None else {}
        elif self.source == "line":
            metadata = getattr(chunk.line_chunk, "metadata", {})
        else:
            metadata = getattr(chunk, "metadata", {})
        return _matches(self.test, metadata.get(self.key, self.default))


@dataclass(frozen=True)
class AllOf:
    tests: tuple[Test, ...]

    def __init__(self, *tests: Test):
        object.__setattr__(self, "tests", tests)

    def __call__(self, chunk: Any) -> bool:
        return all(test(chunk) for test in self.tests)


@dataclass(frozen=True)
class AnyOf:
    tests: tuple[Test, ...]

    def __init__(self, *tests: Test):
        object.__setattr__(self, "tests", tests)

    def __call__(self, chunk: Any) -> bool:
        return any(test(chunk) for test in self.tests)


@dataclass(frozen=True)
class Not:
    test: Test

    def __call__(self, chunk: Any) -> bool:
        return not self.test(chunk)
