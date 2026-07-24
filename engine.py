from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Literal, TYPE_CHECKING, cast, overload

if TYPE_CHECKING:
    # python-docx is only needed for Word documents; importing it lazily keeps
    # PDF-only invocations from paying its startup cost.
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run
from type_decs import (
    Action,
    Chunk,
    CosmeticAnnotations,
    OnPop,
    PDFCosmeticAnnotation,
    WordCosmeticAnnotation,
)
from dataclasses import dataclass, field
from collections import defaultdict

# any character XML 1.0 cannot represent (the complement of the ranges
# accepted below); clean text short-circuits without a per-character loop
_XML_UNSAFE_RE = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
# conservative ASCII NCName; non-ASCII identifiers fall back to the full check
_ASCII_NCNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")


def xml_safe_text(value: object) -> str:
    """Return text that can always be serialized as XML 1.0.

    PDF text layers occasionally contain NULs or other control characters.
    XML cannot represent those code points, so retain their identity as a
    visible token instead of either dropping them or failing at serialization.
    """

    text = str(value)
    if _XML_UNSAFE_RE.search(text) is None:
        return text
    parts: list[str] = []
    for character in text:
        codepoint = ord(character)
        if (
            character in "\t\n\r"
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            parts.append(character)
        else:
            parts.append(f"[U+{codepoint:04X}]")
    return "".join(parts)


def sanitize_xml_id(value: object, *, prefix: str = "id") -> str:
    """Coerce arbitrary source text to a conservative XML ``NCName``.

    The subset used here (Unicode alphanumerics plus ``_.-``) is deliberately
    narrower than the complete XML grammar. It is portable across validators,
    deterministic, and leaves already-valid identifiers byte-for-byte intact.
    """

    text = xml_safe_text(value).strip()
    if _ASCII_NCNAME_RE.fullmatch(text):
        return text
    if (
        text
        and (text[0].isalpha() or text[0] == "_")
        and all(character.isalnum() or character in "_.-" for character in text)
    ):
        return text
    cleaned = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in text
    )
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    safe_prefix = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in prefix
    ).strip("-.")
    if not safe_prefix or not (safe_prefix[0].isalpha() or safe_prefix[0] == "_"):
        safe_prefix = "id"
    if not cleaned:
        return safe_prefix
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        return f"{safe_prefix}-{cleaned}"
    return cleaned


def default_document() -> tuple[ET.Element, ET.Element]:
    """Build the standard TEI > text > body > div[debateSection] skeleton.

    Returns the ``<TEI>`` root and the element parsed content lands in. A
    config can supply its own skeleton via ``CONFIG["document"]``.
    """
    tei = ET.Element("TEI", version="3.3.0", xmlns="http://www.tei-c.org/ns/1.0")
    text = ET.SubElement(tei, "text")
    body = ET.SubElement(text, "body")
    content = ET.SubElement(body, "div", type="debateSection")
    return tei, content


root, debate = default_document()
debate.text, debate.tail = "", ""
children: list[str | ET.Element] = []  # reference to the innermost stack tag's children


@dataclass
class StackEntry:
    element: ET.Element
    children: list[str | ET.Element]
    last_elem: ET.Element | None
    cosmetic: bool


stack: list[StackEntry] = [
    StackEntry(element=debate, children=children, last_elem=None, cosmetic=False)
]  # le stack of open tags

# will be assigned in parse.py
COSMETIC_ANNOTATIONS: CosmeticAnnotations

on_pop: OnPop | None = None
filename: str
# set from CONFIG["auto_xml_ids"] by the parser; when true, push() stamps a
# generated xml:id on structural (non-cosmetic) elements that don't have one
auto_xml_ids = False
id_counters = defaultdict(int)
# Batch parsing supplies the final ParlaMint component stem before any rule
# pushes an element. Standalone parsing leaves this unset and keeps using the
# extensionless source filename.
id_prefix: str | None = None


def next_id(tag: str | ET.Element) -> int:
    # 1-based, following the ParlaMint id convention (seg1, u1, ...)
    name = tag if isinstance(tag, str) else tag.tag
    id_counters[name] += 1
    return id_counters[name]


def gen_id(tag: str | ET.Element) -> str:
    idx = next_id(tag)
    name = tag if isinstance(tag, str) else tag.tag
    prefix = id_prefix
    if prefix is None:
        prefix = filename.rsplit(".", 1)[0] if "." in filename else filename
    return sanitize_xml_id(f"{prefix}.{name}{idx}", prefix="doc")


