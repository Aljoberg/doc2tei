from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from typing import Callable, Literal
import unicodedata
import xml.etree.ElementTree as ET

from engine import (
    PDFChunk,
    StackEntry,
    append,
    pop_to,
    push,
    sanitize_xml_id,
    tag_is_on_top,
    xml_safe_text,
)
from type_decs import (
    ResultWithData,
    SpeakerIdentifier,
    WikidataBinding,
    WikidataFetcher,
)

TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = "doc2tei/1.0 (https://github.com/Aljoberg/doc2tei)"

WIKIDATA_PERSON_QUERY = """
SELECT DISTINCT ?p ?pLabel ?givenLabel ?familyLabel ?birth ?death
       ?bplaceLabel ?dplaceLabel ?sexLabel ?occLabel ?party ?partyLabel
       ?citizenLabel ?viaf ?gnd ?isni WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:api "EntitySearch" .
    bd:serviceParam wikibase:endpoint "www.wikidata.org" .
    bd:serviceParam wikibase:limit "5" .
    bd:serviceParam mwapi:search "%s" .
    bd:serviceParam mwapi:language "sl" .
    ?p wikibase:apiOutputItem mwapi:item .
  }
  ?p wdt:P31 wd:Q5 .
  OPTIONAL { ?p wdt:P569 ?birth. }   OPTIONAL { ?p wdt:P570 ?death. }
  OPTIONAL { ?p wdt:P19 ?bplace. }   OPTIONAL { ?p wdt:P20 ?dplace. }
  OPTIONAL { ?p wdt:P735 ?given. }   OPTIONAL { ?p wdt:P734 ?family. }
  OPTIONAL { ?p wdt:P21 ?sex. }      OPTIONAL { ?p wdt:P106 ?occ. }
  OPTIONAL { ?p wdt:P102 ?party. }   OPTIONAL { ?p wdt:P27 ?citizen. }
  OPTIONAL { ?p wdt:P214 ?viaf. }    OPTIONAL { ?p wdt:P227 ?gnd. }
  OPTIONAL { ?p wdt:P213 ?isni. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "sl,en". }
}
LIMIT 100
"""

