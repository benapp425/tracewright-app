"""Custom design rules (.kicad_dru) that the toolkit's own tools rely on, added idempotently."""
import os, re


def dru_path(project):
    return os.path.splitext(project.pcb)[0] + ".kicad_dru"


def ensure_rules(project, rules):
    """rules: {name: rule text starting with (rule "name" ...)}; returns the names added."""
    path = dru_path(project)
    txt = open(path, encoding="utf-8").read() if os.path.exists(path) else "(version 1)\n"
    added = []
    for name, body in rules.items():
        if f'(rule "{name}"' in txt:
            continue
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
