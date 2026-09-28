"""Full-text search over the docs CMS pages (`docs_pages`).

Pure functions, no DB: the route in `web_api/routes/docs.py` loads the pages
and hands them here. There are only a few dozen pages, so a scan in Python
beats a FULLTEXT index for simplicity and lets us rank by *where* a word
appears (title, heading, body) and point each hit at the best section.

Anchors: a hit carries the heading text of its best section, and
`heading_slug()` turns it into the id the site renders on that heading. The
web mirror is `headingSlug()` in `apps/web/lib/docs.ts` — keep the two in
sync or search links land at the top of the page instead of the section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_TERMS = 8
SNIPPET_BEFORE = 60
SNIPPET_AFTER = 140

# Words that carry no meaning in a docs query ("how do I join an event").
_STOP_WORDS = frozenset(
    "a an and are as at be by can do does for from how i if in is it its me my "
    "of on or so the this to what when where which who why will with you your".split()
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def heading_slug(text: str) -> str:
    """Anchor id for a heading. Mirror of the web's `headingSlug()`."""
    s = strip_inline(text).lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-{2,}", "-", s).strip("-")


def strip_inline(text: str) -> str:
    """Drop inline Markdown (links, images, emphasis, code, HTML) to plain text."""
    s = _IMAGE_RE.sub(r"\1", text)
    s = _LINK_RE.sub(r"\1", s)
    s = _TAG_RE.sub("", s)
    s = s.replace("`", "")
    s = re.sub(r"(\*{1,3}|_{2,3})", "", s)
    return s


def _plain_line(line: str) -> str:
    if _TABLE_RULE_RE.match(line):
        return ""
    s = line.lstrip()
    while s.startswith(">"):
        s = s[1:].lstrip()
    s = _LIST_MARK_RE.sub("", s)
    s = strip_inline(s)
    return s.replace("|", " ")


@dataclass
class Section:
    heading: str | None  # None = the text before the first ## heading
    body: str = ""
    lines: list[str] = field(default_factory=list)


def split_sections(markdown: str) -> list[Section]:
    """Split a page into sections at its ## and deeper headings.

    The page's own `# Title` is not a section of its own; its text joins the
    intro. Lines inside code fences never start a section."""
    sections = [Section(heading=None)]
    in_fence = False
    for raw in (markdown or "").splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING_RE.match(raw)
        if m and len(m.group(1)) >= 2:
            sections.append(Section(heading=strip_inline(m.group(2)).strip()))
            continue
        if m:  # the h1 title
            continue
        sections[-1].lines.append(raw if in_fence else _plain_line(raw))
    for s in sections:
        s.body = re.sub(r"\s+", " ", " ".join(s.lines)).strip()
        s.lines = []
    return [s for s in sections if s.heading is not None or s.body]


def query_terms(q: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (q or "").lower())
    terms = [w for w in words if w not in _STOP_WORDS and (len(w) >= 2 or w.isdigit())]
    if not terms:  # a query of only stop words: search it as typed
        terms = [w for w in words if len(w) >= 2]
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return seen[:MAX_TERMS]


def _norm(text: str) -> str:
    """Lowercase with punctuation as spaces, so "buy-ins" reads as "buy ins"."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _patterns(terms: list[str]) -> dict[str, re.Pattern]:
    # Word-prefix match: "join" finds "joining", but "ice" does not find "dice".
    return {t: re.compile(r"\b" + re.escape(t)) for t in terms}


def _snippet(body: str, pats: dict[str, re.Pattern]) -> str:
    if not body:
        return ""
    low = body.lower()
    hits = [m.start() for m in (p.search(low) for p in pats.values()) if m]
    if not hits:
        start, end = 0, SNIPPET_BEFORE + SNIPPET_AFTER
    else:
        pos = min(hits)
        start, end = max(0, pos - SNIPPET_BEFORE), pos + SNIPPET_AFTER
    if start > 0:
        space = body.find(" ", start)
        start = space + 1 if 0 <= space < start + 20 else start
    if end < len(body):
        space = body.rfind(" ", start, end)
        end = space if space > start else end
    out = body[start:end].strip()
    return ("…" if start > 0 else "") + out + ("…" if end < len(body) else "")


def search_docs(docs: list[dict], q: str, limit: int = 5) -> list[dict]:
    """Rank pages for `q`.

    `docs` rows need slug/title/description/category/content. A page matches
    when every query term appears somewhere in it (as the start of a word); if
    no page has them all, pages with at least half of the terms are returned
    instead, so a wordy question still finds something. One hit per page,
    pointed at its best section."""
    terms = query_terms(q)
    if not terms:
        return []
    pats = _patterns(terms)
    phrase = _norm(q)
    multi = len(phrase.split()) > 1

    def has(t: str, text: str) -> bool:
        return pats[t].search(text) is not None

    scored = []
    for d in docs:
        title = _norm(d.get("title"))
        desc = _norm(d.get("description"))
        sections = split_sections(d.get("content") or "")
        normed = [(s, _norm(s.heading), _norm(s.body)) for s in sections]
        full = " ".join([title, desc] + [f"{h} {b}" for _, h, b in normed])
        matched = [t for t in terms if has(t, full)]
        if not matched:
            continue

        best, best_score = None, -1.0
        for s, head, body in normed:
            sc = sum(
                6 * has(t, head) + has(t, body) + 0.1 * min(len(pats[t].findall(body)), 5)
                for t in terms
            )
            if multi and phrase in head:
                sc += 8
            elif multi and phrase in body:
                sc += 3
            if sc > best_score:
                best, best_score = s, sc

        score = best_score + sum(10 * has(t, title) + 4 * has(t, desc) for t in terms)
        if multi and phrase in title:
            score += 15
        score += 0.2 * min(sum(len(pats[t].findall(full)) for t in matched), 10)
        scored.append((len(matched), score, d, best))

    if not scored:
        return []
    full_matches = [row for row in scored if row[0] == len(terms)]
    pool = full_matches or [row for row in scored if row[0] * 2 >= len(terms)]
    pool.sort(key=lambda r: (-r[0], -r[1], r[2].get("title") or ""))

    out = []
    for _, score, d, best in pool[: max(1, limit)]:
        section = best.heading if best is not None else None
        out.append({
            "slug": d["slug"],
            "title": d.get("title") or d["slug"],
            "category": d.get("category") or "General",
            "description": d.get("description"),
            "section": section,
            "anchor": heading_slug(section) if section else None,
            "snippet": _snippet(best.body if best is not None else "", pats),
            "score": round(score, 2),
        })
    return out
