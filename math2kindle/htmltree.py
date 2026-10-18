"""lxml helpers for manipulating the intermediate HTML."""

from __future__ import annotations

import re
from typing import Callable, Iterable, Union

from lxml import etree
from lxml import html as LH

Node = Union[str, "etree._Element"]


def parse_fragment(html: str):
    """Parse an HTML fragment into a <div> root."""
    html = html.strip() or "<p></p>"
    return LH.fragment_fromstring(html, create_parent="div")


def E(tag: str, attrib: dict | None = None, text: str | None = None, *children):
    el = LH.Element(tag)
    for k, v in (attrib or {}).items():
        if v is not None:
            el.set(k, str(v))
    if text:
        el.text = text
    for c in children:
        el.append(c)
    return el


def _insert_seq(parent, index: int, seq: list[Node], tail_owner=None) -> None:
    """Insert a sequence of strings/elements into parent at index.  Leading
    text is appended to parent.text (index 0) or to the previous sibling's tail."""
    pending_text = ""
    last_el = None
    pos = index
    for item in seq:
        if isinstance(item, str):
            if last_el is None:
                pending_text += item
            else:
                last_el.tail = (last_el.tail or "") + item
        else:
            if item.tail:
                extra = item.tail
                item.tail = None
            else:
                extra = ""
            parent.insert(pos, item)
            pos += 1
            last_el = item
            if extra:
                last_el.tail = extra
    if pending_text:
        if index == 0:
            parent.text = (parent.text or "") + pending_text
        else:
            prev = parent[index - 1]
            prev.tail = (prev.tail or "") + pending_text


def replace_tokens(root, regex: re.Pattern, make: Callable[[re.Match], Iterable[Node] | Node | None]) -> int:
    """Replace regex matches in all text/tail nodes under root.  ``make``
    returns a string, an element, or a list of them (None = leave as is)."""
    count = 0
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        # text
        if el.text and regex.search(el.text):
            seq = _split(el.text, regex, make)
            if seq is not None:
                count += 1
                el.text = None
                _insert_seq(el, 0, seq)
        # tail
        if el is not root and el.tail and regex.search(el.tail):
            seq = _split(el.tail, regex, make)
            if seq is not None:
                count += 1
                parent = el.getparent()
                el.tail = None
                idx = parent.index(el) + 1
                # leading text of seq goes to el.tail
                lead = []
                while seq and isinstance(seq[0], str):
                    lead.append(seq.pop(0))
                el.tail = "".join(lead) or None
                _insert_seq(parent, idx, seq)
    return count


def _split(text: str, regex: re.Pattern, make) -> list[Node] | None:
    out: list[Node] = []
    pos = 0
    changed = False
    for m in regex.finditer(text):
        r = make(m)
        if r is None:
            continue
        changed = True
        out.append(text[pos:m.start()])
        if isinstance(r, (str, etree._Element)):
            r = [r]
        out.extend(list(r))
        pos = m.end()
    if not changed:
        return None
    out.append(text[pos:])
    return [x for x in out if not (isinstance(x, str) and x == "")]


def add_class(el, *classes: str) -> None:
    cur = (el.get("class") or "").split()
    for c in classes:
        if c and c not in cur:
            cur.append(c)
    el.set("class", " ".join(cur))


def has_class(el, c: str) -> bool:
    return c in (el.get("class") or "").split()


def text_of(el) -> str:
    """Text content where images contribute their alt text."""
    parts = []

    def walk(e):
        if not isinstance(e.tag, str):
            if e.tail:
                parts.append(e.tail)
            return
        if e.tag == "img":
            parts.append(e.get("data-plain") or e.get("alt") or "")
        elif e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
        if e.tail and e is not el:
            parts.append(e.tail)

    walk(el)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def to_xhtml(el) -> str:
    """Serialize an element (and its tail excluded) as XHTML."""
    tail = el.tail
    el.tail = None
    s = etree.tostring(el, method="xml", encoding="unicode")
    el.tail = tail
    return s


def remove_keep_tail(el) -> None:
    """Remove an element but keep the text that follows it."""
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail or ""
    prev = el.getprevious()
    if prev is not None:
        prev.tail = (prev.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail
    parent.remove(el)


def unwrap(el) -> None:
    """Replace el by its children."""
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    seq: list[Node] = []
    if el.text:
        seq.append(el.text)
    for c in list(el):
        seq.append(c)
    tail = el.tail
    parent.remove(el)
    if tail:
        seq.append(tail)
    _insert_seq(parent, idx, seq)


INLINE_TAGS = {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i", "kbd", "mark", "q", "s",
               "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "img", "br", "wbr"}
