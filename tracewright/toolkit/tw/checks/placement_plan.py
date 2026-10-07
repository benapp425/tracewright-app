"""The placement plan held to the board: every constraint the user (or Claude) set is kept, and every part placed has
its reason (tw/placeplan.py)."""
from . import check, Finding, NotApplicable, examined, plural


@check("placement.plan", "Placement keeps its constraints", "Placement", needs=("pcb",))
def placement_plan(ctx):
    """Each constraint in hardware/<board>/placement-plan.json -- a part near a pin, parts together, a part away from
    others, at an edge, on a side -- measured on the board as it is; one that is broken is an error when the user set
    it, a warning when Claude did. Parts placed without a reason are listed (the user reads it when they pick one)."""
    from .. import placeplan
    plan = placeplan.load(ctx.p)
    if not plan["constraints"] and not plan["parts"]:
        raise NotApplicable("no placement plan yet")
    out = []
    ev = placeplan.evaluate(ctx.board, plan)
    for e in ev:
        if e["ok"] is False:
            c = e["constraint"]
            out.append(Finding("placement.plan", "error" if c.get("by") == "user" else "warning", f"broken: {e['text']}",
                               {"ref": c.get("ref") or (c.get("refs") or [""])[0]}, key=f"plan:{c.get('id')}",
                               hint=("You set this" if c.get("by") == "user" else "Claude set this") + " constraint: move the part back "
                                    "within it, or change the constraint."))
    examined(ctx, plural(len(ev), "constraint") + ", " + plural(len(plan["parts"]), "reason"))
    return out