AFFILIATION_RE = re.compile(r"\((?P<organization>[^()]*)\)\s*$")
ROLE_RE = re.compile(r",\s*(?P<role>.+?)\s*$")
ROLE_PREFIX_RE = re.compile(
    r"^(?P<role>(?:pod)?predsednik(?:\s+vlade)?|predsedujo[čcć])\s+",
    re.IGNORECASE,
)
ROLE_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"(?:pod)?predsedni\w*|predsedujo\w*|član\w*|clan\w*|"
    r"sekretar\w*|minist\w*|poročeval\w*|poroceval\w*|"
    r"delegat\w*|poslan\w*|predstavni\w*|guverner\w*|direktor\w*"
    r")\b",
    re.IGNORECASE,
)
TITLE_PREFIX_RE = re.compile(
    r"^(?:(?:dr|mag|prof|inž|inz|ing)\.?\s+)+",
    re.IGNORECASE,
)


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
    recoveries: list[str] = field(default_factory=list)

    def reset(self, _result: ResultWithData | None = None) -> None:
        self.mapping.clear()
        self.recoveries.clear()

    def __call__(self, popped: StackEntry) -> None:
        if (
            popped.element.tag != "note"
            or popped.element.attrib.get("type") != self.note_type
        ):
            return
        text = "".join(popped.element.itertext()).strip()
        try:
            raw_identifier = self.identifier(text)
        except Exception as error:
            raw_identifier = text
            self.recoveries.append(
                f"speaker identifier failed for {text!r}: "
                f"{type(error).__name__}: {error}"
            )
        identifier = "#" + sanitize_xml_id(
            str(raw_identifier).removeprefix("#"), prefix="speaker"
        )
        self.mapping.setdefault(identifier, text)
        push(self.utterance_tag, **{self.who_attribute: identifier})

    def export(self, result: ResultWithData) -> None:
        result.data[self.data_key] = dict(self.mapping)
        if self.recoveries:
            recoveries = result.data.setdefault("recoveries", [])
            if isinstance(recoveries, list):
                recoveries.extend(self.recoveries)


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
    y_min: float = 0.02
    y_max: float = 0.25
    footnote_page: int | None = field(default=None, init=False)
    definition_ids: dict[int, str] = field(default_factory=dict, init=False)
    used_definition_ids: set[str] = field(default_factory=set, init=False)
    consumed_runs: dict[int, Literal["append", "skip"]] = field(
        default_factory=dict, init=False
    )
    definitions: dict[tuple[int, str], str] = field(default_factory=dict, init=False)
    references: dict[tuple[int, str], list[ET.Element]] = field(
        default_factory=dict, init=False
    )
    unresolved_count: int = field(default=0, init=False)
    # one-entry memo: the sub-tests of a single rule evaluation ask for the
    # same chunk's group several times, and each rebuild scans the whole line
    _last_group: "tuple[PDFChunk, list[PDFChunk]] | None" = field(
        default=None, init=False, repr=False
    )

    def reset(self) -> None:
        self.footnote_page = None
        self.definition_ids.clear()
        self.used_definition_ids.clear()
        self.consumed_runs.clear()
        self.definitions.clear()
        self.references.clear()
        self.unresolved_count = 0
        self._last_group = None

    def is_small_numeric_run(self, chunk: PDFChunk) -> bool:
        return (
            chunk.font_size <= self.body_size() - 2.0 and chunk.text.strip().isdigit()
        )

    def numeric_run_group(self, chunk: PDFChunk) -> list[PDFChunk]:
        """Join adjacent small runs when extraction splits ``15`` into 1 + 5."""
        cached = self._last_group
        if cached is not None and cached[0] is chunk:
            return cached[1]
        group = self._numeric_run_group(chunk)
        self._last_group = (chunk, group)
        return group

    def _numeric_run_group(self, chunk: PDFChunk) -> list[PDFChunk]:
        runs = chunk.line_chunk.runs
        index = next((index for index, run in enumerate(runs) if run is chunk), None)
        if index is None:
            return [chunk]
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
        """Return the known or best provisional page-local definition ID."""
        number = self.number(chunk)
        known = self.definitions.get((chunk.page_num, number))
        if known is not None:
            return known
        candidate = f"note{number}"
        if candidate in self.used_definition_ids:
            candidate = f"{candidate}-p{chunk.page_num + 1}"
        return candidate

    def _is_raised(self, chunk: PDFChunk) -> bool:
        """Require superscript geometry, not merely a smaller digit glyph."""
        group = self.numeric_run_group(chunk)
        runs = chunk.line_chunk.runs
        group_ids = {id(run) for run in group}
        indexes = [index for index, run in enumerate(runs) if id(run) in group_ids]
        if not indexes:
            return False
        start, end = min(indexes), max(indexes)
        neighbors = [
            run
            for run in runs[max(0, start - 2) : end + 3]
            if id(run) not in group_ids
            and abs(run.font_size - self.body_size()) <= 1.6
            and any(character.isalpha() for character in run.text)
        ]
        if not neighbors:
            return False
        baseline = max(neighbors, key=lambda run: len(run.text))
        minimum_rise = max(0.75, (baseline.font_size - chunk.font_size) * 0.20)
        return chunk.y - baseline.y >= minimum_rise

    def _looks_like_percentage(self, chunk: PDFChunk) -> bool:
        group = self.numeric_run_group(chunk)
        runs = chunk.line_chunk.runs
        group_ids = {id(run) for run in group}
        indexes = [index for index, run in enumerate(runs) if id(run) in group_ids]
        if not indexes:
            return False
        before = "".join(run.text for run in runs[: min(indexes)]).rstrip()
        after = "".join(run.text for run in runs[max(indexes) + 1 :]).lstrip()
        return before.endswith(("/", "%")) or after.startswith(("/", "%"))

    def is_inline_reference(self, chunk: PDFChunk) -> bool:
        return (
            not chunk.is_line_start
            and self.is_small_numeric_run(chunk)
            and self.is_first_numeric_run(chunk)
            and self.number(chunk).lstrip("0").isdigit()
            and not self._looks_like_percentage(chunk)
            and self._is_raised(chunk)
        )

    def inline_reference_action(self, chunk: PDFChunk) -> None:
        self._consume_later_runs(chunk, "append")
        reference = push(
            "ref",
            cosmetic=True,
            type="footnote",
            target=f"#{self.target_id(chunk)}",
        )
        key = (chunk.page_num, self.number(chunk))
        self.references.setdefault(key, []).append(reference)

    def is_entry(self, chunk: PDFChunk) -> bool:
        context = chunk.page_context
        if (
            not self.structural_page(chunk)
            or context is None
            or not self.is_small_numeric_run(chunk)
            or not self.is_first_numeric_run(chunk)
        ):
            return False
        relative_y = chunk.y / context.height
        if relative_y > self.y_max or relative_y < self.y_min:
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
        definition_id = self.definition_id(chunk)
        key = (chunk.page_num, number)
        self.definitions.setdefault(key, definition_id)
        for reference in self.references.get(key, []):
            reference.set("target", f"#{self.definitions[key]}")
        push(
            "note",
            attribs={"xml:id": definition_id},
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

    def finalize(self) -> None:
        """Link proven footnotes; downgrade unmatched markers to typography."""
        for key, references in self.references.items():
            definition = self.definitions.get(key)
            for reference in references:
                if definition is None:
                    # A raised digit can also be a unit exponent or OCR noise.
                    # Without a page-local definition, retain the exact glyph
                    # and its visual role but do not make a semantic claim or
                    # leave a dangling pointer.
                    reference.tag = "hi"
                    reference.attrib.clear()
                    reference.set("rend", "superscript")
                    self.unresolved_count += 1
                else:
                    reference.set("target", f"#{definition}")


@dataclass(frozen=True)
class _SpeakerDetails:
    forename: str
    surname: str
    role: str | None
    organization: str | None

    @property
    def lookup_name(self) -> str:
        return " ".join(part for part in (self.forename, self.surname) if part)


def _sparql_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _fetch_wikidata_bindings(
    search_name: str, *, timeout: float = 20.0
) -> list[WikidataBinding]:
    """Best-effort Wikidata lookup which can never abort document output."""

    try:
        import requests

        response = requests.get(
            WIKIDATA_ENDPOINT,
            params={"query": WIKIDATA_PERSON_QUERY % _sparql_escape(search_name)},
            headers={
                "User-Agent": WIKIDATA_USER_AGENT,
                "Accept": "application/sparql-results+json",
            },
            timeout=(5.0, timeout),
        )
        response.raise_for_status()
        payload = response.json()
        bindings = payload.get("results", {}).get("bindings", [])
        if not isinstance(bindings, list):
            return []
        return [binding for binding in bindings if isinstance(binding, dict)]
    except Exception:
        # Enrichment is optional. Network failures, throttling, malformed JSON,
        # and unexpected service responses must leave a valid local listPerson.
        return []


def _identifier_words(reference: str) -> list[str]:
    identifier = reference.removeprefix("#").strip()
    spaced = "".join(
        f" {character}" if index and character.isupper() else character
        for index, character in enumerate(identifier)
    )
    return [word for word in spaced.split() if word]


def _speaker_details(reference: str, label: str) -> _SpeakerDetails:
    text = xml_safe_text(label).strip().rstrip(":").strip()
    organization = None
    affiliation_match = AFFILIATION_RE.search(text)
    if affiliation_match is not None:
        organization = affiliation_match.group("organization").strip() or None
        text = text[: affiliation_match.start()].rstrip()

    role = None
    prefix_match = ROLE_PREFIX_RE.match(text)
    if prefix_match is not None:
        role = prefix_match.group("role").strip()
        text = text[prefix_match.end() :].lstrip()
    else:
        role_match = ROLE_RE.search(text)
        if role_match is not None:
            candidate_role = role_match.group("role").strip()
            if ROLE_KEYWORD_RE.search(candidate_role):
                role = candidate_role
                text = text[: role_match.start()].rstrip()
    text = TITLE_PREFIX_RE.sub("", text).strip()

    local_words = text.split()
    words = local_words if 2 <= len(local_words) <= 4 else _identifier_words(reference)
    if not words:
        words = [sanitize_xml_id(reference.removeprefix("#"), prefix="speaker")]
    normalized_words = [word.title() if word.isupper() else word for word in words]
    forename = normalized_words[0]
    surname = " ".join(normalized_words[1:])
    return _SpeakerDetails(
        forename=forename,
        surname=surname,
        role=role,
        organization=organization,
    )


def _normalized_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character) and character.isalnum()
    )


