import re


def slugify(text):
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "-".join(words)
