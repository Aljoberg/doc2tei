from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol
import xml.etree.ElementTree as ET

from engine import PDFChunk, StackEntry, append, pop_to, push, tag_is_on_top

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


@dataclass
class FootnoteLinker:
    """Recognize and link run-level PDF footnote references and definitions.

    A config supplies its document-adaptive signals; this helper owns the
    state needed to reconstruct split numeric markers, allocate valid IDs,
    keep wrapped definitions open, and resume the surrounding TEI block.
    """

    body_size: Callable[[], float]
    mode: Callable[[], str | None]
    structural_page: Callable[[PDFChunk], bool]
    utterance_context: Callable[[], bool]
    y_min: float = 0.065
    y_max: float = 0.20
    footnote_page: int | None = field(default=None, init=False)
    definition_ids: dict[int, str] = field(default_factory=dict, init=False)
    used_definition_ids: set[str] = field(default_factory=set, init=False)
    consumed_runs: dict[int, Literal["append", "skip"]] = field(
        default_factory=dict, init=False
    )

    def reset(self) -> None:
        self.footnote_page = None
        self.definition_ids.clear()
        self.used_definition_ids.clear()
        self.consumed_runs.clear()

    def is_small_numeric_run(self, chunk: PDFChunk) -> bool:
        return (
            chunk.font_size <= self.body_size() - 2.0 and chunk.text.strip().isdigit()
        )

    def numeric_run_group(self, chunk: PDFChunk) -> list[PDFChunk]:
        """Join adjacent small runs when extraction splits ``15`` into 1 + 5."""
        runs = chunk.line_chunk.runs
        index = next(index for index, run in enumerate(runs) if run is chunk)
        start = index
        while start > 0 and self.is_small_numeric_run(runs[start - 1]):
            start -= 1
        end = index + 1
        while end < len(runs) and self.is_small_numeric_run(runs[end]):
            end += 1
        return runs[start:end]

    def number(self, chunk: PDFChunk) -> str:
        return "".join(run.text.strip() for run in self.numeric_run_group(chunk))

    def is_first_numeric_run(self, chunk: PDFChunk) -> bool:
        return self.numeric_run_group(chunk)[0] is chunk

    def _consume_later_runs(
        self, chunk: PDFChunk, mode: Literal["append", "skip"]
    ) -> None:
        for run in self.numeric_run_group(chunk)[1:]:
            self.consumed_runs[id(run)] = mode

    def is_consumed_run(self, chunk: PDFChunk) -> bool:
        return id(chunk) in self.consumed_runs

    def consumed_run_action(self, chunk: PDFChunk) -> None:
        mode = self.consumed_runs.pop(id(chunk))
        if mode == "append":
            append(chunk, should_annotate=[])

    def definition_id(self, chunk: PDFChunk) -> str:
        """Allocate a valid, idempotent ID for one definition marker run."""
        key = id(chunk)
        if key in self.definition_ids:
            return self.definition_ids[key]

        number = self.number(chunk)
        candidate = f"note{number}"
        if candidate in self.used_definition_ids:
            candidate = f"{candidate}-p{chunk.page_num + 1}"
            suffix = 2
            while candidate in self.used_definition_ids:
                candidate = f"note{number}-p{chunk.page_num + 1}-{suffix}"
                suffix += 1
        self.definition_ids[key] = candidate
        self.used_definition_ids.add(candidate)
        return candidate

    def target_id(self, chunk: PDFChunk) -> str:
        """References use the document-level number, e.g. ``note15``."""
        return f"note{self.number(chunk)}"

    def is_inline_reference(self, chunk: PDFChunk) -> bool:
        return (
            self.mode() != "ocr"
            and not chunk.is_line_start
            and self.is_small_numeric_run(chunk)
            and self.is_first_numeric_run(chunk)
        )

    def inline_reference_action(self, chunk: PDFChunk) -> None:
        self._consume_later_runs(chunk, "append")
        push(
            "ref",
            cosmetic=True,
            type="footnote",
            target=f"#{self.target_id(chunk)}",
        )

    def is_entry(self, chunk: PDFChunk) -> bool:
        context = chunk.page_context
        if (
            self.mode() == "ocr"
            or not self.structural_page(chunk)
            or context is None
            or not self.is_small_numeric_run(chunk)
            or not self.is_first_numeric_run(chunk)
        ):
            return False
        relative_y = chunk.y / context.height
        if relative_y > self.y_max or (relative_y < self.y_min and chunk.is_line_start):
            return False
        runs = chunk.line_chunk.runs
        group = self.numeric_run_group(chunk)
        last_index = next(index for index, run in enumerate(runs) if run is group[-1])
        return any(
            run.font_size <= self.body_size() - 1.0
            and any(character.isalpha() for character in run.text)
            for run in runs[last_index + 1 :]
        )

    def entry_action(self, chunk: PDFChunk) -> None:
        number = self.number(chunk)
        self._consume_later_runs(chunk, "skip")
        self.footnote_page = chunk.page_num
        pop_to("u", "div")
        push(
            "note",
            attribs={"xml:id": self.definition_id(chunk)},
            place="foot",
            n=number,
        )

    def _is_open_footnote_line(self, chunk: PDFChunk) -> bool:
        return bool(
            chunk.is_line_start
            and chunk.page_context is not None
            and tag_is_on_top("note", place="foot")
        )

    def is_continuation(self, chunk: PDFChunk) -> bool:
        if not self._is_open_footnote_line(chunk):
            return False
        assert chunk.page_context is not None
        return (
            self.footnote_page == chunk.page_num
            and chunk.y / chunk.page_context.height <= self.y_max
        )

    def is_after(self, chunk: PDFChunk) -> bool:
        return self._is_open_footnote_line(chunk) and not self.is_continuation(chunk)

    def after_action(self, _chunk: PDFChunk) -> None:
        pop_to("note", invert=True)
        self.footnote_page = None
        if self.utterance_context():
            pop_to("u", "div")
            push("seg")
        else:
            pop_to("div")
            push("p")


def build_list_person(mapping: Mapping[str, str]) -> ET.Element:
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
            or any(
                not (character.isalnum() or character in "_.-") for character in xml_id
            )
        ):
            raise ValueError(f"speaker reference is not a valid xml:id: {reference!r}")
        person = ET.SubElement(list_person, "person", {"xml:id": xml_id})
        pers_name = ET.SubElement(person, "persName")
        pers_name.text = label.split(":", 1)[0].strip() or xml_id
    return list_person