def _binding_value(binding: WikidataBinding, key: str) -> str | None:
    item = binding.get(key)
    if not isinstance(item, Mapping):
        return None
    value = item.get("value")
    return value if isinstance(value, str) and value else None


def _candidate_bindings(
    bindings: list[WikidataBinding], search_name: str
) -> list[WikidataBinding]:
    """Choose one searched person, then retain all rows for that entity."""

    grouped: dict[str, list[WikidataBinding]] = {}
    for binding in bindings:
        uri = _binding_value(binding, "p")
        if uri is not None:
            grouped.setdefault(uri, []).append(binding)
    if not grouped:
        return []

    normalized_search = _normalized_name(search_name)

    def score(rows: list[WikidataBinding]) -> float:
        label = next(
            (
                value
                for row in rows
                if (value := _binding_value(row, "pLabel")) is not None
            ),
            "",
        )
        if not label:
            return 0.0
        return SequenceMatcher(None, normalized_search, _normalized_name(label)).ratio()

    candidate = max(grouped.values(), key=score)
    return candidate if score(candidate) >= 0.65 else []


def _unique_values(bindings: list[WikidataBinding], key: str) -> list[str]:
    values: list[str] = []
    for binding in bindings:
        value = _binding_value(binding, key)
        if value is not None and value not in values:
            values.append(value)
    return values


