"""What each Claude turn cost. Claude Code reports a session's running total (ResultMessage.total_cost_usd grows
turn after turn and carries on when the session is resumed), so a turn's own cost is the step from the total
before it. Transcripts written before 0.4.0 kept the running total as each turn's cost; they are read the same
way, so their turns, conversations and stage reports come out right without rewriting the files."""


def turn_cost(total, before):
    """This turn's cost from the running total now and the one before it (None: no turn before). A total below the
    last one is a new running total: a new session of the CLI."""
    total = float(total or 0.0)
    if before is None or total < float(before) - 1e-9:
        return round(total, 4)
    return round(total - float(before), 4)


def per_turn(records):
    """(records, total): the transcript with each "done" record's cost as its own turn's (records from before 0.4.0,
    which hold the running total, are given the step and their total as "total"; copies, the files are not touched),
    and what the conversation cost."""
    out, last, spent = [], None, 0.0
    for r in records:
        if r.get("kind") == "done":
            if "total" in r:                               # 0.4.0 on: the turn's own cost, and the running total
                last = r["total"]
            else:
                run = r.get("cost") or 0.0
                r = dict(r, cost=turn_cost(run, last), total=run)
                last = run
            spent += r.get("cost") or 0.0
        out.append(r)
    return out, round(spent, 4)


def last_total(records):
    """The CLI's running total after the transcript's last turn, or None."""
    for r in reversed(records):
        if r.get("kind") == "done":
            return r.get("total", r.get("cost"))
    return None