def reset(document: tuple[ET.Element, ET.Element] | None = None) -> None:
    """Reset all mutable engine state so multiple documents can be parsed safely.

    ``document`` optionally replaces the default TEI skeleton: a ``(root,
    content)`` tuple where ``content`` is the descendant element parsed
    content is appended into.
    """
    global root, debate, children, stack, on_pop, id_prefix
    root, debate = document if document is not None else default_document()
    debate.text, debate.tail = "", ""
    children = []
    stack = [
        StackEntry(element=debate, children=children, last_elem=None, cosmetic=False)
    ]
    on_pop = None
    id_prefix = None
    id_counters.clear()


@dataclass
class WordChunk:
    x: float
    y: float
    w: int
    h: int
    text: str
    bold: bool | None
    italic: bool | None
    run: Run
    paragraph: Paragraph
    space_before: bool = True


@dataclass
class PDFPageContext:
    page_num: int
    width: float
    height: float
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class PDFLineChunk:
    x: float  # leftmost x
    y: float  # leftmost bottom y
    text: str  # all text
    bold: bool | None  # all bold
    italic: bool | None  # all italic
    runs: "list[PDFChunk]"  # child chunks
    space_before: bool = True  # whole-line chunks are never appended directly
    page_context: PDFPageContext | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class PDFChunk:
    # one chunk of text
    # :D
    x: float
    y: float
    text: str
    bold: bool | None
    italic: bool | None
    font_name: str
    font_size: float
    page_num: int
    space_before: bool = True
    _line_chunk: PDFLineChunk | None = field(
        default=None, init=False, repr=False
    )  # so we don't get lint errors, since we don't provide the line chunk at init
    previous: PDFChunk | None = field(default=None, repr=False)
    page_context: PDFPageContext | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def line_chunk(self) -> PDFLineChunk:
        if self._line_chunk is None:
            raise RuntimeError("PDF chunk has not been attached to a line")
        return self._line_chunk

    @line_chunk.setter
    def line_chunk(self, value: PDFLineChunk):
        self._line_chunk = value

    @property
    def is_line_start(self) -> bool:
        return bool(self.line_chunk.runs) and self is self.line_chunk.runs[0]

    @property
    def line_text(self) -> str:
        return self.line_chunk.text


def get_para_xywh(para: Paragraph):
    from docx.oxml.ns import qn

    p = para._p

    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        raise ValueError("pPr is None")

    framePr = pPr.find(qn("w:framePr"))
    if framePr is None:
        raise ValueError("framePr is None")

    return (
        int(framePr.get(qn("w:x")) or 0),
        int(framePr.get(qn("w:y")) or 0),
        int(framePr.get(qn("w:w")) or 0),
        int(framePr.get(qn("w:h")) or 0),
    )


# good dx, or something
@overload
def make_chunk(
    word_prop: Run, parent_paragraph: Paragraph, *, page_num: int
) -> WordChunk: ...
@overload
def make_chunk(
    *,
    text: str,
    x: float,
    y: float,
    font_name: str,
    size: float,
    previous: PDFChunk | None,
    page_num: int,
    space_before: bool = True,
    page_context: PDFPageContext | None = None,
    metadata: dict[str, object] | None = None,
) -> PDFChunk: ...
@overload
def make_chunk(
    *,
    text: str,
    x: float,
    y: float,
    runs: list[PDFChunk],
    page_num: int,
    page_context: PDFPageContext | None = None,
    metadata: dict[str, object] | None = None,
) -> PDFLineChunk: ...


