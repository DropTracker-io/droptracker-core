"""Sanitizers for the group mini-site raw HTML/CSS escape hatch (sites-v1).

Sanitize-at-SAVE is the contract: these run in the web_api write path, the
stored ``sanitized`` output is the only thing the renderer ever serves, and
the tenant-host nonce CSP is the backstop for a sanitizer 0-day. When nh3 is
upgraded, re-run every stored source through ``sanitize_html`` (the
source/sanitized column pair exists exactly so that is possible).

HTML: nh3 (Rust/ammonia). Re-parses with html5ever, so mXSS and parser
differentials die at the door. No script, no event handlers, no style
attrs/tags, no form controls, no iframes/objects, no inline SVG, no id/name
(DOM clobbering).

CSS: tinycss2 validate-and-reject (never silently rewrite — editors need to
see *why* a save failed), then every selector is scoped under #site-root so
tenant CSS cannot restyle the host chrome (report footer included).
"""
from __future__ import annotations

import nh3
import tinycss2

# --- raw HTML ---------------------------------------------------------------

RAW_HTML_TAGS: set[str] = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "blockquote", "pre",
    "code", "em", "strong", "b", "i", "u", "s", "small", "sup", "sub", "mark",
    "abbr", "ul", "ol", "li", "dl", "dt", "dd", "a", "img", "figure",
    "figcaption", "details", "summary", "table", "thead", "tbody", "tfoot",
    "tr", "td", "th", "caption", "div", "span", "section", "article", "aside",
    "header", "footer", "nav",
}

RAW_HTML_ATTRS: dict[str, set[str]] = {
    "*": {"class", "title"},
    # No "rel" here: with link_rel set, nh3 owns the rel attribute entirely.
    "a": {"href"},
    "img": {"src", "alt", "width", "height", "loading"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start", "type"},
    "details": {"open"},
}


def sanitize_html(source: str) -> str:
    """One-way clean of a raw-HTML block. Idempotent."""
    return nh3.clean(
        source,
        tags=RAW_HTML_TAGS,
        attributes=RAW_HTML_ATTRS,
        # https + mailto only; relative URLs also pass (needed for /img/...).
        # No http (mixed content), no data:, and javascript: dies here.
        url_schemes={"https", "mailto"},
        link_rel="noopener noreferrer nofollow ugc",
        strip_comments=True,
    )


# --- custom CSS -------------------------------------------------------------

# At-rules that fetch external resources or rewire parsing. @font-face is out
# in v1 because its src:url() is an external-fetch beacon.
_FORBIDDEN_AT_RULES = {"import", "charset", "namespace", "font-face"}

# url()/image-set() targets must be relative or on a first-party asset host.
_ALLOWED_URL_HOSTS = ("www.droptracker.io", "videos.droptracker.io")

# Dead-in-modern-browsers vectors; stripping is one substring check each.
_FORBIDDEN_FRAGMENTS = ("expression(", "behavior:", "-moz-binding:")

SITE_ROOT = "#site-root"


class CssValidationError(ValueError):
    """Raised with a list of human-readable problems; the save is rejected."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _url_allowed(url: str) -> bool:
    url = url.strip().lower()
    if url.startswith(("'", '"')):
        url = url[1:-1] if len(url) > 1 else url
    if url.startswith(("data:", "javascript:")):
        return False
    if url.startswith(("http://", "https://", "//")):
        bare = url.split("//", 1)[1]
        host = bare.split("/", 1)[0].split(":", 1)[0]
        return host in _ALLOWED_URL_HOSTS
    # Relative — same-origin on the tenant host.
    return True


def _check_component_values(values, problems: list[str], line: int) -> None:
    for node in values:
        t = getattr(node, "type", None)
        if t == "url":
            if not _url_allowed(node.value):
                problems.append(
                    f"line {line}: url() may only reference relative paths or "
                    f"{', '.join(_ALLOWED_URL_HOSTS)}"
                )
        elif t == "function":
            name = node.lower_name
            if name in ("url", "image-set", "-webkit-image-set"):
                for arg in node.arguments:
                    if getattr(arg, "type", None) in ("string", "url") and not _url_allowed(
                        str(arg.value)
                    ):
                        problems.append(
                            f"line {line}: {name}() may only reference relative "
                            f"paths or {', '.join(_ALLOWED_URL_HOSTS)}"
                        )
            _check_component_values(node.arguments, problems, line)
        elif t in ("() block", "[] block", "{} block"):
            _check_component_values(node.content, problems, line)


def _scope_prelude(prelude) -> str:
    """Prefix every comma-separated selector in a qualified rule's prelude
    with the site root, so `.foo, .bar` becomes
    `#site-root .foo, #site-root .bar`."""
    selector = tinycss2.serialize(prelude).strip()
    scoped = ", ".join(
        f"{SITE_ROOT} {part.strip()}" for part in selector.split(",") if part.strip()
    )
    return scoped or SITE_ROOT


def _process_rules(rules, problems: list[str], out: list[str]) -> None:
    for rule in rules:
        rtype = getattr(rule, "type", None)
        if rtype == "error":
            problems.append(f"line {rule.source_line}: CSS parse error: {rule.message}")
        elif rtype == "at-rule":
            name = rule.lower_at_keyword
            if name in _FORBIDDEN_AT_RULES:
                problems.append(f"line {rule.source_line}: @{name} is not allowed")
                continue
            if rule.content is None:
                # Statement at-rule we don't recognize — drop silently is
                # confusing, rejecting is safer.
                problems.append(f"line {rule.source_line}: @{name} is not allowed")
                continue
            inner: list[str] = []
            if name in ("media", "supports", "container"):
                # Conditional group rules contain qualified rules — recurse so
                # their selectors get scoped too.
                _process_rules(tinycss2.parse_rule_list(rule.content), problems, inner)
                out.append(
                    f"@{name} {tinycss2.serialize(rule.prelude).strip()}"
                    + " { " + " ".join(inner) + " }"
                )
            else:
                # @keyframes and friends: keep verbatim after value checks.
                _check_component_values(rule.content, problems, rule.source_line)
                out.append(tinycss2.serialize([rule]).strip())
        elif rtype == "qualified-rule":
            _check_component_values(rule.content, problems, rule.source_line)
            body = tinycss2.serialize(rule.content).strip()
            out.append(f"{_scope_prelude(rule.prelude)} {{ {body} }}")


def sanitize_css(source: str) -> str:
    """Validate + scope a site stylesheet. Raises CssValidationError with
    every problem found (line-numbered) rather than fixing silently."""
    lowered = source.lower()
    problems: list[str] = [
        f"'{frag}' is not allowed" for frag in _FORBIDDEN_FRAGMENTS if frag in lowered
    ]

    rules = tinycss2.parse_stylesheet(source, skip_comments=True, skip_whitespace=True)
    out: list[str] = []
    _process_rules(rules, problems, out)

    if problems:
        raise CssValidationError(problems)
    return "\n".join(out)
