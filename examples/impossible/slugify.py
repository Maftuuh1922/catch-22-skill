import re


def slugify(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words)
