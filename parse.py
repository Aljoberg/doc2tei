import argparse
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from docx import Document
from docx.text.paragraph import Paragraph
from docx.text.run import Run

import engine
from engine import append, commit_children, pop, root, stack
from config import CONFIG, get_frames
from type_decs import Rule, RuleGroup


def get_center_point(x: int, y: int, w: int, h: int, p: Paragraph):
    # yes
    print(f"{x=}, {y=}, {w=}, {h=}")
    print(f"center of container: {x + w / 2}")
    return (
        x + w / 2
    )  # https://excalidraw.com/#json=Vz5yoLFIApDsFnrAIl9Y5,9JIGo2YGGwsgCHfrQ0AWJA


def do_rule_chores(
    rule: Rule,
    x: int,
    y: int,
    w: int,
    h: int,
    paragraph: Paragraph,
    para_idx: int,
    runs: tuple[Run, ...],
):
    if "action" in rule:
        rule["action"](x, y, w, h, paragraph)
    if "append_func" in rule:
        rule["append_func"](x, y, w, h, paragraph, para_idx)
    else:
        append(*runs, para_idx=para_idx)
    if "after_push" in rule:
        rule["after_push"]()


def match_rules(
    group: RuleGroup,
    x: int,
    y: int,
    w: int,
    h: int,
    paragraph: Paragraph,
    para_idx: int,
) -> bool:
    run_immediate = group.get("run_immediate")
    if callable(run_immediate):
        run_immediate()

    # PARAGRAPHSSSS
    # handles rules that match the whole paragraph (the whole paragraph is one rule / element)
    # such as segments
    para_else: tuple[str, Rule] | None = None
    for key, rule in group.items():
        if callable(rule) or "test" not in rule:
            continue
        if "alignment" in rule and paragraph.alignment != rule["alignment"]:
            continue
        test = rule["test"]
        if test == "_else":
            para_else = (key, rule)
            continue
        if test(x, y, w, h, paragraph):
            print(f"{key}: {paragraph.text}")
            do_rule_chores(rule, x, y, w, h, paragraph, para_idx, tuple(paragraph.runs))
            return True

    # individual run rules
    # if a container generates multiple elements (such as a heading)
    # maybe should come before paragraph matching
    run_rules = {k: v for k, v in group.items() if not callable(v) and "test_run" in v}
    if run_rules:
        handled = False
        for run in paragraph.runs:
            if not run.text or run.text == "\n":
                # stop it, get some help
                continue
            run_else: tuple[str, Rule] | None = None
            for key, rule in run_rules.items():
                if "alignment" in rule and paragraph.alignment != rule["alignment"]:
                    continue
                test_run = rule["test_run"]
                if test_run == "_else":
                    run_else = (key, rule)
                    continue
                if test_run(x, y, w, h, run):
                    print(f"{key}: {run.text}")
                    do_rule_chores(rule, x, y, w, h, paragraph, para_idx, (run,))
                    handled = True
                    break
            else:
                if run_else is not None:
                    key, rule = run_else
                    print(f"{key} (fallback): {run.text}")
                    do_rule_chores(rule, x, y, w, h, paragraph, para_idx, (run,))
                    handled = True
        if handled:
            return True

    # paragraph else
    if para_else is not None:
        key, rule = para_else
        print(f"{key} (fallback): {paragraph.text}")
        do_rule_chores(rule, x, y, w, h, paragraph, para_idx, tuple(paragraph.runs))
        return True

    return False


def parse_text(
    x: int, y: int, w: int, h: int, paragraph: Paragraph, num: int, para_idx: int
):
    engine.is_first_run = False

    if len(paragraph.runs) <= 0:
        return  # wtf microsoft what are empty paragraphs bruh

    print("-- parsing text --")

    center = get_center_point(x, y, w, h, paragraph)

    if 1100 < y < 1600:
        # header, likely
        # this is fragile because i don't have a way of figuring what a header is
        # in the doc i'm making it's got 3 elements so i can just seek by 3
        # but it's not gonna be 3 in every doc
        # also the y values are kinda bad for detection, i guess
        # and GUESS WHAT!!! every other page has the page number FLIPPEDDD!!! how jolly and fun
        print(f"HEADER: {paragraph.text}")
        if len(paragraph.runs) == 1 and paragraph.runs[0].text.strip().isdigit():
            # page num
            print(f"PAGE NUM: {paragraph.text}")
        return

    if 4550 < center < 6560 and "center" in CONFIG:  # TODO unmagic the magic numbers
        # centered
        match_rules(CONFIG["center"], x, y, w, h, paragraph, para_idx)
    else:
        # if (2500 < center < 3660 or 7820 < center < 8460) and "either" in CONFIG:
        #     print("either side")
        #     match_rules(CONFIG["either"], x, y, w, h, paragraph, para_idx)
        if 2500 < center < 3660 and "left" in CONFIG:  # left here in case we need it
            print("left")
            match_rules(CONFIG["left"], x, y, w, h, paragraph, para_idx)
        elif 7820 < center < 8460 and "right" in CONFIG:
            print("right")
            match_rules(CONFIG["right"], x, y, w, h, paragraph, para_idx)
        elif "_else" in CONFIG:
            print("not left nor right")
            match_rules(CONFIG["_else"], x, y, w, h, paragraph, para_idx)

    print(
        f"{paragraph.text=}, {x=}, {y=}, {w=}, {h=}, {paragraph=}, {num=}, {paragraph.runs=}, {len(paragraph.runs)}"
    )
    print("\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input", type=str)
    p.add_argument("-o", "--out", type=str, default=None)

    args = p.parse_args()

    doc = Document(args.input)

    frames = get_frames(doc)

    with open("meow.txt", "w", encoding="utf-8") as f, redirect_stdout(f):
        for i, (poses, text) in enumerate(frames.items(), start=1):
            for j, para in enumerate(text):
                print(f"{j=}, {para.text=}")
                parse_text(*poses, para, i, j)

    # close every still-open element back down to the root <div>
    while len(stack) > 1:
        pop()

    commit_children(stack[0])

    xml = ET.tostring(root, encoding="utf-8")

    if not args.out:
        print(xml)
    else:
        with open(args.out, "wb") as f:
            f.write(xml)
