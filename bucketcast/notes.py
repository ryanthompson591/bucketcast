"""Episode notes: Markdown in, safe HTML out.

Notes are stored as the Markdown the author typed. They are rendered to HTML
wherever they are shown: the admin preview now, the feed and show page later.

Raw HTML in notes is not passed through; it is shown as text. Together with
markdown-it's link checks (no ``javascript:`` and the like) this keeps the
output safe to publish without a separate sanitiser.
"""

from __future__ import annotations

from collections.abc import Callable

from markdown_it import MarkdownIt

ASSET_SCHEME = "asset:"

_md = MarkdownIt(
    "commonmark",
    {
        "html": False,  # escape raw HTML instead of emitting it
        "breaks": True,  # a single newline is a line break, as authors expect
        "linkify": True,  # bare URLs become links
    },
).enable("linkify")


def render_notes(text: str, asset_url: Callable[[str], str] | None = None) -> str:
    """Render Markdown notes to HTML.

    ``asset_url`` maps an asset name to its public URL; it is used for images
    written as ``![alt](asset:name.jpg)``. Unknown assets get an empty ``src``.
    """
    tokens = _md.parse(text or "")
    for token in tokens:
        for child in token.children or []:
            attr = {"image": "src", "link_open": "href"}.get(child.type)
            if not attr:
                continue
            target = child.attrGet(attr) or ""
            if target.startswith(ASSET_SCHEME):
                name = target[len(ASSET_SCHEME):]
                child.attrSet(attr, asset_url(name) if asset_url else "")
            if child.type == "image":
                child.attrSet("loading", "lazy")
    return _md.renderer.render(tokens, _md.options, {})
