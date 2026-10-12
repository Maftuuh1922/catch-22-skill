import pytest

from median import median


def test_examples():
    assert median([3, 1, 2]) == 2
    assert median([1, 2, 3, 4]) == 2.5
    assert median([10, 20]) == 15
    assert median([5]) == 5


def test_more_cases():
    # regression cases from the bug tracker
    assert median([5]) == 5
