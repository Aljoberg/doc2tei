from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Literal

from engine import Chunk, PDFChunk, PDFPageContext, make_chunk


@dataclass(frozen=True)
class PageRange:
    """Zero-based, half-open page range."""

    start: int = 0
    end: int | None = None

    def contains(self, page_num: int) -> bool:
        return page_num >= self.start and (self.end is None or page_num < self.end)


@dataclass
class RunRecord:
    text: str
    x: float
    y: float
    font_name: str
    font_size: float
    space_before: bool = True


@dataclass
class LineRecord:
    text: str
    x: float
    y: float
    font_name: str
    font_size: float
    runs: list[RunRecord]
    metadata: dict[str, Any] = field(default_factory=dict)


LineFilter = Callable[[LineRecord, PDFPageContext], bool]
StopTest = Callable[[LineRecord, PDFPageContext], bool]
PageEnricher = Callable[[PDFPageContext, list[LineRecord]], None]
LineEnricher = Callable[
    [PDFPageContext, LineRecord, int, list[LineRecord]], dict[str, Any] | None
]
LineBreakTest = Callable[[float, float], bool]


def _weighted_mode(words: list[dict[str, Any]], key: str) -> Any:
    counts: Counter[Any] = Counter()
    for word in words:
        counts[word[key]] += max(1, len(str(word["text"])))
    return counts.most_common(1)[0][0]