def _role_value(role: str | None) -> str:
    if not role:
        return "member"
    role_keyword = ROLE_KEYWORD_RE.search(role)
    semantic_word = (
        role_keyword.group(0) if role_keyword is not None else role.split()[0]
    ).casefold()
    return (
        "".join(
            character
            for character in semantic_word
            if character.isalnum() or character in ".-"
        )
        or "member"
    )


def _append_affiliation(
    person: ET.Element,
    *,
    role: str | None = None,
    organization: str | None = None,
    organization_ref: str | None = None,
) -> None:
    if not role and not organization:
        return
    affiliation = ET.SubElement(person, "affiliation", role=_role_value(role))
    if role:
        ET.SubElement(affiliation, "roleName", {"xml:lang": "sl"}).text = xml_safe_text(
            role
        )
    if organization:
        attributes = {"ref": organization_ref} if organization_ref else {}
        ET.SubElement(affiliation, "orgName", attributes).text = xml_safe_text(
            organization
        )


def _append_wikidata(
    person: ET.Element,
    pers_name: ET.Element,
    details: _SpeakerDetails,
    bindings: list[WikidataBinding],
) -> None:
    candidate = _candidate_bindings(bindings, details.lookup_name)
    if not candidate:
        return

    given_names = _unique_values(candidate, "givenLabel")
    family_names = _unique_values(candidate, "familyLabel")
    surname = ET.SubElement(pers_name, "surname")
    surname.text = xml_safe_text(family_names[0] if family_names else details.surname)
    forename = ET.SubElement(pers_name, "forename")
    forename.text = xml_safe_text(given_names[0] if given_names else details.forename)

    uri = _binding_value(candidate[0], "p")
    if uri:
        ET.SubElement(person, "idno", type="URI", subtype="wikidata").text = uri
    for key, identifier_type in (("viaf", "VIAF"), ("gnd", "GND"), ("isni", "ISNI")):
        for value in _unique_values(candidate, key):
            ET.SubElement(
                person, "idno", type=identifier_type, subtype="wikidata"
            ).text = value

    for element_name, date_key, place_key in (
        ("birth", "birth", "bplaceLabel"),
        ("death", "death", "dplaceLabel"),
    ):
        dates = _unique_values(candidate, date_key)
        places = _unique_values(candidate, place_key)
        if dates or places:
            attributes = {"when": dates[0].partition("T")[0]} if dates else {}
            event = ET.SubElement(person, element_name, attributes)
            if places:
                ET.SubElement(event, "placeName").text = xml_safe_text(places[0])

    sexes = _unique_values(candidate, "sexLabel")
    if sexes:
        normalized_sex = _normalized_name(sexes[0])
        sex_value = (
            "M"
            if normalized_sex in {"moski", "male"}
            else "F" if normalized_sex in {"zenski", "female"} else "U"
        )
        ET.SubElement(person, "sex", value=sex_value).text = xml_safe_text(sexes[0])
    for occupation in _unique_values(candidate, "occLabel"):
        ET.SubElement(person, "occupation").text = xml_safe_text(occupation)
    for nationality in _unique_values(candidate, "citizenLabel"):
        ET.SubElement(person, "nationality").text = xml_safe_text(nationality)

    seen_parties: set[tuple[str, str | None]] = set()
    for binding in candidate:
        party = _binding_value(binding, "partyLabel")
        party_ref = _binding_value(binding, "party")
        if party is not None and (party, party_ref) not in seen_parties:
            seen_parties.add((party, party_ref))
            _append_affiliation(
                person,
                organization=party,
                organization_ref=party_ref,
            )


