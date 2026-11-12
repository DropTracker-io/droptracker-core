"""Read a JavaScript/TypeScript object or array literal as Python data.

Just enough to read the rate tables Wise Old Man publishes as ``.ts`` files
(``export default [ { skill: Skill.ATTACK, methods: [...] } ]``) without
running them: we never execute code we downloaded.

Understands: objects, arrays, single and double quoted strings, numbers
(with ``_`` separators), ``true`` / ``false`` / ``null`` / ``undefined``,
unquoted keys, ``[Enum.MEMBER]`` computed keys, trailing commas, comments.
A dotted identifier used as a value (``Skill.ATTACK``) comes back as the
string ``"Skill.ATTACK"`` for the caller to resolve. Anything else (a
function call, a spread, a template string) raises `ValueError`.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

_NUMBER = re.compile(r"-?(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "/": "/"}
_WORDS = {"true": True, "false": False, "null": None, "undefined": None}


class _Reader:
    def __init__(self, text: str):
        self.text, self.pos = text, 0

    def fail(self, what: str):
        line = self.text.count("\n", 0, self.pos) + 1
        raise ValueError(f"line {line}: {what}")

    def skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            if text[self.pos].isspace():
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos)
                if end < 0:
                    self.fail("unterminated comment")
                self.pos = end + 2
            else:
                return

    def peek(self) -> str:
        self.skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            self.fail(f"expected {ch!r}")
        self.pos += 1

    def string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: List[str] = []
        while True:
            if self.pos >= len(self.text):
                self.fail("unterminated string")
            ch = self.text[self.pos]
            self.pos += 1
            if ch == quote:
                return "".join(out)
            if ch == "\\":
                nxt = self.text[self.pos]
                self.pos += 1
                if nxt == "u":
                    out.append(chr(int(self.text[self.pos:self.pos + 4], 16)))
                    self.pos += 4
                else:
                    out.append(_ESCAPES.get(nxt, nxt))
            else:
                out.append(ch)

    def value(self) -> Any:
        ch = self.peek()
        if not ch:
            self.fail("unexpected end of text")
        if ch == "{":
            return self.obj()
        if ch == "[":
            return self.array()
        if ch and ch in "'\"":
            return self.string()
        match = _NUMBER.match(self.text, self.pos)
        if match and match.group():
            self.pos = match.end()
            raw = match.group().replace("_", "")
            return float(raw) if any(c in raw for c in ".eE") else int(raw)
        match = _IDENT.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            word = match.group()
            if self.peek() == "(":
                self.fail(f"function call {word}(...) is not data")
            return _WORDS[word] if word in _WORDS else word
        self.fail(f"unexpected {ch!r}")

    def key(self) -> str:
        ch = self.peek()
        if ch and ch in "'\"":
            return self.string()
        if ch == "[":
            self.pos += 1
            self.skip()
            match = _IDENT.match(self.text, self.pos)
            if not match:
                self.fail("unsupported computed key")
            self.pos = match.end()
            self.expect("]")
            return match.group()
        match = _IDENT.match(self.text, self.pos) or _NUMBER.match(self.text, self.pos)
        if not match or not match.group():
            self.fail("expected a key")
        self.pos = match.end()
        return match.group()

    def obj(self) -> dict:
        self.expect("{")
        out = {}
        while self.peek() != "}":
            if self.text.startswith("...", self.pos):
                self.fail("spread is not data")
            name = self.key()
            self.expect(":")
            out[name] = self.value()
            if self.peek() == ",":
                self.pos += 1
        self.pos += 1
        return out

    def array(self) -> list:
        self.expect("[")
        out = []
        while self.peek() != "]":
            if self.text.startswith("...", self.pos):
                self.fail("spread is not data")
            out.append(self.value())
            if self.peek() == ",":
                self.pos += 1
        self.pos += 1
        return out


def parse(text: str) -> Any:
    """Parse one literal. Trailing ``;`` and whitespace are allowed."""
    reader = _Reader(text)
    value = reader.value()
    reader.skip()
    if reader.peek() == ";":
        reader.pos += 1
    if reader.peek():
        reader.fail("unexpected text after the literal")
    return value


def find_literal(source: str, marker: str) -> Tuple[Any, int]:
    """Parse the literal that follows ``marker`` in a source file.

    ``find_literal(ts, "export default")`` reads a module's default export;
    ``find_literal(ts, "export const Skill =")`` an enum object. Returns the
    value and the offset just past it.
    """
    start = source.find(marker)
    if start < 0:
        raise ValueError(f"{marker!r} not found")
    reader = _Reader(source)
    reader.pos = start + len(marker)
    value = reader.value()
    return value, reader.pos
