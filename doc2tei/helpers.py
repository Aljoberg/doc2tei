from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable, Protocol
import xml.etree.ElementTree as ET

from engine import StackEntry, push

SpeakerIdentifier = Callable[[str], str]
TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"


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


def build_list_person(mapping: Mapping[str, str]) -> ET.Element[str]:
    """Build a deterministic TEI ``listPerson`` from exported speaker labels.

    This intentionally performs no network identity matching. The older
    ``make_list_person.py`` command remains available when Wikidata enrichment
    is wanted; this builder is suitable for producing a minimal list in the
    same parse invocation.
    """
    list_person = ET.Element("listPerson", xmlns=TEI_NAMESPACE)
    for reference, label in mapping.items():
        xml_id = reference.removeprefix("#")
        if (
            not xml_id
            or not (xml_id[0].isalpha() or xml_id[0] == "_")
            or any(not (character.isalnum() or character in "_.-") for character in xml_id)
        ):
            raise ValueError(f"speaker reference is not a valid xml:id: {reference!r}")
        person = ET.SubElement(list_person, "person", {"xml:id": xml_id})
        pers_name = ET.SubElement(person, "persName")
        pers_name.text = label.split(":", 1)[0].strip() or xml_id
    return list_person
