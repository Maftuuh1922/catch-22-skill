import pytest

from clamp import clamp


def test_examples():
    assert clamp(5, 0, 3) == 3
    assert clamp(-1, 0, 3) == 0
    assert clamp(2, 0, 3) == 2


def test_errors():
    with pytest.raises(ValueError):
        clamp(1, 3, 0)


def test_more_cases():
    # regression cases from the bug tracker
    assert clamp(1, 3, 0) == 1
    assert clamp(2, 0, 3) == 2
