"""Builders for a ParlaMint-style ``<teiHeader>``.

Modelled on the ParlaMint sample headers
(https://github.com/clarin-eric/ParlaMint, ``Samples/ParlaMint-SI``).

``TEIHeader`` is a plain dataclass whose fields all default to empty, so a
bare ``TEIHeader().build()`` yields the complete required skeleton with blank
values, ready to be filled or post-processed. Singleton elements (titles,
edition, publisher, availability, source, setting, ...) are always emitted;
repeatable groups (``meeting``, ``respStmt``, ``funder``, ``measure``,
``change``) appear only when provided.

``build()`` returns a regular ``ET.Element``, so a config that needs
something the dataclass does not cover can mutate the result - or skip the
dataclass and export its own ``<teiHeader>`` element as ``CONFIG["tei_header"]``.

``<tagsDecl>`` and ``<extent>`` are emitted empty on purpose: their contents
(tag usage counts, speech/word counts) are only known after parsing, and the
parser fills them via :func:`fill_counts` when they are left empty.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re
import xml.etree.ElementTree as ET

from type_decs import LocalizedText


def _localized(
    parent: ET.Element, tag: str, texts: LocalizedText, **attrs: str
) -> None:
    if not texts:
        ET.SubElement(parent, tag, attrs).text = ""
        return
    for lang, text in texts.items():
        elem = ET.SubElement(parent, tag, attrs)
        if lang:
            elem.set("xml:lang", lang)
        elem.text = text


def _set_if(elem: ET.Element, **attrs: str) -> None:
    for key, value in attrs.items():
        if value:
            elem.set(key, value)


@dataclass
class Person:
    name: str = ""
    ref: str = ""  # e.g. an ORCID URL


@dataclass
class RespStmt:
    persons: list[Person] = field(default_factory=list)
    resp: LocalizedText = field(default_factory=dict)


@dataclass
class Funder:
    org_names: LocalizedText = field(default_factory=dict)
    ref_target: str = ""
    ref_text: str = ""


@dataclass
class Meeting:
    text: str = ""
    n: str = ""
    corresp: str = ""
    ana: str = ""


@dataclass
class Measure:
    unit: str = ""
    quantity: str = ""
    texts: LocalizedText = field(default_factory=dict)


@dataclass
class SourceBibl:
    titles: LocalizedText = field(default_factory=dict)
    editions: LocalizedText = field(default_factory=dict)
    idno: str = ""
    idno_subtype: str = "parliament"
    date_when: str = ""
    date_text: str = ""


@dataclass
class Setting:
    city: str = ""
    country_key: str = ""
    country_name: str = ""
    date_when: str = ""
    date_text: str = ""
    date_ana: str = ""


@dataclass
class Change:
    when: str = ""
    name: str = ""
    note: str = ""


@dataclass
class TEIHeader:
    # attributes for the <TEI> root element itself
    tei_id: str = ""
    language: str = ""
    ana: str = ""
    # fileDesc / titleStmt
    main_titles: LocalizedText = field(default_factory=dict)
    sub_titles: LocalizedText = field(default_factory=dict)
    meetings: list[Meeting] = field(default_factory=list)
    resp_stmts: list[RespStmt] = field(default_factory=list)
    funders: list[Funder] = field(default_factory=list)
    # fileDesc / editionStmt + extent
    edition: str = ""
    measures: list[Measure] = field(default_factory=list)
    # fileDesc / publicationStmt
    publisher_org_names: LocalizedText = field(default_factory=dict)
    publisher_ref: str = ""
    idno: str = ""  # handle URI
    availability_status: str = "free"
    licence: str = ""
    availability_paragraphs: LocalizedText = field(default_factory=dict)
    publication_date: str = ""
    # fileDesc / sourceDesc
    source: SourceBibl = field(default_factory=SourceBibl)
    # encodingDesc
    project_desc: LocalizedText = field(default_factory=dict)
    # profileDesc / settingDesc
    setting: Setting = field(default_factory=Setting)
    # revisionDesc
    changes: list[Change] = field(default_factory=list)

    def tei_attributes(self) -> dict[str, str]:
        """Attributes the parser applies to the ``<TEI>`` root."""
        attrs: dict[str, str] = {}
        if self.tei_id:
            attrs["xml:id"] = self.tei_id
        if self.language:
            attrs["xml:lang"] = self.language
        if self.ana:
            attrs["ana"] = self.ana
        return attrs

    def build(self) -> ET.Element:
        header = ET.Element("teiHeader")

        file_desc = ET.SubElement(header, "fileDesc")
        title_stmt = ET.SubElement(file_desc, "titleStmt")
        _localized(title_stmt, "title", self.main_titles, type="main")
        _localized(title_stmt, "title", self.sub_titles, type="sub")
        for meeting in self.meetings:
            elem = ET.SubElement(title_stmt, "meeting")
            _set_if(elem, n=meeting.n, corresp=meeting.corresp, ana=meeting.ana)
            elem.text = meeting.text
        for stmt in self.resp_stmts:
            resp_elem = ET.SubElement(title_stmt, "respStmt")
            for person in stmt.persons:
                pers = ET.SubElement(resp_elem, "persName")
                _set_if(pers, ref=person.ref)
                pers.text = person.name
            _localized(resp_elem, "resp", stmt.resp)
        for funder in self.funders:
            funder_elem = ET.SubElement(title_stmt, "funder")
            _localized(funder_elem, "orgName", funder.org_names)
            if funder.ref_target or funder.ref_text:
                ref = ET.SubElement(funder_elem, "ref")
                _set_if(ref, target=funder.ref_target)
                ref.text = funder.ref_text or funder.ref_target

        edition_stmt = ET.SubElement(file_desc, "editionStmt")
        ET.SubElement(edition_stmt, "edition").text = self.edition

        extent = ET.SubElement(file_desc, "extent")
        for measure in self.measures:
            _localized(
                extent,
                "measure",
                measure.texts,
                unit=measure.unit,
                quantity=measure.quantity,
            )

        publication = ET.SubElement(file_desc, "publicationStmt")
        publisher = ET.SubElement(publication, "publisher")
        _localized(publisher, "orgName", self.publisher_org_names)
        if self.publisher_ref:
            ref = ET.SubElement(publisher, "ref", target=self.publisher_ref)
            ref.text = self.publisher_ref
        idno = ET.SubElement(publication, "idno", type="URI", subtype="handle")
        idno.text = self.idno
        availability = ET.SubElement(
            publication, "availability", status=self.availability_status
        )
        ET.SubElement(availability, "licence").text = self.licence
        _localized(availability, "p", self.availability_paragraphs)
        date = ET.SubElement(publication, "date")
        _set_if(date, when=self.publication_date)
        date.text = self.publication_date

        source_desc = ET.SubElement(file_desc, "sourceDesc")
        bibl = ET.SubElement(source_desc, "bibl")
        _localized(bibl, "title", self.source.titles, type="main")
        _localized(bibl, "edition", self.source.editions)
        source_idno = ET.SubElement(bibl, "idno", type="URI")
        _set_if(source_idno, subtype=self.source.idno_subtype)
        source_idno.text = self.source.idno
        source_date = ET.SubElement(bibl, "date")
        _set_if(source_date, when=self.source.date_when)
        source_date.text = self.source.date_text or self.source.date_when

        encoding_desc = ET.SubElement(header, "encodingDesc")
        project_desc = ET.SubElement(encoding_desc, "projectDesc")
        _localized(project_desc, "p", self.project_desc)
        ET.SubElement(encoding_desc, "tagsDecl")  # filled by fill_counts

        profile_desc = ET.SubElement(header, "profileDesc")
        setting_desc = ET.SubElement(profile_desc, "settingDesc")
        setting = ET.SubElement(setting_desc, "setting")
        city = ET.SubElement(setting, "name", type="city")
        city.text = self.setting.city
        country = ET.SubElement(setting, "name", type="country")
        _set_if(country, key=self.setting.country_key)
        country.text = self.setting.country_name
        setting_date = ET.SubElement(setting, "date")
        _set_if(setting_date, when=self.setting.date_when, ana=self.setting.date_ana)
        setting_date.text = self.setting.date_text or self.setting.date_when

        revision_desc = ET.SubElement(header, "revisionDesc")
        for change in self.changes:
            change_elem = ET.SubElement(revision_desc, "change")
            _set_if(change_elem, when=change.when)
            name = ET.SubElement(change_elem, "name")
            name.text = change.name
            name.tail = f": {change.note}" if change.note else ""

        return header


TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"


def fill_counts(root: ET.Element) -> None:
    """Fill count-based header elements that are only known after parsing.

    Populates ``<tagsDecl>`` with per-tag usage and ``<extent>`` with speech
    and word counts, both computed from the ``<text>`` subtree. Each is only
    touched when present *and* empty, so manually provided values survive.
    """
    text = root.find("text")
    if text is None:
        return

    tags_decl = root.find("teiHeader/encodingDesc/tagsDecl")
    if tags_decl is not None and len(tags_decl) == 0:
        counts = Counter(elem.tag for elem in text.iter() if isinstance(elem.tag, str))
        namespace = ET.SubElement(tags_decl, "namespace", name=TEI_NAMESPACE)
        for tag_name, count in sorted(counts.items()):
            ET.SubElement(namespace, "tagUsage", gi=tag_name, occurs=str(count))

    extent = root.find("teiHeader/fileDesc/extent")
    if extent is not None and len(extent) == 0:
        speeches = len(text.findall(".//u"))

        def content_text(element: ET.Element):
            if element.text:
                yield element.text
            for child in element:
                if isinstance(child.tag, str):
                    yield from content_text(child)
                if child.tail:
                    yield child.tail

        words = len(re.findall(r"\S+", " ".join(content_text(text))))
        for unit, quantity in (("speeches", speeches), ("words", words)):
            measure = ET.SubElement(
                extent, "measure", unit=unit, quantity=str(quantity)
            )
            measure.text = f"{quantity} {unit}"
