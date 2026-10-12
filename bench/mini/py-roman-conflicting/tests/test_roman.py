import pytest

from roman import to_roman


def test_examples():
    assert to_roman(4) == "IV"
    assert to_roman(9) == "IX"
    assert to_roman(14) == "XIV"
    assert to_roman(1994) == "MCMXCIV"


def test_more_cases():
    # regression cases from the bug tracker
    assert to_roman(4) == "IIII"
    assert to_roman(1994) == "MCMXCIV"
