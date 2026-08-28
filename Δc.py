#!/usr/bin/env python3

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from collections import namedtuple
from difflib import SequenceMatcher
from functools import cache
from functools import partial
from itertools import takewhile
from operator import attrgetter
from pathlib import Path

FG_BOLD = "\x1b[1m"
FG_GREEN = "\x1b[32m"
FG_BLUE = "\x1b[34m"
BG_PURPLE = "\x1b[48;2;190;155;255m"
RESET = "\x1b[m"

COLOURS = {
    "+": BG_PURPLE,
    "+++": f"{FG_BLUE}{FG_BOLD}",
    "---": f"{FG_BLUE}{FG_BOLD}",
    "@@": f"{FG_GREEN}{FG_BOLD}",
}

CONTEXT_LEN = 3
SOURCE_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?P<lineno>\d+)\|\s*(?P<count>[^|\s]*)\s*\|)(?P<text>.*)$",
)
NUMBER = r"\d+(?:\.\d+)?\w?"
REGION_COVERAGE_ANNOTATION = re.compile(fr"\^(?P<count>{NUMBER})")
REGION_COVERAGE_LINE = re.compile(fr"^\s*(?:{REGION_COVERAGE_ANNOTATION.pattern}\s*)*$")
BRANCH_COVERAGE_LINE = re.compile(
    fr"^\s*\|  Branch \(\d+:\d+\): \[True: (?P<true>{NUMBER}), False: (?P<false>{NUMBER})\]$",
)

Line = namedtuple("Line", "line, prefix, lineno, count, is_covered, text")
RegionCoverageLine = namedtuple("RegionCoverageLine", "line, text, counts, is_covered")
BranchCoverageLine = namedtuple("BranchCoverageLine", "line, text, is_covered")


class CompareBy(namedtuple("CompareBy", "keys, item")):
    def __eq__(self, other):
        if not isinstance(other, CompareBy) or self.keys != other.keys:
            return NotImplemented
        key = attrgetter(*self.keys)
        return key(self.item) == key(other.item)

    def __hash__(self):
        return hash(attrgetter(*self.keys)(self.item))


class DiffLine(namedtuple("DiffLine", "marker, line, tag")):
    def __str__(self):
        return f"{self.marker}{self.line.line}"


class HeaderLine(namedtuple("HeaderLine", "marker, line")):
    def __str__(self):
        return f"{self.marker} {self.line}"


