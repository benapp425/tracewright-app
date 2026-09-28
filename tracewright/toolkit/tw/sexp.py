"""S-expression reader / writer for KiCad files (stdlib only, Python 3.9+).

Parsed form: nested lists. Atoms are `str` (bare) or `Q` (quoted string). Numbers stay as their
original text, so nothing is reformatted on the way through.

Two ways to change a file:
  * `parse` + edit the lists + `dumps`: KiCad-like formatting, fine for files we generate;
  * `parse(text, spans=True)` + `splice(text, [(node, new_text)])`: replaces only the given nodes'
    text in the original, so a hand-edited KiCad file keeps its exact formatting everywhere else
    (small diffs, nothing surprising for the person who owns the file).
"""
import re

__all__ = ["Q", "Node", "parse", "dumps", "find", "findall", "prop", "value", "splice", "atom_text"]


class Q(str):
    """A quoted string atom."""
    __slots__ = ()


class Node(list):
    """A parsed list; `span` is its (start, end) character range in the source text."""
    __slots__ = ("span",)


_tok = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def _unescape(t):
    if "\\" not in t:
        return t
    out, i = [], 0
    while i < len(t):
        c = t[i]
        if c == "\\" and i + 1 < len(t):
            n = t[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse(text, spans=False):
    """Parse one S-expression document. With spans=True every list is a Node carrying .span."""
    stack, cur = [], (Node() if spans else [])
    starts = []
    pos, n = 0, len(text)
    match = _tok.match
    while pos < n:
        m = match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"parse error at {pos}: {text[pos:pos + 40]!r}")
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = Node() if spans else []
            if spans:
                starts.append(m.start(1))
        elif m.group(2):
            done = cur
            if spans:
                done.span = (starts.pop(), m.end(2))
            cur = stack.pop()
            cur.append(done)
        elif m.group(3) is not None:
            cur.append(Q(_unescape(m.group(3))))
        else:
            cur.append(m.group(4))
    if stack:
        raise ValueError("unbalanced parentheses")
    return cur[0] if len(cur) == 1 else cur


def atom_text(a):
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, float):
        s = f"{a:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(a)


_COMPACT = {"pts", "at", "xy", "size", "start", "end", "mid", "center", "offset", "color", "stroke", "font",
            "effects", "justify", "layers", "width", "type", "uuid", "net", "layer", "length", "fill", "margins"}


def dumps(node, indent=0):
    """KiCad-like formatting: short lists inline, others one child per line (tabs)."""
    if not isinstance(node, list):
        return atom_text(node)
    if not node:
        return "()"
    head = atom_text(node[0])
    if all(not isinstance(x, list) for x in node):
        return "(" + " ".join(atom_text(x) for x in node) + ")"
    if node[0] == "pts":
        return "(pts " + " ".join(dumps(x) for x in node[1:]) + ")"
    pad = "\t" * (indent + 1)
    parts = ["(" + head]
    inline, i = [], 1
    while i < len(node) and not isinstance(node[i], list):
        inline.append(atom_text(node[i]))
        i += 1
    if inline:
        parts[0] += " " + " ".join(inline)
    for child in node[i:]:
        parts.append("\n" + pad + dumps(child, indent + 1))
    parts.append("\n" + "\t" * indent + ")")
    return "".join(parts)


def find(node, key):
    """First child list whose head is `key` (or None)."""
    for c in node:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def findall(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def value(node, key, default=None, index=1):
    """Atom `index` of the first child `key`: value(fp, "layer") -> "F.Cu"."""
    c = find(node, key)
    if c is None or len(c) <= index:
        return default
    return c[index]


def prop(node, name):
    """The (property "name" "value" ...) child, or None."""
    for c in findall(node, "property"):
        if len(c) > 1 and c[1] == name:
            return c
    return None


def flag(node, key):
    """KiCad boolean children: (hide yes), a bare `hide` atom, or (locked) all read as True."""
    for c in node:
        if c == key:
            return True
        if isinstance(c, list) and c and c[0] == key:
            return len(c) == 1 or c[1] in ("yes", "true")
    return False


def splice(text, edits):
    """Replace node spans in `text`: edits = [(node_with_span or (start, end), replacement_text)].
    Non-overlapping; applied back to front so earlier offsets stay valid."""
    rng = []
    for n, rep in edits:
        s, e = n.span if hasattr(n, "span") else n
        rng.append((s, e, rep))
    rng.sort(key=lambda r: r[0], reverse=True)
    last = None
    for s, e, rep in rng:
        if last is not None and e > last:
            raise ValueError("overlapping edits")
        text = text[:s] + rep + text[e:]
        last = s
    return text


def indent_of(text, pos):
    """The whitespace that starts the line containing `pos` (for inserting siblings)."""
    ls = text.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", text[ls:])
    return m.group(0) if m else ""
