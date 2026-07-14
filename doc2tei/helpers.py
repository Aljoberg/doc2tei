from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from engine import StackEntry, push

SpeakerIdentifier = Callable[[str], str]


class ResultWithData(Protocol):
    data: dict[str, object]


@dataclass
class SpeakerUtteranceHook:
    """Optional configurable bridge from a speaker note to an utterance.

    The core parser knows nothing about debates. A config may install an
    instance as ``on_pop`` and choose the note marker, identifier policy,
    output element, attribute, and exported data key.
    """

    identifier: SpeakerIdentifier
    note_type: str = "speaker"
    utterance_tag: str = "u"
    who_attribute: str = "who"
    data_key: str = "speakers"
    mapping: dict[str, str] = field(default_factory=dict)

    def reset(self, _result: ResultWithData | None = None) -> None:
        self.mapping.clear()

    def __call__(self, popped: StackEntry) -> None:
        if (
            popped.element.tag != "note"
            or popped.element.attrib.get("type") != self.note_type
        ):
            return
        text = "".join(popped.element.itertext()).strip()
        identifier = self.identifier(text)
        self.mapping.setdefault(identifier, text)
        push(self.utterance_tag, **{self.who_attribute: identifier})

    def export(self, result: ResultWithData) -> None:
        result.data[self.data_key] = dict(self.mapping)
