"""Cross-references module."""

from __future__ import annotations

import re
import unicodedata
from html import escape, unescape
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Match, Tuple
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from markdown.core import Markdown
from markdown.extensions import Extension
from markdown.inlinepatterns import REFERENCE_RE, ReferenceInlineProcessor
from markdown.treeprocessors import Treeprocessor
from markdown.util import INLINE_PLACEHOLDER_RE

if TYPE_CHECKING:
    from markdown import Markdown

    from mkdocs_autorefs.plugin import AutorefsPlugin

AUTO_REF_RE = re.compile(
    r"<span data-(?P<kind>autorefs-identifier|autorefs-optional|autorefs-optional-hover)="
    r'("?)(?P<identifier>[^"<>]*)\2>(?P<title>.*?)</span>',
    flags=re.DOTALL,
)
"""A regular expression to match mkdocs-autorefs' special reference markers
in the [`on_post_page` hook][mkdocs_autorefs.plugin.AutorefsPlugin.on_post_page].
"""

EvalIDType = Tuple[Any, Any, Any]


class AutoRefInlineProcessor(ReferenceInlineProcessor):
    """A Markdown extension."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D107
        super().__init__(REFERENCE_RE, *args, **kwargs)

    # Code based on
    # https://github.com/Python-Markdown/markdown/blob/8e7528fa5c98bf4652deb13206d6e6241d61630b/markdown/inlinepatterns.py#L780

    def handleMatch(self, m: Match[str], data: Any) -> Element | EvalIDType:  # type: ignore[override]  # noqa: N802
        """Handle an element that matched.

        Arguments:
            m: The match object.
            data: The matched data.

        Returns:
            A new element or a tuple.
        """
        text, index, handled = self.getText(data, m.end(0))
        if not handled:
            return None, None, None

        identifier, end, handled = self.evalId(data, index, text)
        if not handled:
            return None, None, None

        if re.search(r"[/ \x00-\x1f]", identifier):
            # Do nothing if the matched reference contains:
            # - a space, slash or control character (considered unintended);
            # - specifically \x01 is used by Python-Markdown HTML stash when there's inline formatting,
            #   but references with Markdown formatting are not possible anyway.
            return None, m.start(0), end

        return self.makeTag(identifier, text), m.start(0), end

    def evalId(self, data: str, index: int, text: str) -> EvalIDType:  # noqa: N802 (parent's casing)
        """Evaluate the id portion of `[ref][id]`.

        If `[ref][]` use `[ref]`.

        Arguments:
            data: The data to evaluate.
            index: The starting position.
            text: The text to use when no identifier.

        Returns:
            A tuple containing the identifier, its end position, and whether it matched.
        """
        m = self.RE_LINK.match(data, pos=index)
        if not m:
            return None, index, False

        identifier = m.group(1)
        if not identifier:
            identifier = text
            # Allow the entire content to be one placeholder, with the intent of catching things like [`Foo`][].
            # It doesn't catch [*Foo*][] though, just due to the priority order.
            # https://github.com/Python-Markdown/markdown/blob/1858c1b601ead62ed49646ae0d99298f41b1a271/markdown/inlinepatterns.py#L78
            if INLINE_PLACEHOLDER_RE.fullmatch(identifier):
                identifier = self.unescape(identifier)

        end = m.end(0)
        return identifier, end, True

    def makeTag(self, identifier: str, text: str) -> Element:  # type: ignore[override]  # noqa: N802
        """Create a tag that can be matched by `AUTO_REF_RE`.

        Arguments:
            identifier: The identifier to use in the HTML property.
            text: The text to use in the HTML tag.

        Returns:
            A new element.
        """
        el = Element("span")
        el.set("data-autorefs-identifier", identifier)
        el.text = text
        return el


def relative_url(url_a: str, url_b: str) -> str:
    """Compute the relative path from URL A to URL B.

    Arguments:
        url_a: URL A.
        url_b: URL B.

    Returns:
        The relative URL to go from A to B.
    """
    parts_a = url_a.split("/")
    url_b, anchor = url_b.split("#", 1)
    parts_b = url_b.split("/")

    # remove common left parts
    while parts_a and parts_b and parts_a[0] == parts_b[0]:
        parts_a.pop(0)
        parts_b.pop(0)

    # go up as many times as remaining a parts' depth
    levels = len(parts_a) - 1
    parts_relative = [".."] * levels + parts_b
    relative = "/".join(parts_relative)
    return f"{relative}#{anchor}"


def fix_ref(url_mapper: Callable[[str], str], unmapped: list[str]) -> Callable:
    """Return a `repl` function for [`re.sub`](https://docs.python.org/3/library/re.html#re.sub).

    In our context, we match Markdown references and replace them with HTML links.

    When the matched reference's identifier was not mapped to an URL, we append the identifier to the outer
    `unmapped` list. It generally means the user is trying to cross-reference an object that was not collected
    and rendered, making it impossible to link to it. We catch this exception in the caller to issue a warning.

    Arguments:
        url_mapper: A callable that gets an object's site URL by its identifier,
            such as [mkdocs_autorefs.plugin.AutorefsPlugin.get_item_url][].
        unmapped: A list to store unmapped identifiers.

    Returns:
        The actual function accepting a [`Match` object](https://docs.python.org/3/library/re.html#match-objects)
        and returning the replacement strings.
    """

    def inner(match: Match) -> str:
        identifier = match["identifier"]
        title = match["title"]
        kind = match["kind"]

        try:
            url = url_mapper(unescape(identifier))
        except KeyError:
            if kind == "autorefs-optional":
                return title
            if kind == "autorefs-optional-hover":
                return f'<span title="{identifier}">{title}</span>'
            unmapped.append(identifier)
            if title == identifier:
                return f"[{identifier}][]"
            return f"[{title}][{identifier}]"

        parsed = urlsplit(url)
        external = parsed.scheme or parsed.netloc
        classes = ["autorefs", "autorefs-external" if external else "autorefs-internal"]
        class_attr = " ".join(classes)
        if kind == "autorefs-optional-hover":
            return f'<a class="{class_attr}" title="{identifier}" href="{escape(url)}">{title}</a>'
        return f'<a class="{class_attr}" href="{escape(url)}">{title}</a>'

    return inner


def fix_refs(html: str, url_mapper: Callable[[str], str]) -> tuple[str, list[str]]:
    """Fix all references in the given HTML text.

    Arguments:
        html: The text to fix.
        url_mapper: A callable that gets an object's site URL by its identifier,
            such as [mkdocs_autorefs.plugin.AutorefsPlugin.get_item_url][].

    Returns:
        The fixed HTML.
    """
    unmapped: list[str] = []
    html = AUTO_REF_RE.sub(fix_ref(url_mapper, unmapped), html)
    return html, unmapped


class AnchorScannerTreeProcessor(Treeprocessor):
    """Tree processor to scan and register HTML anchors."""

    _htags: ClassVar[set[str]] = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, plugin: AutorefsPlugin, md: Markdown | None = None) -> None:
        """Initialize the tree processor.

        Parameters:
            plugin: A reference to the autorefs plugin, to use its `register_anchor` method.
        """
        super().__init__(md)
        self.plugin = plugin

    def run(self, root: Element) -> None:  # noqa: D102
        if self.plugin.current_page is not None:
            self._scan_anchors(root)

    @staticmethod
    def _slug(value: str, separator: str = "-") -> str:
        value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^\w\s-]", "", value.lower())
        return re.sub(r"[-_\s]+", separator, value).strip("-_")

    def _scan_anchors(self, parent: Element) -> list[str]:
        ids = []
        # We iterate on pairs of elements, to check if the next element is a heading (alias feature).
        for el, next_el in zip_longest(parent, parent[1:], fillvalue=Element("/")):
            if el.tag == "a":
                # We found an anchor. Record its id if it has one.
                if hid := el.get("id"):
                    if el.tail and el.tail.strip():
                        # If the anchor has a non-whitespace-only tail, it's not an alias:
                        # register it immediately.
                        self.plugin.register_anchor(self.plugin.current_page, hid)  # type: ignore[arg-type]
                    else:
                        # Else record its id and continue.
                        ids.append(hid)
            elif el.tag == "p":
                if ids := self._scan_anchors(el):
                    # Markdown anchors are always rendered as `a` tags within a `p` tag.
                    # Headings therefore appear after the `p` tag. Here the current element
                    # is a `p` tag and it contains at least one anchor with an id.
                    # We can check if the next element is a heading, and use its id as href.
                    href = (next_el.get("id") or self._slug(next_el.text or "")) if next_el.tag in self._htags else ""
                    for hid in ids:
                        self.plugin.register_anchor(self.plugin.current_page, hid, href)  # type: ignore[arg-type]
                    ids.clear()
            else:
                # Recurse into sub-elements.
                ids = self._scan_anchors(el)
        return ids


class AutorefsExtension(Extension):
    """Extension that inserts auto-references in Markdown."""

    def __init__(
        self,
        plugin: AutorefsPlugin | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Markdown extension.

        Parameters:
            plugin: An optional reference to the autorefs plugin (to pass it to the anchor scanner tree processor).
            **kwargs: Keyword arguments passed to the [base constructor][markdown.extensions.Extension].
        """
        super().__init__(**kwargs)
        self.plugin = plugin

    def extendMarkdown(self, md: Markdown) -> None:  # noqa: N802 (casing: parent method's name)
        """Register the extension.

        Add an instance of our [`AutoRefInlineProcessor`][mkdocs_autorefs.references.AutoRefInlineProcessor] to the Markdown parser.
        Also optionally add an instance of our [`AnchorScannerTreeProcessor`][mkdocs_autorefs.references.AnchorScannerTreeProcessor]
        to the Markdown parser if a reference to the autorefs plugin was passed to this extension.

        Arguments:
            md: A `markdown.Markdown` instance.
        """
        md.inlinePatterns.register(
            AutoRefInlineProcessor(md),
            "mkdocs-autorefs",
            priority=168,  # Right after markdown.inlinepatterns.ReferenceInlineProcessor
        )
        if self.plugin:
            md.treeprocessors.register(
                AnchorScannerTreeProcessor(self.plugin, md),
                "mkdocs-autorefs-anchors-scanner",
                priority=0,
            )
