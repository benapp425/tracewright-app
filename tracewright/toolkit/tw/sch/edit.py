"""Edit an existing schematic in place, touching only the text that changes.

    set_fields(project, {"R1": {"LCSC": "C25744", "MPN": "0402WGF1002TCE"}, "U1": {"Value": "AMS1117-3.3"}})
    set_flags(project, {"R9": {"dnp": True}, "TP1": {"in_bom": False}})
    rename_net(project, "SDA", "I2C_SDA", kind="global_label")     # KiCad's netlist proves the connections hold

Symbols are found by their reference as resolved for each sheet instance. Existing properties are
rewritten; new ones are added hidden, right after the symbol's last property. Everything else in the
file keeps its exact formatting. Returns what changed, per file.
"""
import os, re, shutil, tempfile
from ..sexp import parse, find, findall, value, splice, indent_of, atom_text, Q
from ..schematic import Hierarchy

FLAGS = ("dnp", "in_bom", "on_board", "exclude_from_sim")
LABELS = ("label", "global_label", "hierarchical_label")


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


def _refs_of(h, path, sym):
    """A placed symbol's references, as resolved for each instance of its sheet."""
    paths = [sh.path for sh in h.sheets if os.path.abspath(sh.file) == os.path.abspath(path)]
    base = next((str(p[2]) for p in findall(sym, "property") if p[1] == "Reference"), None)
    refs = set()
    inst = find(sym, "instances")
    if inst:
        for pr in findall(inst, "project"):
            for pth in findall(pr, "path"):
                if str(pth[1]) in paths:
                    refs.add(str(value(pth, "reference", base)))
    if not refs and base:
        refs.add(base)
    return refs


def set_flags(project, changes, dry_run=False):
    """A placed symbol's flags: {ref: {"dnp": True, "in_bom": False, "on_board": True, "exclude_from_sim": False}},
    each written as KiCad does ((dnp yes)), in place, or added after the symbol's (unit) when missing. Every unit of
    a multi-unit part follows. Returns what changed, per file."""
    h = Hierarchy.load(project.sch)
    report = {}
    for path, sf in h.files.items():
        text = sf.text
        t = parse(text, spans=True)
        edits = []
        for sym in findall(t, "symbol"):
            hit = [r for r in _refs_of(h, path, sym) if r in changes]
            if not hit:
                continue
            ref = hit[0]
            for k, on in changes[ref].items():
                if k not in FLAGS:
                    raise ValueError(f"not a symbol flag: {k} (one of {', '.join(FLAGS)})")
                want = "yes" if on else "no"
                node = find(sym, k)
                if node is not None:
                    if str(node[1]) == want:
                        continue
                    edits.append((node.span, f"({k} {want})"))
                else:
                    anchor = find(sym, "unit") or find(sym, "at")
                    e = anchor.span[1]
                    edits.append(((e, e), f"\n{indent_of(text, anchor.span[0])}({k} {want})"))
                report.setdefault(os.path.basename(path), []).append(f"{ref}.{k} = {want}")
        if edits and not dry_run:
            new = splice(text, edits)
            parse(new)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
    return report


_NAME = re.compile(r"^[^\s\[\]{}\"\\/][^\s\[\]\"\\]{0,63}$")


def _relabel(text, node, old, new):
    """The node's first quoted atom (a label's text, a sheet pin's name) from old to new, as an edit."""
    s, e = node.span
    seg = text[s:e]
    q = _esc(old)
    i = seg.find(q)
    if i < 0:
        return None
    return ((s + i, s + i + len(q)), _esc(new))