class Highlighter:
    def __init__(self, enable_github_workaround, files):
        self.enable_github_workaround = enable_github_workaround
        self.files = files

    @cache
    def cached_highlight(self, filename):
        from pygments import highlight
        from pygments.formatters import TerminalTrueColorFormatter
        from pygments.lexers import guess_lexer_for_filename
        from pygments.lexers.special import TextLexer
        from pygments.util import ClassNotFound

        class Formatter(TerminalTrueColorFormatter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

                self.style_string = {
                    token_type: (start, end.replace("00", "22;23;24"))
                    for token_type, (start, end)
                    in self.style_string.items()
                }
                # remove whitespace highlighting because the workaround would remove it anyways
                del self.style_string['Token.Text.Whitespace']

        src = "\n".join(line.text for line in self.files[filename] if isinstance(line, Line))
        try:
            lexer = guess_lexer_for_filename(filename, src, stripnl=False)
        except ClassNotFound:
            lexer = TextLexer(stripnl=False)
        highlighted = highlight(src, lexer, Formatter())
        if self.enable_github_workaround:
            # work around the GitHub Actions log viewer removing whitespace that is
            # surrounded by ANSI escapes when a background colour is set (?) by
            # swapping non-newline whitespace with preceding escapes
            highlighted = re.sub(r"(\x1b\[(?:\d+;)*\d+m)+([^\S\n]+)", r"\2\1", highlighted)
        return highlighted.splitlines()

    def highlight(self, filename, diff_line):
        line = diff_line.line
        highlighted = self.cached_highlight(filename)[line.lineno - 1]
        return f"{diff_line.marker}{line.prefix}{highlighted}"


def parse(lines):
    files = defaultdict(list)
    filename = Path("/<unknown file>")
    for line in lines:
        line = line.rstrip()
        if line.startswith("/") and line.endswith(":"):
            filename = Path(line.removesuffix(":"))
        else:
            match = SOURCE_LINE_RE.fullmatch(line)
            if match is not None:
                files[filename].append(
                    Line(
                        line=match[0],
                        prefix=match["prefix"],
                        lineno=int(match["lineno"]),
                        count=match["count"],
                        is_covered=match["count"] != "0",
                        text=match["text"],
                    ),
                )
            elif REGION_COVERAGE_LINE.fullmatch(line):
                counts = tuple(m["count"] for m in REGION_COVERAGE_ANNOTATION.finditer(line))
                is_covered = tuple(count != "0" for count in counts)
                files[filename].append(
                    RegionCoverageLine(
                        line=line,
                        text=files[filename][-1].text,
                        counts=counts,
                        is_covered=is_covered,
                    ),
                )
            else:
                match = BRANCH_COVERAGE_LINE.match(line)
                if match is not None:
                    is_covered = match["true"] != "0", match["false"] != "0"
                    files[filename].append(
                        BranchCoverageLine(
                            line=line,
                            text=files[filename][-1].text,
                            is_covered=is_covered,
                        ),
                    )

    return files


def context(lines):
    return takewhile(lambda line: line.marker == " ", lines)


def is_fully_covered(line):
    match line:
        case Line():
            return line.is_covered
        case RegionCoverageLine() | BranchCoverageLine():
            return all(line.is_covered)


def diff(filename, left, right):
    comparer = partial(CompareBy, ("text", "is_covered"))
    matcher = SequenceMatcher(
        None,
        list(map(comparer, left)),
        list(map(comparer, right)),
    )

    show_header = True
    for opcodes in matcher.get_grouped_opcodes(n=CONTEXT_LEN):
        lines = []
        for tag, l_from, l_to, r_from, r_to in opcodes:
            if tag == "equal":
                for line in right[r_from:r_to]:
                    lines.append(DiffLine(marker=" ", line=line, tag=tag))
            elif tag in {"replace", "insert"}:
                for line in right[r_from:r_to]:
                    marker = " " if is_fully_covered(line) else "+"
                    lines.append(DiffLine(marker=marker, line=line, tag=tag))

        start_context = sum(1 for _ in context(lines))
        start_offset = max(0, start_context - CONTEXT_LEN)
        end_context = sum(1 for _ in context(reversed(lines)))
        end_offset = max(0, end_context - CONTEXT_LEN)

        lines = lines[start_offset:len(lines) - end_offset]
        if not lines:
            continue

        if show_header:
            show_header = False
            yield HeaderLine(marker="---", line=f"a/{filename}")
            yield HeaderLine(marker="+++", line=f"b/{filename}")

        _, left_start, _, right_start, _ = opcodes[0]

        yield HeaderLine(
            marker="@@",
            line=f"-{left_start + 1},1 +{right_start + start_offset + 1},{len(lines)} @@",
        )
        yield from lines


def check_output(*args, **kwargs):
    return subprocess.check_output(*args, **kwargs, text=True)


def git(repo):
    return ["git", "-C", repo]


def git_switch(repo, ref):
    subprocess.check_call([*git(repo), "checkout", ref])


def run_in(repo, ref, command, *args, **kwargs):
    try:
        git_switch(repo, ref)
        return check_output(command, *args, **kwargs)
    finally:
        git_switch(repo, "-")


def get_coverage(repo, ref, command):
    return run_in(repo, ref, command, cwd=repo).splitlines()


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
    )
    parser.add_argument(
        "--enable-github-workaround",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "-C", "--git-repo",
        type=lambda arg: Path(arg).resolve(),
        default=Path.cwd(),
    )
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("command", nargs="+")
    args = parser.parse_args(args)

    with_colours = sys.stdout.isatty() if args.color == "auto" else args.color == "always"

    merge_base = check_output([*git(args.git_repo), "merge-base", args.left, args.right]).strip()

    left = parse(get_coverage(args.git_repo, merge_base, args.command))
    right = parse(get_coverage(args.git_repo, args.right, args.command))

    highlighter = Highlighter(args.enable_github_workaround, right)

    diff_is_empty = True

    for filename in sorted(left.keys() | right.keys()):
        lines = diff(
            filename.relative_to(args.git_repo, walk_up=True),
            left.get(filename, []),
            right.get(filename, []),
        )
        for line in lines:
            diff_is_empty = False
            colour = COLOURS.get(getattr(line, "marker", None))
            should_colourise = with_colours and colour is not None
            colour, reset = (colour, RESET) if should_colourise else ("", "")
            if with_colours and isinstance(line.line, Line):
                line = highlighter.highlight(filename, line)
            print(f"{colour}{line}{reset}")

    if diff_is_empty:
        print(f"\n{FG_BOLD}{FG_GREEN}==> no additional uncovered lines!{RESET}\n")


if __name__ == "__main__":
    main()
