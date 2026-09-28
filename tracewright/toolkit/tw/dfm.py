"""Fab capability profiles (what the board house can build) used by the DFM checks.

The numbers are the fab's published standard-process limits as of the date below. They change:
check the fab's capabilities page before ordering, and override any value in tracewright.json:

    "fab": {"house": "jlcpcb", "layers": 4, "rules": {"min_track": 0.1}}

"min_*" values are hard limits (below = error); "rec_*" values are the comfortable defaults
(below = warning, costs yield or money).
"""

CHECKED = "2026-09"

PROFILES = {
    "jlcpcb": {
        "_source": "jlcpcb.com/capabilities (standard PCB process)",
        "_date": CHECKED,
        "2": {"min_track": 0.10, "rec_track": 0.127, "min_space": 0.10, "rec_space": 0.127,
              "min_via_drill": 0.30, "min_via_diameter": 0.50, "min_annular": 0.10, "rec_annular": 0.15,
              "min_pth_drill": 0.30, "min_npth_drill": 0.50, "min_hole_to_hole": 0.50, "min_edge_clearance": 0.30,
              "silk_text_height": 1.0, "silk_line_width": 0.15, "outer_copper_mm": 0.035, "inner_copper_mm": 0.0175,
              "thicknesses": [0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0], "max_size_mm": [500, 400]},
        "4": {"min_track": 0.09, "rec_track": 0.10, "min_space": 0.09, "rec_space": 0.10,
              "min_via_drill": 0.20, "min_via_diameter": 0.45, "min_annular": 0.125, "rec_annular": 0.15,
              "min_pth_drill": 0.20, "min_npth_drill": 0.50, "min_hole_to_hole": 0.25, "min_edge_clearance": 0.30,
              "silk_text_height": 1.0, "silk_line_width": 0.15, "outer_copper_mm": 0.035, "inner_copper_mm": 0.0152,
              "thicknesses": [0.6, 0.8, 1.0, 1.2, 1.6, 2.0], "max_size_mm": [500, 400]},
    },
    "pcbway": {
        "_source": "pcbway.com capabilities (standard)",
        "_date": CHECKED,
        "2": {"min_track": 0.10, "rec_track": 0.15, "min_space": 0.10, "rec_space": 0.15,
              "min_via_drill": 0.20, "min_via_diameter": 0.45, "min_annular": 0.15, "rec_annular": 0.15,
              "min_pth_drill": 0.20, "min_npth_drill": 0.50, "min_hole_to_hole": 0.30, "min_edge_clearance": 0.30,
              "silk_text_height": 0.8, "silk_line_width": 0.15, "outer_copper_mm": 0.035, "inner_copper_mm": 0.0175,
              "thicknesses": [0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0, 2.4], "max_size_mm": [500, 1100]},
        "4": {"min_track": 0.09, "rec_track": 0.10, "min_space": 0.09, "rec_space": 0.10,
              "min_via_drill": 0.20, "min_via_diameter": 0.40, "min_annular": 0.10, "rec_annular": 0.15,
              "min_pth_drill": 0.20, "min_npth_drill": 0.50, "min_hole_to_hole": 0.25, "min_edge_clearance": 0.30,
              "silk_text_height": 0.8, "silk_line_width": 0.15, "outer_copper_mm": 0.035, "inner_copper_mm": 0.0175,
              "thicknesses": [0.6, 0.8, 1.0, 1.2, 1.6, 2.0, 2.4], "max_size_mm": [500, 1100]},
    },
    "oshpark": {
        "_source": "docs.oshpark.com/services (2-layer / 4-layer)",
        "_date": CHECKED,
        "2": {"min_track": 0.152, "rec_track": 0.152, "min_space": 0.152, "rec_space": 0.152,
              "min_via_drill": 0.254, "min_via_diameter": 0.508, "min_annular": 0.127, "rec_annular": 0.127,
              "min_pth_drill": 0.254, "min_npth_drill": 0.254, "min_hole_to_hole": 0.3, "min_edge_clearance": 0.381,
              "silk_text_height": 0.8, "silk_line_width": 0.127, "outer_copper_mm": 0.035, "inner_copper_mm": 0.0175,
              "thicknesses": [0.8, 1.6], "max_size_mm": [400, 400]},
        "4": {"min_track": 0.127, "rec_track": 0.127, "min_space": 0.127, "rec_space": 0.127,
              "min_via_drill": 0.254, "min_via_diameter": 0.508, "min_annular": 0.102, "rec_annular": 0.127,
              "min_pth_drill": 0.254, "min_npth_drill": 0.254, "min_hole_to_hole": 0.3, "min_edge_clearance": 0.381,
              "silk_text_height": 0.8, "silk_line_width": 0.127, "outer_copper_mm": 0.035, "inner_copper_mm": 0.0175,
              "thicknesses": [1.6], "max_size_mm": [400, 400]},
    },
}


def profile(house="jlcpcb", layers=2):
    h = PROFILES.get((house or "jlcpcb").lower(), PROFILES["jlcpcb"])
    key = "2" if layers <= 2 else "4"
    d = dict(h[key])
    d["_house"] = house
    d["_source"] = h["_source"]
    d["_date"] = h["_date"]
    d["_layers"] = layers
    return d


def capabilities(ctx):
    """The profile for the project's fab and layer count, with tracewright.json overrides."""
    layers = ctx.setting("fab.layers")
    if not layers and ctx.available("pcb"):
        layers = len(ctx.board.copper)
    d = profile(ctx.setting("fab.house", "jlcpcb"), int(layers or 2))
    d.update(ctx.setting("fab.rules", {}) or {})
    return d