def rename_net(project, old, new, kind=None, sheet=None, check=True):
    """Rename what names a net. kind: label (the local labels named old in one sheet file -- sheet: that file's
    path, or a sheet's name path; the root without it), global_label (every global label named old, on every
    sheet) or hierarchical_label (those in the sheet file, with the sheet pins that meet them in the sheets
    above). Without kind: global labels when there are any, else local labels in the sheet. Refused when new is
    already a label in that scope or a supply's name (that would join two nets). With check, KiCad's netlist
    before and after must join the same pins, else nothing is written. Returns {changed: [files], labels, pins,
    proof}."""
    old, new = str(old), str(new).strip()
    if not _NAME.match(new):
        raise ValueError("a net name has no spaces, quotes, brackets or braces, does not start with /, and is at most 64 characters")
    if new == old:
        raise ValueError("that is its name already")
    h = Hierarchy.load(project.sch)
    files = {os.path.abspath(p): sf for p, sf in h.files.items()}
    trees = {p: parse(sf.text, spans=True) for p, sf in files.items()}

    def nodes(p, k, name):
        return [n for n in findall(trees[p], k) if len(n) > 1 and not isinstance(n[1], list) and str(n[1]) == name]

    root = os.path.abspath(project.sch)
    if sheet:
        cand = [os.path.abspath(sh.file) for sh in h.sheets if sh.name_path == sheet] or [os.path.abspath(sheet)]
        target = cand[0]
        if target not in files:
            raise ValueError(f"no sheet {sheet}")
    else:
        target = root
    if kind is None:
        kind = "global_label" if any(nodes(p, "global_label", old) for p in files) else "label"
    if kind not in LABELS:
        raise ValueError(f"kind: one of {', '.join(LABELS)}")
    scope = list(files) if kind == "global_label" else [target]
    found = {p: nodes(p, kind, old) for p in scope}
    if not any(found.values()):
        raise ValueError(f"no {kind.replace('_', ' ')} named {old}" + ("" if kind == "global_label" else f" on {os.path.basename(target)}"))
    clash = [p for p in scope if nodes(p, kind, new)]
    if clash:
        raise ValueError(f"{new} is already a {kind.replace('_', ' ')}{'' if kind == 'global_label' else ' on this sheet'}: "
                         "renaming would join the two nets")
    if any(sym.is_power and sym.value == new for sh in h.sheets for sym in sh.symbols):
        raise ValueError(f"{new} is a supply's name (its power symbol): renaming would join the net to it")
    edits = {p: [] for p in files}
    n_labels = n_pins = 0
    for p, ns in found.items():
        for n in ns:
            ed = _relabel(files[p].text, n, old, new)
            if ed:
                edits[p].append(ed)
                n_labels += 1
    if kind == "hierarchical_label":                      # the sheet pins that meet them, in the sheets above
        for p in files:
            for sh in findall(trees[p], "sheet"):
                props = {str(q[1]): str(q[2]) for q in findall(sh, "property") if len(q) >= 3}
                sfile = props.get("Sheetfile") or props.get("Sheet file") or ""
                if os.path.abspath(os.path.join(os.path.dirname(p), sfile)) != target:
                    continue
                for pin in findall(sh, "pin"):
                    if len(pin) > 1 and str(pin[1]) == old:
                        ed = _relabel(files[p].text, pin, old, new)
                        if ed:
                            edits[p].append(ed)
                            n_pins += 1
    new_text = {p: splice(files[p].text, e) for p, e in edits.items() if e}
    for t in new_text.values():
        parse(t)
    proof = None
    if check:
        proof = _same_connections(project.sch, h, new_text)
        if not proof["same"]:
            raise ValueError("renaming would change the connections, so nothing was written: " + "; ".join(proof["differences"][:3]))
    for p, t in new_text.items():
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)
    base = os.path.dirname(root)
    return {"changed": sorted(os.path.relpath(p, base) for p in new_text), "labels": n_labels, "pins": n_pins, "proof": proof,
            "kind": kind}


def _same_connections(sch, h, new_text):
    """KiCad's netlist of the schematic as it is and as it would be: {same, nets, differences}."""
    from .style import _export, _netlist_partition, _diff
    tmp = tempfile.mkdtemp(prefix="tw-rename-")
    try:
        base = os.path.dirname(os.path.abspath(sch))
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        for d in (a, b):
            for f in h.files:
                rel = os.path.relpath(os.path.abspath(f), base)
                if rel.startswith(".."):
                    raise ValueError(f"{os.path.basename(f)} lives outside the schematic's folder")
                os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
                shutil.copyfile(f, os.path.join(d, rel))
            pro = os.path.splitext(os.path.abspath(sch))[0] + ".kicad_pro"
            if os.path.exists(pro):
                shutil.copyfile(pro, os.path.join(d, os.path.basename(pro)))
        for p, t in new_text.items():
            with open(os.path.join(b, os.path.relpath(p, base)), "w", encoding="utf-8") as f:
                f.write(t)
        name = os.path.basename(sch)
        n0 = _export(os.path.join(a, name), a)
        n1 = _export(os.path.join(b, name), b)
        p0, p1 = _netlist_partition(n0), _netlist_partition(n1)
        same = set(p0) == set(p1)
        return {"same": same, "nets": len(p0), "differences": [] if same else _diff(set(p0), set(p1))}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