def build_list_person(
    mapping: Mapping[str, str],
    *,
    include_wikidata: bool = False,
    wikidata_fetcher: WikidataFetcher | None = None,
    wikidata_workers: int = 4,
    wikidata_timeout: float = 20.0,
) -> ET.Element:
    """Build a fail-soft TEI ``listPerson`` from exported speaker labels.

    Local names and regex-derived affiliations are deterministic. Wikidata
    enrichment is optional because it performs network requests and may be
    unavailable or throttled; any failed lookup falls back to the local record.
    """

    attributes = {"xmlns": TEI_NAMESPACE}
    if include_wikidata:
        attributes["xml:lang"] = "sl"
    list_person = ET.Element("listPerson", attributes)
    if include_wikidata:
        ET.SubElement(list_person, "head", {"xml:lang": "sl"}).text = (
            "Seznam govornikov"
        )
        ET.SubElement(list_person, "head", {"xml:lang": "en"}).text = "List of speakers"

    if not mapping:
        person = ET.SubElement(
            list_person,
            "person",
            {"xml:id": "UnknownSpeaker", "role": "undetected"},
        )
        ET.SubElement(person, "persName").text = "Unknown speaker"
        ET.SubElement(person, "note", type="conversionNote").text = (
            "No speaker labels were detected in the source document."
        )
        return list_person

    entries = [
        (reference, label, _speaker_details(reference, label))
        for reference, label in mapping.items()
    ]
    lookup_results: dict[str, list[WikidataBinding]] = {}
    if include_wikidata:
        terms = list(
            dict.fromkeys(
                details.lookup_name for _reference, _label, details in entries
            )
        )
        fetcher = wikidata_fetcher
        if fetcher is None:
            fetcher = lambda term: _fetch_wikidata_bindings(
                term, timeout=wikidata_timeout
            )

        if terms:
            worker_count = min(max(1, wikidata_workers), len(terms))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                lookup_results = dict(zip(terms, pool.map(fetcher, terms)))

    used_ids: set[str] = set()
    for reference, label, details in entries:
        base_id = sanitize_xml_id(reference.removeprefix("#"), prefix="speaker")
        xml_id = base_id
        suffix = 2
        while xml_id in used_ids:
            xml_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(xml_id)
        person = ET.SubElement(list_person, "person", {"xml:id": xml_id})
        pers_name = ET.SubElement(person, "persName")
        if include_wikidata:
            bindings = lookup_results.get(details.lookup_name, [])
            _append_wikidata(person, pers_name, details, bindings)
            if not list(pers_name):
                if details.surname:
                    ET.SubElement(pers_name, "surname").text = details.surname
                ET.SubElement(pers_name, "forename").text = details.forename
        else:
            pers_name.text = xml_safe_text(label.split(":", 1)[0].strip() or xml_id)
        _append_affiliation(
            person,
            role=details.role,
            organization=details.organization,
        )
    return list_person
