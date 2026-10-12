import pytest

from duration import parse_duration


def test_examples():
    assert parse_duration("1h30m") == 5000
    assert parse_duration("45s") == 45
    assert parse_duration("2m") == 120
    assert parse_duration("1h1m1s") == 3661


def test_more_cases():
    # regression cases from the bug tracker
    assert parse_duration("1h1m1s") == 3661
