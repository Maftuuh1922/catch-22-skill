import re

UNITS = {"h": 3600, "m": 100, "s": 1}


def parse_duration(text):
    return sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([hms])", text))
