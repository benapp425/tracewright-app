"""The design in one read, compact: every part (value, part number and LCSC code, footprint, sheet, where it sits on
the board, not fitted) and every net (its kind from the net model -- a supply's voltage and current, a pair's partner
and impedance -- and its pins), from KiCad's netlist and the board file. One line each, so a whole board is a few
thousand characters: read it instead of querying part by part.

    print(text(project))                 # the whole design
    print(text(project, ref="U2"))       # one part: every pin with its name and net (and its kind)
    print(text(project, net="+3V3"))     # one net: every pin, with the part and pin name

    ./tw design [--ref U2 | --net +3V3]
"""
import re

from .nettypes import short


def _nat(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _fp(lib_id):
    return (lib_id or "").split(":")[-1]


def _ctx(project):
    from .checks.context import Context
    return Context(project, offline=True)


def _where(board, ref):
    f = board.footprints.get(ref) if board is not None else None
    if f is None:
        return "not on the board" if board is not None else ""
    return f"({f.x:.2f}, {f.y:.2f}) {f.side}{f' {f.angle:g}°' if f.angle else ''}{' locked' if f.locked else ''}"


def _kind(rec):
    if not rec:
        return ""
    from .netmodel import tag
    k = rec.get("kind") or "signal"
    t = tag(rec)
    bits = [k] + ([t] if t and t not in ("GND",) else [])
    if k == "pair" and rec.get("pair"):
        bits.append(f"with {short(rec['pair'])}")
    if rec.get("impedance"):
        bits.append(f"{rec['impedance']:g} Ω")
    return " ".join(bits)


def text(project, ref=None, net=None, pins=8):
    ctx = _ctx(project)
    nl = ctx.netlist
    board = ctx.board if ctx.available("pcb") else None
    try:
        from .netmodel import for_context
        model = for_context(ctx)
    except Exception:
        model = None
    rec = (lambda n: model.records.get(n) if model else None)
    if ref:
        p = nl.parts.get(ref)
        if not p:
            return f"no {ref} in the schematic"
        f = p.get("fields") or {}
        out = [f"{ref} {p['value']}  {f.get('MPN') or ''} {f.get('LCSC') or ''}".rstrip(),
               f"  footprint {p['footprint']}  sheet {p.get('sheet') or '/'}  {_where(board, ref)}{'  NOT FITTED' if p.get('dnp') else ''}"]
        try:
            from .sch import notes as dn
            for n_ in dn.for_ref(project, ref):
                out.append(f"  note{' (pin ' + n_['anchor']['pin'] + ')' if n_['anchor'].get('pin') else ''}: "
                           f"{n_.get('short') + ' -- ' if n_.get('short') else ''}{n_['why']}")
        except Exception:
            pass
        for pin in sorted(nl.pins_of(ref), key=_nat):
            n = nl.pin.get((ref, pin)) or ""
            nm = nl.pin_name(ref, pin)
            k = _kind(rec(n)) if n and not n.startswith("unconnected-") else ""
            k = "" if k == "signal" else k
            out.append(f"  {pin:>4} {nm[:16]:<16} {'not connected' if not n or n.startswith('unconnected-') else short(n)}{'  ' + k if k else ''}")
        return "\n".join(out)
    if net:
        full = next((n for n in nl.nets if n == net or short(n) == short(net)), None)
        if not full:
            return f"no net {net}"
        out = [f"{short(full)}  {_kind(rec(full)) or 'signal'}  {len(nl.nets[full])} pins" + (f"  ({full})" if full != short(full) else "")]
        for r, pin in sorted(nl.nets[full], key=lambda x: (_nat(x[0]), _nat(x[1]))):
            p = nl.parts.get(r) or {}
            out.append(f"  {r}.{pin} {nl.pin_name(r, pin)[:16]:<16} {p.get('value', '')[:20]}")
        return "\n".join(out)
    # the whole design
    parts = sorted((r for r in nl.parts if not r.startswith("#")), key=_nat)
    dnp = [r for r in parts if nl.parts[r].get("dnp")]
    out = [f"PARTS {len(parts)}" + (f" ({len(dnp)} not fitted)" if dnp else "") +
           (f"; board: {len(board.fp_list)} footprints" if board is not None else "; no board yet")]
    for r in parts:
        p = nl.parts[r]
        f = p.get("fields") or {}
        num = " ".join(x for x in (f.get("MPN") or "", f.get("LCSC") or "") if x)
        out.append(f"{r} {p['value'][:24]}  {num or '-'}  {_fp(p['footprint'])[:56] or '(no footprint)'}  {p.get('sheet') or '/'}"
                   f"{'  ' + _where(board, r) if board is not None else ''}{'  NOT FITTED' if p.get('dnp') else ''}")
    if board is not None:
        extra = sorted((r for r in board.footprints if r not in nl.parts and not r.startswith(("#", "REF"))), key=_nat)
        if extra:
            out.append("ON THE BOARD ONLY " + " ".join(extra))
    nets = sorted((n for n in nl.nets if not n.startswith("unconnected-")), key=lambda n: (
        {"ground": 0, "power": 1}.get((rec(n) or {}).get("kind"), 2), _nat(short(n))))
    out.append(f"NETS {len(nets)}")
    for n in nets:
        nodes = sorted(nl.nets[n], key=lambda x: (_nat(x[0]), _nat(x[1])))
        k = _kind(rec(n))
        shown = " ".join(f"{r}.{p}" for r, p in nodes[:pins]) + (f" +{len(nodes) - pins}" if len(nodes) > pins else "")
        out.append(f"{short(n)}{'  ' + k if k and k != 'signal' else ''}: {shown}")
    return "\n".join(out)
