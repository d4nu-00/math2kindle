"""Parse .bbl bibliographies (classic thebibliography, natbib, basic biblatex)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .texscan import match_brace, read_arg, read_opt


@dataclass
class BibEntry:
    key: str
    label: str          # what to print in brackets, e.g. "3" or "Dav80"
    text: str           # LaTeX of the entry
    author: str = ""    # natbib author-year info (may be empty)
    year: str = ""


def parse_thebibliography(tex: str) -> list[BibEntry]:
    m = re.search(r"\\begin\s*\{thebibliography\}", tex)
    if not m:
        return []
    _, j = read_arg(tex, m.end())          # widest-label argument
    e = re.search(r"\\end\s*\{thebibliography\}", tex[j:])
    body = tex[j: j + e.start()] if e else tex[j:]
    entries: list[BibEntry] = []
    parts = re.split(r"\\bibitem(?![A-Za-z@])", body)
    n = 0
    for part in parts[1:]:
        opt, k = read_opt(part, 0)
        key, k = read_arg(part, k)
        if key is None:
            continue
        text = part[k:]
        text = re.sub(r"\\newblock\b\s*", " ", text)
        text = re.sub(r"\\(providecommand|newcommand)\s*\{?\\[A-Za-z@]+\}?\s*(\[\d\])?\s*\{.*?\}\s*$", "", text,
                      flags=re.S)
        text = re.sub(r"\\BibitemShut\s*\{[^}]*\}|\\BibitemOpen|\\bibfield\s*\{[^}]*\}|\\bibinfo\s*\{[^}]*\}",
                      "", text)
        text = text.strip()
        n += 1
        author = year = ""
        label = str(n)
        if opt:
            o = opt.strip()
            if o.startswith("{") and o.endswith("}"):
                o = o[1:-1]
            nat = re.match(r"^(.*?)\((.*?)\)(.*)$", o, re.S)
            if nat:
                author, year = nat.group(1).strip(), nat.group(2).strip()
            else:
                label = o
        entries.append(BibEntry(key=key.strip(), label=label, text=text, author=author, year=year))
    # natbib with author-year: keep numeric labels for brackets but remember author/year
    return entries


def parse_biblatex_bbl(tex: str) -> list[BibEntry]:
    """Very small biblatex .bbl reader: authors, title, journal/booktitle, volume, year, pages."""
    entries = []
    n = 0
    for m in re.finditer(r"\\entry\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{[^}]*\}", tex):
        key = m.group(1)
        end = tex.find("\\endentry", m.end())
        block = tex[m.end(): end if end > 0 else len(tex)]
        fields = {}
        for f in re.finditer(r"\\field\s*\{(\w+)\}\s*\{", block):
            e = match_brace(block, f.end() - 1)
            fields[f.group(1)] = block[f.end(): e]
        names = []
        nm = re.search(r"\\name\s*\{author\}\s*\{\d+\}\s*\{[^}]*\}\s*\{", block)
        if nm:
            e = match_brace(block, nm.end() - 1)
            seg = block[nm.end(): e]
            for person in re.finditer(r"family=\{([^}]*)\}.*?(?:given=\{([^}]*)\})?", seg, re.S):
                fam, giv = person.group(1), person.group(2) or ""
                names.append((giv + " " + fam).strip())
        n += 1
        parts = []
        if names:
            parts.append(", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1])
        if fields.get("title"):
            parts.append("\\emph{%s}" % fields["title"])
        venue = fields.get("journaltitle") or fields.get("booktitle") or fields.get("publisher") or ""
        if venue:
            v = venue
            if fields.get("volume"):
                v += " \\textbf{%s}" % fields["volume"]
            if fields.get("year"):
                v += " (%s)" % fields["year"]
            if fields.get("pages"):
                v += ", %s" % fields["pages"].replace("\\bibrangedash", "--")
            parts.append(v)
        elif fields.get("year"):
            parts.append(fields["year"])
        if fields.get("eprint"):
            parts.append("arXiv:" + fields["eprint"])
        lab = fields.get("labelalpha")
        entries.append(BibEntry(key=key, label=lab or str(n), text=", ".join(parts) + ".",
                                author=(names[0].split()[-1] if names else ""), year=fields.get("year", "")))
    return entries


def parse_bbl(tex: str) -> list[BibEntry]:
    if "\\entry{" in tex or "\\entry {" in tex:
        return parse_biblatex_bbl(tex)
    return parse_thebibliography(tex)