def make_chunk(
    word_prop: Run | None = None,
    parent_paragraph: Paragraph | None = None,
    *,
    text: str | None = None,
    x: float | None = None,
    y: float | None = None,
    font_name: str | None = None,
    size: float | None = None,
    runs: list[PDFChunk] | None = None,
    previous: PDFChunk | None = None,
    page_num: int,
    space_before: bool = True,
    page_context: PDFPageContext | None = None,
    metadata: dict[str, object] | None = None,
):
    if word_prop is not None and parent_paragraph is not None:
        run = word_prop
        x, y, w, h = get_para_xywh(parent_paragraph)
        return WordChunk(
            x=x,
            y=y,
            w=int(w),
            h=int(h),
            text=run.text,
            bold=run.bold,
            italic=run.italic,
            run=run,
            paragraph=parent_paragraph,
            space_before=space_before,
        )
    # i hate the python type checker
    assert text is not None
    assert x is not None
    assert y is not None
    if runs is not None:
        return PDFLineChunk(
            x=x,
            y=y,
            text=text,
            bold=all(i.bold for i in runs),
            italic=all(i.italic for i in runs),
            runs=runs,
            space_before=space_before,
            page_context=page_context,
            metadata=dict(metadata or {}),
        )
    else:
        assert font_name is not None
        assert size is not None
        return PDFChunk(
            x=x,
            y=y,
            text=text,
            bold="bold" in font_name.lower(),
            italic="italic" in font_name.lower(),
            font_name=font_name,
            font_size=size,
            previous=previous,
            page_num=page_num,
            space_before=space_before,
            page_context=page_context,
            metadata=dict(metadata or {}),
        )


def commit_children(stack_instance: StackEntry):
    # appends children to the element
    # we have a children list ["abc", <emph>hi</emph>, "def"]
    # but that's not commited to the element yet (ET.Element)
    # so we gotta actually append it
    last_elem = stack_instance.last_elem
    elem = stack_instance.element
    for child in stack_instance.children:
        if isinstance(child, str) and last_elem is None:
            elem.text = (elem.text or "") + child
        elif isinstance(child, str) and last_elem is not None:
            last_elem.tail = (last_elem.tail or "") + child
        elif isinstance(child, ET.Element) and last_elem is None:
            elem.append(child)
            # last_elem is used for its tail
            # if we have text after it (which we do usually) we gotta append it to its tail
            last_elem = child
            # because .text and .tail can be None, we need to set it to "" so we can +=
            last_elem.tail = ""
        elif isinstance(child, ET.Element) and last_elem is not None:
            elem.append(child)
            # this is now the last element - text that follows belongs in ITS
            # tail, otherwise it gets serialized before this element
            last_elem = child
            last_elem.tail = ""

    stack_instance.last_elem = last_elem


@overload
def push(
    tag: str, *, cosmetic: bool = False, attribs: dict[str, str] = {}, **rest: str
): ...
@overload
def push(tag: ET.Element, *, cosmetic: bool = False): ...


def push(
    tag: str | ET.Element,
    *,
    cosmetic: bool = False,
    attribs: dict[str, str] = {},
    **rest: str,
):
    # push element on top of stack, set `children` to be the element's children
    global children
    children = []

    if isinstance(tag, ET.Element):
        attribs = tag.attrib
        tag = tag.tag

    safe_attribs = {
        key: (sanitize_xml_id(value) if key == "xml:id" else xml_safe_text(value))
        for key, value in {**attribs, **rest}.items()
    }
    elem = ET.Element(tag, safe_attribs)
    elem.text = ""
    elem.tail = ""
    if auto_xml_ids and not cosmetic and "xml:id" not in elem.attrib:
        elem.attrib["xml:id"] = gen_id(elem)

    stack.append(
        StackEntry(element=elem, children=children, last_elem=None, cosmetic=cosmetic)
    )
    return elem


def pop():
    # remove the innermost stack entry, commit children to it, append it as a child of the parent (the now innermost stack entry)
    # and change the children reference
    global children
    elem = stack.pop()
    commit_children(elem)
    stack[-1].children.append(elem.element)
    children = stack[-1].children

    if on_pop is not None:
        on_pop(elem)

    return elem


@overload
def pop_to(*parent_tags: str, invert: bool = False): ...
@overload
def pop_to(*parent_tags: ET.Element, invert: bool = False): ...


def pop_to(*parent_tags: str | ET.Element, invert: bool = False):
    # pops until the stack top is one of parent_tags
    str_parent_tags: list[str] = [
        i.tag if isinstance(i, ET.Element) else i for i in parent_tags
    ]

    if invert:
        while len(stack) > 1 and stack[-1].element.tag in str_parent_tags:
            pop()
    else:
        while len(stack) > 1 and stack[-1].element.tag not in str_parent_tags:
            pop()


@overload
def tag_is_on_top(tag: str, ignore_cosmetic: bool = True, **attribs: str): ...
@overload
def tag_is_on_top(tag: ET.Element, ignore_cosmetic: bool = True): ...


