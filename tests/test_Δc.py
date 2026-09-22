import pytest

import Δc

DEFAULT_LINE = Δc.Line(line=None, prefix=None, lineno=None, count=None, is_covered=None, text=None)


def make_diffable(lines):
    return [DEFAULT_LINE._replace(text=line, is_covered=True) for line in lines]


@pytest.mark.parametrize(
    "left",
    [
        [0, 1, 2, 3, 4, 6, 7, 8, 9],
        [0, 2, 3, 4, 5, 6, 7, 8, 9],
        range(1, 10),
        range(9),
        [0, 1, 2, 3, 4, 5, 6, 7, 9],
    ],
)
def test_diff_extraneous_context_removal_when_all_covered(left):
    left = make_diffable(left)
    right = make_diffable(range(10))
    assert list(Δc.diff("filename", left, right)) == []
