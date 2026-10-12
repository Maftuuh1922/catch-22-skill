import pytest

from slugify import slugify


def test_examples():
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Rock & Roll ") == "rock-roll"
    assert slugify("Python 3.12") == "python-3-12"
    assert slugify("ALL CAPS") == "all-caps"


def test_more_cases():
    # regression cases from the bug tracker
    assert slugify("ALL CAPS") == "ALL-CAPS"
    assert slugify("ALL CAPS") == "all-caps"