def tag_is_on_top(tag: str | ET.Element, ignore_cosmetic: bool = True, **attribs: str):
    # is the tag on top of the stack?
    # ignores cosmetic entries if needed (mostly the case)
    if isinstance(tag, ET.Element):
        attribs = tag.attrib
        tag = tag.tag  # me when

    # ignore any cosmetic elements and match the first structural element
    for entry in reversed(stack):
        if ignore_cosmetic and entry.cosmetic:
            continue
        return entry.element.tag == tag and all(
            val == entry.element.attrib.get(key) for key, val in attribs.items()
        )

    return False


def is_before_layout(tag: ET.Element):
    # is the (cosmetic) tag before any layout / structural elements (i still haven't decided how to call these lmao)
    for entry in reversed(stack):
        if not entry.cosmetic:
            return False
        if entry.cosmetic and all(
            val == entry.element.attrib.get(key) for key, val in tag.attrib.items()
        ):
            return True

    return False


def tag(tag: str, **attribs: str):
    # helper for tag
    # may remove if it's too confusing
    return ET.Element(tag, attribs)


def append_comment(text: str):
    text = xml_safe_text(text).replace("--", "- -")  # me when XSS
    if text.endswith("-"):
        text += " "
    children.append(cast(ET.Element, ET.Comment(text)))


def append(*chunks: Chunk, should_annotate: list[str] | Literal[True] = True):
    # appends runs to children
    # takes care of italic / bold / references (which can appear anywhere) and of the separating space between runs
    if should_annotate is True:
        should_annotate = list(COSMETIC_ANNOTATIONS.keys())
    else:
        # if you can't beat the type checker, become the type checker
        for ann in should_annotate:
            if ann not in COSMETIC_ANNOTATIONS:
                raise ValueError(
                    f"value {ann} does not exist in {COSMETIC_ANNOTATIONS.keys()}"
                )

    for chunk in chunks:
        # since all spaces we'll need are in either this .space_before prop or the next chunk's space_before, we can strip it
        # and avoid any incidents where spaces appear out of thin air
        text = xml_safe_text(chunk.text).strip()
        if not text:
            continue

        # we need to ignore the cosmetic annotations for spaces
        # so we cache the previous stack ids without the to be added cosmetics
        outer = list(stack)
        outer_ids = {id(e) for e in outer}

        if isinstance(chunk, PDFChunk):
            for name, annotation in COSMETIC_ANNOTATIONS.items():
                annotation = cast(PDFCosmeticAnnotation, annotation)
                if (
                    name in should_annotate
                    and not is_before_layout(annotation["tag"])
                    and annotation["test"](chunk)
                ):
                    if "append_func" in annotation:
                        annotation["append_func"](chunk)
                    else:
                        push(annotation["tag"], cosmetic=True)
                elif (
                    name in should_annotate
                    and is_before_layout(annotation["tag"])
                    and not annotation["test"](chunk)
                ):
                    pop_to(annotation["tag"], invert=True)

        elif isinstance(chunk, WordChunk):
            for name, annotation in COSMETIC_ANNOTATIONS.items():
                annotation = cast(WordCosmeticAnnotation, annotation)
                if (
                    name in should_annotate
                    and is_before_layout(annotation["tag"])
                    and annotation["test"](chunk)
                ):
                    if "append_func" in annotation:
                        annotation["append_func"](chunk)
                    else:
                        push(annotation["tag"])
                elif (
                    name in should_annotate
                    and is_before_layout(annotation["tag"])
                    and not annotation["test"](chunk)
                ):
                    pop_to(annotation["tag"], invert=True)

        # append the space if needed
        # if the previous container already has content, we append the space there before adding our text (so it's between the two)
        # if not, we don't append a stray leading space
        if chunk.space_before:
            for entry in reversed(stack):
                if id(entry) not in outer_ids:
                    continue
                if entry.children:
                    entry.children.append(" ")
                break

        children.append(text)


def pop_and_push_to(
    *pop_args: str, tag: str, chunked: bool = True, **attribs: str
) -> Action:
    # the normalest action
    # pops to pop_args and pushes the tag
    def action(chunk: Chunk):
        # we love closures
        if not chunked or not tag_is_on_top(tag, True, **attribs):
            pop_to(*pop_args)
            push(tag, attribs=attribs)

    return action
