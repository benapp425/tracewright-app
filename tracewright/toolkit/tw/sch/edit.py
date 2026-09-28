"""Edit an existing schematic in place, touching only the text that changes.

    set_fields(project, {"R1": {"LCSC": "C25744", "MPN": "0402WGF1002TCE"}, "U1": {"Value": "AMS1117-3.3"}})

Symbols are found by their reference as resolved for each sheet instance. Existing properties are
rewritten; new ones are added hidden, right after the symbol's last property. Everything else in the
file keeps its exact formatting. Returns what changed, per file.
"""
import os
from ..sexp import parse, find, findall, value, splice, indent_of, atom_text, Q
from ..schematic import Hierarchy


def _esc(v):
    return atom_text(Q(v))


def set_fields(project, changes, dry_run=False):
    h = Hierarchy.load(project.sch)
    want = {r: dict(f) for r, f in changes.items()}
    report = {}
    for path, sf in h.files.items():
        text = sf.text
        t = parse(text, spans=True)
        edits = []
        # resolved references of the symbols in this file, per instance path
        paths = [sh.path for sh in h.sheets if os.path.abspath(sh.file) == os.path.abspath(path)]
        for sym in findall(t, "symbol"):
            refs = set()
            base = None
            for p in findall(sym, "property"):
                if p[1] == "Reference":
                    base = str(p[2])
            inst = find(sym, "instances")
            if inst:
                for pr in findall(inst, "project"):
                    for pth in findall(pr, "path"):
                        if str(pth[1]) in paths:
                            refs.add(str(value(pth, "reference", base)))
            if not refs and base:
                refs.add(base)
            hit = [r for r in refs if r in want]
            if not hit:
                continue
            fields = want[hit[0]]
            props = {str(p[1]): p for p in findall(sym, "property")}
            last = findall(sym, "property")[-1]
            at = find(sym, "at")
            x, y = (at[1], at[2]) if at else ("0", "0")
            ind = indent_of(text, last.span[0])
            new_props = []
            for k, v in fields.items():
                if k in props:
                    node = props[k]
                    if str(node[2]) == str(v):
                        continue
                    # replace just the value atom: rebuild "(property "K" "V"" head text
                    s, e = node.span
                    seg = text[s:e]
                    head = f'(property {_esc(k)} '
                    i = seg.find(head)
                    if i != 0:
                        continue
                    rest = seg[len(head):]
                    # the old value is the first quoted atom of rest
                    j = _quoted_end(rest)
                    edits.append(((s, e), head + _esc(str(v)) + rest[j:]))
                else:
                    new_props.append(f'\n{ind}(property {_esc(k)} {_esc(str(v))}\n{ind}\t(at {x} {y} 0)\n{ind}\t(hide yes)\n'
                                     f'{ind}\t(effects\n{ind}\t\t(font\n{ind}\t\t\t(size 1.27 1.27)\n{ind}\t\t)\n{ind}\t)\n{ind})')
                report.setdefault(os.path.basename(path), []).append(f"{hit[0]}.{k} = {v}")
            if new_props:
                e = last.span[1]
                edits.append(((e, e), "".join(new_props)))
        if edits and not dry_run:
            new = splice(text, edits)
            parse(new)                                   # still a valid S-expression
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
    return report


def _quoted_end(s):
    """Index just past the first quoted string in s."""
    i = s.find('"')
    if i < 0:
        return 0
    j = i + 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == '"':
            return j + 1
        j += 1
    return len(s)
