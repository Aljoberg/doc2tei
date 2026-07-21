from __future__ import annotations

from collections.abc import Iterable
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import cast, Iterator, Literal, TYPE_CHECKING

from engine import PDFChunk, PDFPageContext, make_chunk
from type_decs import (
    Chunk,
    ExtractedWord,
    LineBreakTest,
    LineEnricher,
    LineFilter,
    PageEnricher,
    PageErrorHandler,
    StopTest,
)

if TYPE_CHECKING:
    from pdfminer.layout import LTChar, LTPage


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
    metadata: dict[str, object] = field(default_factory=dict)
    x_end: float | None = None


@dataclass
class _CharacterRun:
    text: str
    x0: float
    y0: float
    fontname: str
    size: float
    gap: bool
    x1: float
    merge_previous: bool = False


def _weighted_mode(
    words: list[ExtractedWord], key: Literal["size", "fontname"]
) -> float | str:
    counts: Counter[float | str] = Counter()
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
        merge_nearby_runs: bool = True,
        font_size_tolerance: float = 0.1,
        baseline_tolerance: float = 0.25,
        line_filter: LineFilter | None = None,
        stop_before: StopTest | None = None,
        page_enricher: PageEnricher | None = None,
        line_enricher: LineEnricher | None = None,
        page_error_handler: PageErrorHandler | None = None,
    ):
        self.pages = pages
        self.line_tolerance = line_tolerance
        self.line_break = line_break
        self.literal_spaces = literal_spaces
        self.gap_threshold = gap_threshold
        self.max_run_x_gap = max_run_x_gap
        self.merge_nearby_runs = merge_nearby_runs
        self.font_size_tolerance = max(0.0, font_size_tolerance)
        self.baseline_tolerance = max(0.0, baseline_tolerance)
        self.line_filter = line_filter
        self.stop_before = stop_before
        self.page_enricher = page_enricher
        self.line_enricher = line_enricher
        self.page_error_handler = page_error_handler

    def __call__(self, filename: str) -> Iterator[Chunk]:
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

            def receive_layout(self, page: LTPage):
                self.width = float(page.width)
                self.height = float(page.height)

                def walk(obj: Iterable[object]):
                    for item in obj:
                        if isinstance(item, LTChar):
                            self.chars.append(item)
                        elif isinstance(item, Iterable):
                            walk(cast(Iterable[object], item))

                walk(cast(Iterable[object], page))

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

                try:
                    device.chars = []
                    interpreter.process_page(page)
                    context = PDFPageContext(page_num, device.width, device.height)
                    raw_lines = self._group_lines(device.chars)
                    records, pending_values = self._make_records(
                        raw_lines, pending_space
                    )
                    if records:
                        pending_space = pending_values[-1]
                    if self.page_enricher is not None:
                        self.page_enricher(context, records)
                except Exception as error:
                    if self.page_error_handler is None:
                        raise
                    self.page_error_handler(page_num, error)
                    pending_space = True
                    continue

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

                    flow_previous_run = previous_run
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
                    if record.metadata.get("out_of_flow"):
                        previous_run = flow_previous_run

    def _is_line_break(self, previous_y: float, current_y: float) -> bool:
        if self.line_break is not None:
            return self.line_break(previous_y, current_y)
        return abs(previous_y - current_y) > self.line_tolerance

    def _group_lines(self, chars: list[LTChar]) -> list[list[LTChar]]:
        lines: list[list[LTChar]] = []
        current: list[LTChar] = []
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
        self, lines: list[list[LTChar]], initial_pending_space: bool
    ) -> tuple[list[LineRecord], list[bool]]:
        records: list[LineRecord] = []
        pending_space = initial_pending_space
        pending_values: list[bool] = []
        pending_lines = deque(lines)
        while pending_lines:
            line = pending_lines.popleft()
            runs: list[_CharacterRun] = []
            previous: LTChar | None = None
            broke = False
            for index, char in enumerate(line):
                char_text = char.get_text()
                if char_text == " " and self.literal_spaces == "break":
                    broke = True
                    remainder = line[index + 1 :]
                    if remainder:
                        pending_lines.appendleft(remainder)
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
                    and runs[-1].fontname == char.fontname
                    and runs[-1].size == char.size
                    and within_max_gap
                )
                if grouped:
                    if gap and self.literal_spaces != "preserve":
                        runs[-1].text += " "
                    runs[-1].text += char_text
                    runs[-1].x1 = float(char.x1)
                else:
                    merge_previous = bool(
                        self.merge_nearby_runs
                        # The line-start run can trigger different structural
                        # actions than later runs, so never absorb run 2 into it.
                        and len(runs) > 1
                        and runs[-1].fontname == char.fontname
                        and abs(runs[-1].size - float(char.size))
                        <= self.font_size_tolerance
                        and abs(runs[-1].y0 - float(char.y0))
                        <= self.baseline_tolerance
                        and within_max_gap
                    )
                    runs.append(
                        _CharacterRun(
                            text=char_text,
                            x0=float(char.x0),
                            y0=float(char.y0),
                            fontname=str(char.fontname),
                            size=float(char.size),
                            gap=bool(gap),
                            x1=float(char.x1),
                            merge_previous=merge_previous,
                        )
                    )
                previous = char

            if not runs or not "".join(run.text for run in runs).strip():
                pending_space = not broke
                pending_values.append(pending_space)
                continue

            run_records: list[RunRecord] = []
            for index, run in enumerate(runs):
                if index == 0:
                    space_before = pending_space
                elif self.literal_spaces == "preserve":
                    prior = runs[index - 1].text
                    current = run.text
                    space_before = prior[-1:].isspace() or current[:1].isspace()
                else:
                    space_before = run.gap
                run_records.append(
                    RunRecord(
                        text=run.text,
                        x=run.x0,
                        y=run.y0,
                        font_name=run.fontname,
                        font_size=run.size,
                        space_before=space_before,
                    )
                )
            text = "".join(run.text for run in run_records)
            if self.merge_nearby_runs:
                run_records = self._merge_run_records(run_records, runs)
            first = run_records[0]
            records.append(
                LineRecord(
                    text=text,
                    x=first.x,
                    y=first.y,
                    font_name=first.font_name,
                    font_size=first.font_size,
                    runs=run_records,
                    x_end=max(run.x1 for run in runs),
                )
            )
            pending_space = not broke if self.literal_spaces == "break" else True
            pending_values.append(pending_space)
        return records, pending_values

    def _merge_run_records(
        self,
        records: list[RunRecord],
        source_runs: list[_CharacterRun],
    ) -> list[RunRecord]:
        """Coalesce size-jitter runs while preserving old rendered spacing.

        ``engine.append`` strips each original run and inserts at most one
        leading space from ``space_before``. Rebuilding merged text with those
        same rules prevents literal-space glyphs from introducing new or lost
        whitespace. The first physical run is deliberately kept separate
        because line-start rules may treat it specially.
        """

        if len(records) < 3 or not any(
            run.merge_previous for run in source_runs[1:]
        ):
            return records

        groups: list[list[RunRecord]] = [[records[0]]]
        protected = [False]
        for index, (record, source) in enumerate(
            zip(records[1:], source_runs[1:]), start=1
        ):
            group = groups[-1]
            anchor = group[0]
            if (
                source.merge_previous
                and len(groups) > 1
                and not protected[-1]
                and anchor.font_name == record.font_name
                and abs(anchor.font_size - record.font_size)
                <= self.font_size_tolerance
                and abs(anchor.y - record.y) <= self.baseline_tolerance
            ):
                group.append(record)
            else:
                groups.append([record])
                # The first run after a font/style transition must remain a
                # separate append operation. The cosmetic stack closes the old
                # style on that run and opens the new style on the next one.
                protected.append(
                    source.fontname != source_runs[index - 1].fontname
                )

        merged: list[RunRecord] = []
        for group in groups:
            if len(group) == 1:
                merged.append(group[0])
                continue
            visible = [(run, run.text.strip()) for run in group if run.text.strip()]
            if not visible:
                merged.append(group[0])
                continue
            parts: list[str] = []
            for run, text in visible:
                if parts and run.space_before:
                    parts.append(" ")
                parts.append(text)
            first, _text = visible[0]
            merged.append(
                RunRecord(
                    text="".join(parts),
                    x=first.x,
                    y=first.y,
                    font_name=first.font_name,
                    font_size=first.font_size,
                    space_before=first.space_before,
                )
            )
        return merged


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
        preserve_word_runs: bool = False,
        line_filter: LineFilter | None = None,
        stop_before: StopTest | None = None,
        page_enricher: PageEnricher | None = None,
        line_enricher: LineEnricher | None = None,
        page_error_handler: PageErrorHandler | None = None,
    ):
        self.pages = pages
        self.x_tolerance = x_tolerance
        self.y_tolerance = y_tolerance
        self.line_tolerance = line_tolerance
        self.word_gap = word_gap
        self.join_line_end_hyphens = join_line_end_hyphens
        self.preserve_word_runs = preserve_word_runs
        self.line_filter = line_filter
        self.stop_before = stop_before
        self.page_enricher = page_enricher
        self.line_enricher = line_enricher
        self.page_error_handler = page_error_handler

    def __call__(self, filename: str) -> Iterator[Chunk]:
        import pdfplumber

        previous_run: PDFChunk | None = None
        pending_space = False
        with pdfplumber.open(filename) as pdf:
            for page_num, page in enumerate(pdf.pages):
                if page_num < self.pages.start:
                    continue
                if self.pages.end is not None and page_num >= self.pages.end:
                    break
                try:
                    context = PDFPageContext(
                        page_num=page_num,
                        width=float(page.width),
                        height=float(page.height),
                    )
                    words = cast(
                        list[ExtractedWord],
                        page.extract_words(
                            x_tolerance=self.x_tolerance,
                            y_tolerance=self.y_tolerance,
                            keep_blank_chars=False,
                            use_text_flow=True,
                            extra_attrs=["fontname", "size"],
                        ),
                    )
                    records = [
                        self._record(context.height, words_on_line)
                        for words_on_line in self._group_words(words)
                        if words_on_line
                    ]
                    records = [record for record in records if record.text]
                    if self.page_enricher is not None:
                        self.page_enricher(context, records)
                except Exception as error:
                    if self.page_error_handler is None:
                        raise
                    self.page_error_handler(page_num, error)
                    pending_space = True
                    continue

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
                    flow_previous_run = previous_run
                    flow_pending_space = pending_space
                    text = record.text
                    hyphenated = (
                        not record.metadata.get("out_of_flow")
                        and self.join_line_end_hyphens
                        and text.endswith(("-", "‐", "‑"))
                    )
                    if hyphenated:
                        text = text[:-1]
                    source_runs = (
                        record.runs
                        if self.preserve_word_runs
                        else [
                            RunRecord(
                                text=text,
                                x=record.x,
                                y=record.y,
                                font_name=record.font_name,
                                font_size=record.font_size,
                                space_before=pending_space,
                            )
                        ]
                    )
                    if self.preserve_word_runs and hyphenated and source_runs:
                        source_runs[-1].text = source_runs[-1].text[:-1]
                    run_chunks: list[PDFChunk] = []
                    for run_index, run in enumerate(source_runs):
                        previous_run = make_chunk(
                            text=run.text,
                            x=run.x,
                            y=run.y,
                            font_name=run.font_name,
                            size=run.font_size,
                            previous=previous_run,
                            page_num=page_num,
                            space_before=(
                                pending_space if run_index == 0 else run.space_before
                            ),
                            page_context=context,
                        )
                        run_chunks.append(previous_run)
                    if not run_chunks:
                        continue
                    line_chunk = make_chunk(
                        text=text,
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
                    pending_space = not hyphenated
                    if record.metadata.get("out_of_flow"):
                        previous_run = flow_previous_run
                        pending_space = flow_pending_space

    def _group_words(self, words: list[ExtractedWord]) -> list[list[ExtractedWord]]:
        lines: list[list[ExtractedWord]] = []
        current: list[ExtractedWord] = []
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

    def _record(self, page_height: float, words: list[ExtractedWord]) -> LineRecord:
        parts: list[str] = []
        runs: list[RunRecord] = []
        previous = None
        for word in words:
            spaced = (
                previous is not None
                and float(word["x0"]) - float(previous["x1"]) > self.word_gap
            )
            if spaced:
                parts.append(" ")
            word_text = str(word["text"])
            parts.append(word_text)
            word_size = float(word["size"])
            word_font = str(word["fontname"])
            word_y = page_height - float(word["bottom"])
            if (
                self.preserve_word_runs
                and runs
                and runs[-1].font_name == word_font
                and abs(runs[-1].font_size - word_size) < 0.05
                and abs(runs[-1].y - word_y) < 0.75
            ):
                if spaced:
                    runs[-1].text += " "
                runs[-1].text += word_text
            elif self.preserve_word_runs:
                runs.append(
                    RunRecord(
                        word_text,
                        float(word["x0"]),
                        word_y,
                        word_font,
                        word_size,
                        space_before=spaced,
                    )
                )
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
        if not self.preserve_word_runs:
            runs = [RunRecord(text, x, y, font, size)]
        return LineRecord(
            text,
            x,
            y,
            font,
            size,
            runs,
            x_end=max(float(word["x1"]) for word in words),
        )