class CharacterPDFExtractor:
    """Configurable pdfminer character pipeline.

    The class supplies collection, line/run construction and standard context;
    document configs still choose all thresholds and may replace the line-break
    policy, filtering and metadata enrichment.
    """

    def __init__(
        self,
        *,
        pages: PageRange = PageRange(),
        line_tolerance: float = 4.0,
        line_break: LineBreakTest | None = None,
        literal_spaces: Literal["preserve", "break", "ignore"] = "preserve",
        gap_threshold: float = 1.7,
        max_run_x_gap: float | None = None,
        line_filter: LineFilter | None = None,
        stop_before: StopTest | None = None,
        page_enricher: PageEnricher | None = None,
        line_enricher: LineEnricher | None = None,
    ):
        self.pages = pages
        self.line_tolerance = line_tolerance
        self.line_break = line_break
        self.literal_spaces = literal_spaces
        self.gap_threshold = gap_threshold
        self.max_run_x_gap = max_run_x_gap
        self.line_filter = line_filter
        self.stop_before = stop_before
        self.page_enricher = page_enricher
        self.line_enricher = line_enricher

    def __call__(self, filename: str) -> Generator[Chunk, Any, Any]:
        from pdfminer.converter import PDFLayoutAnalyzer
        from pdfminer.layout import LTChar
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdfpage import PDFPage

        resource_manager = PDFResourceManager()

        class CharCollector(PDFLayoutAnalyzer):
            def __init__(self):
                super().__init__(resource_manager, laparams=None)
                self.chars: list[LTChar] = []
                self.width = 0.0
                self.height = 0.0

            def receive_layout(self, page: Any):
                self.width = float(page.width)
                self.height = float(page.height)

                def walk(obj: Any):
                    for item in obj:
                        if isinstance(item, LTChar):
                            self.chars.append(item)
                        elif hasattr(item, "__iter__"):
                            walk(item)

                walk(page)

        device = CharCollector()
        interpreter = PDFPageInterpreter(resource_manager, device)
        previous_run: PDFChunk | None = None
        pending_space = False

        with open(filename, "rb") as source:
            for page_num, page in enumerate(PDFPage.get_pages(source)):
                if page_num < self.pages.start:
                    continue
                if self.pages.end is not None and page_num >= self.pages.end:
                    break

                device.chars = []
                interpreter.process_page(page)
                context = PDFPageContext(page_num, device.width, device.height)
                raw_lines = self._group_lines(device.chars)
                records, pending_values = self._make_records(raw_lines, pending_space)
                if records:
                    pending_space = pending_values[-1]
                if self.page_enricher is not None:
                    self.page_enricher(context, records)

                for index, record in enumerate(records):
                    if self.stop_before is not None and self.stop_before(
                        record, context
                    ):
                        return
                    if self.line_filter is not None and not self.line_filter(
                        record, context
                    ):
                        continue
                    if self.line_enricher is not None:
                        record.metadata.update(
                            self.line_enricher(context, record, index, records) or {}
                        )

                    run_chunks: list[PDFChunk] = []
                    for run in record.runs:
                        previous_run = make_chunk(
                            text=run.text,
                            x=run.x,
                            y=run.y,
                            font_name=run.font_name,
                            size=run.font_size,
                            previous=previous_run,
                            page_num=page_num,
                            space_before=run.space_before,
                            page_context=context,
                        )
                        run_chunks.append(previous_run)
                    if not run_chunks:
                        continue
                    line_chunk = make_chunk(
                        text=record.text,
                        x=record.x,
                        y=record.y,
                        runs=run_chunks,
                        page_num=page_num,
                        page_context=context,
                        metadata=record.metadata,
                    )
                    for run in run_chunks:
                        run.line_chunk = line_chunk
                    yield from run_chunks

    def _is_line_break(self, previous_y: float, current_y: float) -> bool:
        if self.line_break is not None:
            return self.line_break(previous_y, current_y)
        return abs(previous_y - current_y) > self.line_tolerance

    def _group_lines(self, chars: list[Any]) -> list[list[Any]]:
        lines: list[list[Any]] = []
        current: list[Any] = []
        previous_y: float | None = None
        for char in chars:
            if previous_y is not None and self._is_line_break(previous_y, char.y0):
                if current:
                    lines.append(current)
                current = []
            current.append(char)
            previous_y = char.y0
        if current:
            lines.append(current)
        return lines

    def _make_records(
        self, lines: list[list[Any]], initial_pending_space: bool
    ) -> tuple[list[LineRecord], list[bool]]:
        records: list[LineRecord] = []
        pending_space = initial_pending_space
        pending_values: list[bool] = []
        for line in lines:
            runs: list[dict[str, Any]] = []
            previous = None
            broke = False
            for char in line:
                char_text = char.get_text()
                if char_text == " " and self.literal_spaces == "break":
                    broke = True
                    break
                if char_text == " " and self.literal_spaces == "ignore":
                    previous = char
                    continue
                gap = bool(previous) and char.x0 - previous.x1 > self.gap_threshold
                within_max_gap = (
                    True
                    if self.max_run_x_gap is None or previous is None
                    else abs(previous.x0 - char.x0) < self.max_run_x_gap
                )
                grouped = (
                    bool(runs)
                    and runs[-1]["fontname"] == char.fontname
                    and runs[-1]["size"] == char.size
                    and within_max_gap
                )
                if grouped:
                    if gap and self.literal_spaces != "preserve":
                        runs[-1]["text"] += " "
                    runs[-1]["text"] += char_text
                else:
                    runs.append(
                        {
                            "text": char_text,
                            "x0": char.x0,
                            "y0": char.y0,
                            "fontname": char.fontname,
                            "size": char.size,
                            "gap": bool(gap),
                        }
                    )
                previous = char

            if not runs or not "".join(str(run["text"]) for run in runs).strip():
                pending_space = not broke
                pending_values.append(pending_space)
                continue

            run_records: list[RunRecord] = []
            for index, run in enumerate(runs):
                if index == 0:
                    space_before = pending_space
                elif self.literal_spaces == "preserve":
                    prior = str(runs[index - 1]["text"])
                    current = str(run["text"])
                    space_before = prior[-1:].isspace() or current[:1].isspace()
                else:
                    space_before = bool(run["gap"])
                run_records.append(
                    RunRecord(
                        text=str(run["text"]),
                        x=float(run["x0"]),
                        y=float(run["y0"]),
                        font_name=str(run["fontname"]),
                        font_size=float(run["size"]),
                        space_before=space_before,
                    )
                )
            text = "".join(run.text for run in run_records)
            first = run_records[0]
            records.append(
                LineRecord(
                    text=text,
                    x=first.x,
                    y=first.y,
                    font_name=first.font_name,
                    font_size=first.font_size,
                    runs=run_records,
                )
            )
            pending_space = not broke if self.literal_spaces == "break" else True
            pending_values.append(pending_space)
        return records, pending_values


