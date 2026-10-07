"""Custom design rules (.kicad_dru) that the toolkit's own tools rely on, added idempotently."""
import os, re


def dru_path(project):
    return os.path.splitext(project.pcb)[0] + ".kicad_dru"


def _strip_rule(txt, name):
    """txt without the rule `name` (and the comment lines just above it)."""
    i = txt.find(f'(rule "{name}"')
    if i < 0:
        return txt
    depth, j = 0, i
    while j < len(txt):
        if txt[j] == "(":
            depth += 1
        elif txt[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    k = txt.rfind("\n", 0, i)
    while k > 0:
        prev = txt.rfind("\n", 0, k)
        if txt[prev + 1:k].lstrip().startswith("#"):
            k = prev
        else:
            break
    return txt[:k + 1 if k >= 0 else 0] + txt[j + 1:].lstrip("\n")


def ensure_rules(project, rules, replace=False):
    """rules: {name: rule text starting with (rule "name" ...)}; returns the names added (replace: rewrite a rule of
    that name that reads differently, e.g. the neck-down clearance changed)."""
    path = dru_path(project)
    txt = open(path, encoding="utf-8").read() if os.path.exists(path) else "(version 1)\n"
    added = []
    for name, body in rules.items():
        if f'(rule "{name}"' in txt:
            if not replace or body.strip() in txt:
                continue
            txt = _strip_rule(txt, name)
        txt = txt.rstrip() + "\n" + body.strip() + "\n"
        added.append(name)
    if added:
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
    return added


def neck_rule(clearance):
    return ("tw fine-pitch escape", f'''# Tracewright: tracks escaping fine-pitch pads may run at {clearance} mm clearance inside
# the "TW neck <ref>" rule areas around those footprints (the router necks down there).
(rule "tw fine-pitch escape"
  (condition "A.intersectsArea('TW neck*')")
  (constraint clearance (min {clearance}mm)))''')
