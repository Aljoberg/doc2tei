import re
import xml.etree.ElementTree as ET
from typing import Literal, Any, Protocol, cast, overload
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from type_decs import (
    Action,
    CosmeticAnnotations,
    OnPop,
    PDFCosmeticAnnotation,
    WordCosmeticAnnotation,
)
from dataclasses import dataclass, field
from docx.oxml.ns import qn

root = ET.Element("TEI", version="3.3.0", xmlns="http://www.tei-c.org/ns/1.0")
text_elem = ET.SubElement(root, "text")
body = ET.SubElement(text_elem, "body")
debate = ET.SubElement(body, "div", type="debateSection")
debate.text, debate.tail = "", ""
children: list[str | ET.Element[str]] = (
    []
)  # reference to the innermost stack tag's children


@dataclass
class StackEntry:
    element: ET.Element[str]
    children: list[str | ET.Element[str]]
    last_elem: ET.Element[str] | None
    cosmetic: bool


stack: list[StackEntry] = [
    StackEntry(element=debate, children=children, last_elem=None, cosmetic=False)
]  # le stack of open tags

# will be assigned in parse.py
COSMETIC_ANNOTATIONS: CosmeticAnnotations

on_pop: OnPop | None = None


class Chunk(Protocol):
    x: float
    y: float
    text: str
    bold: bool | None
    italic: bool | None
    # whether there's a space to be added at the start
    # it can be because of a deljaj, a gap or a line break
    space_before: bool


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
class PDFLineChunk:
    x: float  # leftmost x
    y: float  # leftmost bottom y
    text: str  # all text
    bold: bool | None  # all bold
    italic: bool | None  # all italic
    runs: "list[PDFChunk]"  # child chunks
    space_before: bool = True  # whole-line chunks are never appended directly


@dataclass
class PDFChunk:
    # one chunk of text
    # :D
    x: float
    y: float
    text: str
    bold: bool | None
    italic: bool | None
    font_size: float
    page_num: int
    space_before: bool = True
    _line_chunk: Any = field(
        default=None, init=False, repr=False
    )  # so we don't get lint errors, since we don't provide the line chunk at init
    previous: PDFChunk | None = field(default=None, repr=False)

    @property
    def line_chunk(self) -> PDFLineChunk:
        return self._line_chunk

    @line_chunk.setter
    def line_chunk(self, value: PDFLineChunk):
        self._line_chunk = value


def get_para_xywh(para: Paragraph):
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
) -> PDFChunk: ...
@overload
def make_chunk(
    *, text: str, x: float, y: float, runs: list[PDFChunk], page_num: int
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
):
    if isinstance(word_prop, Run) and isinstance(parent_paragraph, Paragraph):
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
            font_size=size,
            previous=previous,
            page_num=page_num,
            space_before=space_before,
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
def push(tag: ET.Element[str], *, cosmetic: bool = False): ...


def push(
    tag: str | ET.Element[str],
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

    elem = ET.Element(tag, attribs, **rest)
    elem.text = ""
    elem.tail = ""
    stack.append(
        StackEntry(element=elem, children=children, last_elem=None, cosmetic=cosmetic)
    )


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
def pop_to(*parent_tags: ET.Element[str], invert: bool = False): ...


def pop_to(*parent_tags: str | ET.Element[str], invert: bool = False):
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
def tag_is_on_top(tag: ET.Element[str], ignore_cosmetic: bool = True): ...


def tag_is_on_top(tag: str | ET.Element[str], ignore_cosmetic: bool = True, **attribs: str):
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


def is_before_layout(tag: ET.Element[str]):
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
        text = chunk.text.strip()
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
