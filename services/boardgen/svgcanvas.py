"""A tiny dependency-free SVG writer with named layers.

Top-level layers become Inkscape layers (groupmode="layer" + label), so the
output opens as an editable, clearly-separated layer stack in Inkscape /
Illustrator, and renders as-is in any browser. Within a layer, every element
carries a class and (optionally) an id so individual tiles/icons are directly
selectable and editable.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _attrs(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if v is None:
            continue
        k = k.replace("_", "-")
        parts.append(f'{k}="{esc(v)}"')
    return " ".join(parts)


@dataclass
class Layer:
    key: str
    label: str
    elements: list[str] = field(default_factory=list)
    display: bool = True

    def add(self, element: str) -> None:
        self.elements.append(element)

    def render(self) -> str:
        style = "" if self.display else 'style="display:none" '
        head = (f'  <g inkscape:groupmode="layer" id="layer-{esc(self.key)}" '
                f'inkscape:label="{esc(self.label)}" {style}>')
        body = "\n".join("    " + e for e in self.elements)
        return f"{head}\n{body}\n  </g>"


class Canvas:
    def __init__(self, width: float, height: float, background: str | None = None):
        self.width = width
        self.height = height
        self.background = background
        self._defs: list[str] = []
        self._layers: dict[str, Layer] = {}
        self._order: list[str] = []

    # -- layers -----------------------------------------------------------
    def layer(self, key: str, label: str | None = None) -> Layer:
        if key not in self._layers:
            self._layers[key] = Layer(key, label or key.replace("-", " ").title())
            self._order.append(key)
        return self._layers[key]

    def add_def(self, element: str) -> None:
        self._defs.append(element)

    # -- primitives (return SVG strings; caller adds to a layer) ----------
    @staticmethod
    def polygon(points: list[tuple[float, float]], **kw) -> str:
        pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        return f'<polygon points="{pts}" {_attrs(kw)}/>'

    @staticmethod
    def path(d: str, **kw) -> str:
        return f'<path d="{esc(d)}" {_attrs(kw)}/>'

    @staticmethod
    def circle(cx: float, cy: float, r: float, **kw) -> str:
        return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" {_attrs(kw)}/>'

    @staticmethod
    def rect(x: float, y: float, w: float, h: float, **kw) -> str:
        return (f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" '
                f'height="{h:.2f}" {_attrs(kw)}/>')

    @staticmethod
    def text(x: float, y: float, content: str, **kw) -> str:
        return f'<text x="{x:.2f}" y="{y:.2f}" {_attrs(kw)}>{esc(content)}</text>'

    @staticmethod
    def group(inner: str, **kw) -> str:
        return f'<g {_attrs(kw)}>{inner}</g>'

    @staticmethod
    def use(href: str, x: float = 0, y: float = 0, **kw) -> str:
        return f'<use href="#{esc(href)}" x="{x:.2f}" y="{y:.2f}" {_attrs(kw)}/>'

    @staticmethod
    def image(href: str, x: float, y: float, w: float, h: float, **kw) -> str:
        return (f'<image href="{esc(href)}" x="{x:.2f}" y="{y:.2f}" '
                f'width="{w:.2f}" height="{h:.2f}" {_attrs(kw)}/>')

    # -- serialize --------------------------------------------------------
    def render(self) -> str:
        defs = "\n".join("    " + d for d in self._defs)
        layers = "\n".join(self._layers[k].render() for k in self._order)
        bg = ""
        if self.background:
            bg = (f'  <rect x="0" y="0" width="{self.width}" '
                  f'height="{self.height}" fill="{esc(self.background)}"/>')
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'xmlns:inkscape="{INKSCAPE_NS}" '
            f'xmlns:sodipodi="{SODIPODI_NS}" '
            f'width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}">\n'
            f'  <defs>\n{defs}\n  </defs>\n'
            f'{bg}\n'
            f'{layers}\n'
            f'</svg>\n'
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.render())