class WordPDFExtractor:
    """Configurable pdfplumber word-to-line pipeline."""

    def __init__(
        self,
        *,
        pages: PageRange = PageRange(),
        x_tolerance: float = 1.7,
        y_tolerance: float = 3.0,
        line_tolerance: float = 3.2,
        word_gap: float = 0.6,
        join_line_end_hyphens: bool = False,
        line_filter: LineFilter | None = None,
        stop_before: StopTest | None = None,
        page_enricher: PageEnricher | None = None,
        line_enricher: LineEnricher | None = None,
    ):
        self.pages = pages
        self.x_tolerance = x_tolerance
        self.y_tolerance = y_tolerance
        self.line_tolerance = line_tolerance
        self.word_gap = word_gap
        self.join_line_end_hyphens = join_line_end_hyphens
        self.line_filter = line_filter
        self.stop_before = stop_before
        self.page_enricher = page_enricher
        self.line_enricher = line_enricher

    def __call__(self, filename: str) -> Generator[Chunk, Any, Any]:
        import pdfplumber

        previous_run: PDFChunk | None = None
        pending_space = False
        with pdfplumber.open(filename) as pdf:
            for page_num, page in enumerate(pdf.pages):
                if page_num < self.pages.start:
                    continue
                if self.pages.end is not None and page_num >= self.pages.end:
                    break
                context = PDFPageContext(
                    page_num=page_num,
                    width=float(page.width),
                    height=float(page.height),
                )
                words = page.extract_words(
                    x_tolerance=self.x_tolerance,
                    y_tolerance=self.y_tolerance,
                    keep_blank_chars=False,
                    use_text_flow=True,
                    extra_attrs=["fontname", "size"],
                )
                records = [
                    self._record(context.height, words_on_line)
                    for words_on_line in self._group_words(words)
                    if words_on_line
                ]
                records = [record for record in records if record.text]
                if self.page_enricher is not None:
                    self.page_enricher(context, records)

                for index, record in enumerate(records):
                    if self.stop_before is not None and self.stop_before(
                        record, context
                    ):
                        return
                    if self.line_filter is not None and not self.line_filter(
                        record, context
                    ):
                        continue
                    if self.line_enricher is not None:
                        record.metadata.update(
                            self.line_enricher(context, record, index, records) or {}
                        )
                    text = record.text
                    hyphenated = self.join_line_end_hyphens and text.endswith(
                        ("-", "‐", "‑")
                    )
                    if hyphenated:
                        text = text[:-1]
                    previous_run = make_chunk(
                        text=text,
                        x=record.x,
                        y=record.y,
                        font_name=record.font_name,
                        size=record.font_size,
                        previous=previous_run,
                        page_num=page_num,
                        space_before=pending_space,
                        page_context=context,
                    )
                    line_chunk = make_chunk(
                        text=text,
                        x=record.x,
                        y=record.y,
                        runs=[previous_run],
                        page_num=page_num,
                        page_context=context,
                        metadata=record.metadata,
                    )
                    previous_run.line_chunk = line_chunk
                    yield previous_run
                    pending_space = not hyphenated

    def _group_words(self, words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        lines: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        anchor_top: float | None = None
        for word in words:
            top = float(word["top"])
            if (
                current
                and anchor_top is not None
                and abs(top - anchor_top) > self.line_tolerance
            ):
                lines.append(sorted(current, key=lambda item: float(item["x0"])))
                current = []
                anchor_top = None
            current.append(word)
            if anchor_top is None or float(word["size"]) >= 9:
                anchor_top = top
        if current:
            lines.append(sorted(current, key=lambda item: float(item["x0"])))
        return lines

    def _record(self, page_height: float, words: list[dict[str, Any]]) -> LineRecord:
        parts: list[str] = []
        previous = None
        for word in words:
            if (
                previous is not None
                and float(word["x0"]) - float(previous["x1"]) > self.word_gap
            ):
                parts.append(" ")
            parts.append(str(word["text"]))
            previous = word
        size = float(_weighted_mode(words, "size"))
        font = str(_weighted_mode(words, "fontname"))
        baseline = next(
            (word for word in words if abs(float(word["size"]) - size) < 0.05),
            words[0],
        )
        text = "".join(parts).strip()
        x = min(float(word["x0"]) for word in words)
        y = page_height - float(baseline["bottom"])
        run = RunRecord(text, x, y, font, size)
        return LineRecord(text, x, y, font, size, [run])
