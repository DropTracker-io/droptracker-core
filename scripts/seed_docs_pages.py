"""Seed ``docs_pages`` from the original static `.mdx` files (backend Task 15/16).

The docs CMS replaces `apps/web/content/docs/*.mdx` (frontmatter + Markdown,
read at build time) with a DB table editable from `/admin/docs`. This is a
one-time migration of the 9 existing docs pages so nothing is lost when the
public `/docs` pages switch from the filesystem loader to the API.

Idempotent — skips slugs that already exist (safe to re-run; edit content
through `/admin/docs` afterward, not by re-running this script).

Run:
    venv/bin/python -m scripts.seed_docs_pages            # apply
    venv/bin/python -m scripts.seed_docs_pages --dry-run  # preview
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from db.models import DocsPage, session

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "apps" / "web" / "content" / "docs"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse_mdx(raw: str) -> tuple[dict, str]:
    """Minimal frontmatter parser — the existing docs only use flat
    `key: value` pairs (no lists/nesting), so a full YAML parser (an
    otherwise-unneeded dependency for this one-off script) isn't worth adding."""
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    data = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data, m.group(2).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed docs_pages from content/docs/*.mdx.")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = ap.parse_args()

    if not DOCS_DIR.is_dir():
        print(f"No docs directory found at {DOCS_DIR}")
        return 1

    files = sorted(DOCS_DIR.glob("*.mdx"))
    if not files:
        print(f"No .mdx files found under {DOCS_DIR}")
        return 0

    created = 0
    skipped = 0
    for f in files:
        slug = f.stem
        existing = session.query(DocsPage).filter(DocsPage.slug == slug).first()
        if existing:
            print(f"  skip  {slug} (already exists)")
            skipped += 1
            continue

        data, body = _parse_mdx(f.read_text(encoding="utf-8"))
        title = str(data.get("title") or slug)
        description = str(data["description"]) if data.get("description") else None
        category = str(data.get("category") or "General")
        order = int(data.get("order") or 100)

        print(f"  {'would create' if args.dry_run else 'create'}  {slug}  ({title!r}, {category}, order={order})")
        if not args.dry_run:
            session.add(DocsPage(
                slug=slug, title=title, description=description,
                category=category, order=order, body_md=body,
            ))
            created += 1

    if not args.dry_run and created:
        session.commit()

    print(f"\n{'Would create' if args.dry_run else 'Created'} {created if not args.dry_run else len(files) - skipped}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
