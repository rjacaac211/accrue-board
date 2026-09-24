"""Tokenization shared by lexical search, the classifier and the test embedder."""

import re

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())
