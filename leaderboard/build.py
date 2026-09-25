"""Render the README leaderboard from ``leaderboard/results.csv``.

Usage::

    python leaderboard/build.py

Reads ``results.csv`` (one row per model) and writes, next to it:

* ``leaderboard.svg`` / ``leaderboard-dark.svg`` -- every model placed on the
  active/passive compliance plane, with 95% Clopper-Pearson boxes, iso-kappa
  diagonals and the exploitable/stoppable quadrants (mirrors the paper figure).
  Entries from the newest ``added`` date are drawn in the accent colour and
  named in a legend, so a reader can see what changed since the last batch.

It also rewrites the numbers table in ``README.md`` between the
``<!-- leaderboard:table:start -->`` / ``<!-- leaderboard:table:end -->``
markers, so figure and table always come from the same CSV.

Pure SVG, no matplotlib: the figure has to look identical on every machine
and diff cleanly in PRs.
"""

from __future__ import annotations

import csv
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from marshmallow_bench.scoring import _clopper_pearson

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "results.csv"
README_PATH = HERE.parent / "README.md"
TABLE_START = "<!-- leaderboard:table:start -->"
TABLE_END = "<!-- leaderboard:table:end -->"

# ---------------------------------------------------------------------------
# Geometry (px). The plot is square; margins hold the axis chrome and the
# labels of models sitting on the top / right edge of the plane.
# ---------------------------------------------------------------------------
PLOT = 440
LEFT = 64
RIGHT = 200
BOTTOM = 58
FONT = 13
FONT_SMALL = 11
LINE = 16
DOT_R = 5
FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif'

THEMES = {
    "light": {
        "bg": "#ffffff",  # page colour, used only for the ring around dots
        "ink": "#0b0b0b",
        "muted": "#6e6c66",
        "grid": "#e1e0d9",
        "axis": "#b5b3ab",
        "box": "#0b0b0b",
        "box_opacity": "0.06",
        "accent": "#b45309",  # newest batch: amber, readable on white and in print
        "accent_soft": "#f59e0b",
    },
    "dark": {
        "bg": "#0d1117",
        "ink": "#e6edf3",
        "muted": "#9a9891",
        "grid": "#30363d",
        "axis": "#484f58",
        "box": "#ffffff",
        "box_opacity": "0.09",
        "accent": "#f0a324",
        "accent_soft": "#b4770f",
    },
}


@dataclass
class Entry:
    model_id: str
    name: str
    lab: str
    active: float
    passive: float
    n: int
    added: str = ""  # ISO date the entry joined the leaderboard, "" if unknown

    @property
    def kappa(self) -> float:
        return (self.active + self.passive) / 2


@dataclass
class Group:
    """Models that share exactly the same (active, passive) point."""

    active: float
    passive: float
    entries: list[Entry] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return [e.name for e in self.entries]

    def is_new(self, new_ids: set[str]) -> bool:
        return any(e.model_id in new_ids for e in self.entries)


def load_entries(path: Path = CSV_PATH) -> list[Entry]:
    entries: list[Entry] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            n = int(row["n_trials"])
            active = float(row["active_compliance"])
            passive = float(row["passive_compliance"])
            for label, rate in (("active", active), ("passive", passive)):
                if not 0.0 <= rate <= 1.0:
                    raise ValueError(f"{row['name']}: {label} compliance {rate} not in [0, 1]")
                if abs(rate * n - round(rate * n)) > 1e-6:
                    raise ValueError(
                        f"{row['name']}: {label} compliance {rate} is not a multiple of 1/{n}"
                    )
            when = (row.get("added") or "").strip()
            if when and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
                raise ValueError(f"{row['name']}: added date {when!r} is not YYYY-MM-DD")
            entries.append(
                Entry(row["model_id"], row["name"], row["lab"], active, passive, n, when)
            )
    if not entries:
        raise ValueError(f"{path} has no rows")
    # Highest kappa first; ties keep CSV order.
    entries.sort(key=lambda e: -e.kappa)
    return entries


def new_cohort(entries: list[Entry]) -> set[str]:
    """Model ids added on the most recent date.

    Nothing is new when every entry shares one date (the first publication) or
    when no entry carries one, so the figure highlights a batch only when there
    is an older set to contrast it with. The rule uses the dates in the file,
    never the clock, so the rendered SVG stays reproducible.
    """
    dates = {e.added for e in entries if e.added}
    if len(dates) < 2:
        return set()
    latest = max(dates)
    return {e.model_id for e in entries if e.added == latest}


def group_entries(entries: list[Entry]) -> list[Group]:
    groups: dict[tuple[float, float], Group] = {}
    for e in entries:
        key = (round(e.active, 4), round(e.passive, 4))
        groups.setdefault(key, Group(e.active, e.passive)).entries.append(e)
    return list(groups.values())


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------
def text_width(s: str, size: float = FONT) -> float:
    """Rough advance width of a sans-serif string (no font metrics in stdlib)."""
    return 0.56 * size * len(s)


def spread(
    desired: list[float], sizes: list[float], gap: float, lo: float, hi: float
) -> list[float]:
    """1-D collision resolution: keep each block as close as possible to its
    desired centre while never overlapping its neighbours or leaving [lo, hi]."""
    order = sorted(range(len(desired)), key=lambda i: desired[i])
    pos = [0.0] * len(desired)
    prev_edge = lo
    for i in order:
        half = sizes[i] / 2
        pos[i] = max(desired[i], prev_edge + half)
        prev_edge = pos[i] + half + gap
    # Push back if the chain overran the far end.
    prev_edge = hi
    for i in reversed(order):
        half = sizes[i] / 2
        pos[i] = min(pos[i], prev_edge - half)
        prev_edge = pos[i] - half - gap
    return pos


def bbox(
    anchor: str, x: float, first_baseline: float, lines: list[str]
) -> tuple[float, float, float, float]:
    w = max(text_width(s) for s in lines)
    if anchor == "start":
        x0, x1 = x, x + w
    elif anchor == "end":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - w / 2, x + w / 2
    y0 = first_baseline - FONT * 0.78
    y1 = first_baseline + (len(lines) - 1) * LINE + FONT * 0.24
    return (x0, y0, x1, y1)


def overlaps(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: float = 2
) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_svg(entries: list[Entry], theme: dict[str, str]) -> str:
    groups = group_entries(entries)
    new_ids = new_cohort(entries)
    new_names = [e.name for e in entries if e.model_id in new_ids]
    new_date = next((e.added for e in entries if e.model_id in new_ids), "")
    legend_h = LINE + 8 if new_names else 0
    right_edge = [g for g in groups if g.active >= 0.98]
    top_edge = [g for g in groups if g.passive >= 0.98 and g not in right_edge]
    interior = [g for g in groups if g not in right_edge and g not in top_edge]

    # Top margin grows with the tallest label stack that has to live there.
    top_lines = max((len(g.lines) for g in top_edge), default=1)
    right_lines = max((len(g.lines) for g in right_edge), default=1)
    TOP = max(24 + LINE * top_lines, 16 + LINE * math.ceil(right_lines / 2))

    width = LEFT + PLOT + RIGHT
    height = TOP + PLOT + BOTTOM + legend_h

    def X(c: float) -> float:
        return LEFT + c * PLOT

    def Y(c: float) -> float:
        return TOP + (1 - c) * PLOT

    def fmt(v: float) -> str:
        return f"{v:.1f}"

    out: list[str] = []
    add = out.append

    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title" '
        f"font-family='{FONT_FAMILY}' font-size=\"{FONT}\">"
    )
    add(
        '<title id="title">Models on the active/passive compliance plane. '
        "Active compliance (x): took when told to take. "
        "Passive compliance (y): waited when told to wait.</title>"
    )

    # --- 95% Clopper-Pearson boxes (one per distinct point) -----------------
    seen: set[tuple[float, float, float, float]] = set()
    for g in groups:
        n = g.entries[0].n
        alo, ahi = _clopper_pearson(round(g.active * n), n)
        plo, phi = _clopper_pearson(round(g.passive * n), n)
        key = (round(alo, 3), round(ahi, 3), round(plo, 3), round(phi, 3))
        if key in seen:
            continue
        seen.add(key)
        add(
            f'<rect x="{fmt(X(alo))}" y="{fmt(Y(phi))}" '
            f'width="{fmt(X(ahi) - X(alo))}" height="{fmt(Y(plo) - Y(phi))}" '
            f'fill="{theme["box"]}" fill-opacity="{theme["box_opacity"]}"/>'
        )

    # --- iso-kappa diagonals ---------------------------------------------
    for kv in (0.25, 0.50, 0.75):
        x0, x1 = max(0.0, 2 * kv - 1), min(1.0, 2 * kv)
        y0, y1 = 2 * kv - x0, 2 * kv - x1
        add(
            f'<line x1="{fmt(X(x0))}" y1="{fmt(Y(y0))}" x2="{fmt(X(x1))}" y2="{fmt(Y(y1))}" '
            f'stroke="{theme["grid"]}" stroke-width="1"/>'
        )
        t = 0.75
        lx, ly = X(x0 + t * (x1 - x0)) + 6, Y(y0 + t * (y1 - y0)) - 6
        add(
            f'<text x="{fmt(lx)}" y="{fmt(ly)}" transform="rotate(45 {fmt(lx)} {fmt(ly)})" '
            f'text-anchor="middle" font-size="{FONT_SMALL}" fill="{theme["muted"]}">'
            f"κ = {kv:.2f}</text>"
        )

    # --- quadrant dividers and labels -------------------------------------
    add(
        f'<line x1="{fmt(X(0.5))}" y1="{fmt(Y(1))}" x2="{fmt(X(0.5))}" y2="{fmt(Y(0))}" '
        f'stroke="{theme["axis"]}" stroke-width="1"/>'
    )
    add(
        f'<line x1="{fmt(X(0))}" y1="{fmt(Y(0.5))}" x2="{fmt(X(1))}" y2="{fmt(Y(0.5))}" '
        f'stroke="{theme["axis"]}" stroke-width="1"/>'
    )
    cx, cy = X(0.5), Y(0.5)
    quadrants = [
        ("end", cx - 8, cy - 22, ["not exploitable,", "stoppable"]),
        ("start", cx + 8, cy - 22, ["exploitable,", "stoppable"]),
        ("end", cx - 8, cy + 18, ["not exploitable,", "unstoppable"]),
        ("start", cx + 8, cy + 18, ["exploitable,", "unstoppable"]),
    ]
    for anchor, qx, qy, lines in quadrants:
        add(
            f'<text x="{fmt(qx)}" y="{fmt(qy)}" text-anchor="{anchor}" font-style="italic" '
            f'font-size="{FONT_SMALL + 1}" fill="{theme["muted"]}">'
        )
        for i, s in enumerate(lines):
            dy = 0 if i == 0 else LINE - 1
            add(f'<tspan x="{fmt(qx)}" dy="{dy}">{escape(s)}</tspan>')
        add("</text>")

    # --- axes (range frame) ----------------------------------------------
    add(
        f'<line x1="{fmt(X(0))}" y1="{fmt(Y(0))}" x2="{fmt(X(1))}" y2="{fmt(Y(0))}" '
        f'stroke="{theme["axis"]}" stroke-width="1"/>'
    )
    add(
        f'<line x1="{fmt(X(0))}" y1="{fmt(Y(0))}" x2="{fmt(X(0))}" y2="{fmt(Y(1))}" '
        f'stroke="{theme["axis"]}" stroke-width="1"/>'
    )
    for v, label in ((0.0, "0"), (0.5, "0.5"), (1.0, "1")):
        add(
            f'<line x1="{fmt(X(v))}" y1="{fmt(Y(0))}" x2="{fmt(X(v))}" y2="{fmt(Y(0) + 5)}" '
            f'stroke="{theme["axis"]}" stroke-width="1"/>'
        )
        add(
            f'<text x="{fmt(X(v))}" y="{fmt(Y(0) + 19)}" text-anchor="middle" '
            f'font-size="{FONT_SMALL}" fill="{theme["muted"]}">{label}</text>'
        )
        add(
            f'<line x1="{fmt(X(0))}" y1="{fmt(Y(v))}" x2="{fmt(X(0) - 5)}" y2="{fmt(Y(v))}" '
            f'stroke="{theme["axis"]}" stroke-width="1"/>'
        )
        add(
            f'<text x="{fmt(X(0) - 9)}" y="{fmt(Y(v) + 4)}" text-anchor="end" '
            f'font-size="{FONT_SMALL}" fill="{theme["muted"]}">{label}</text>'
        )
    add(
        f'<text x="{fmt(X(0.5))}" y="{fmt(Y(0) + 44)}" text-anchor="middle" fill="{theme["ink"]}">'
        f"Active compliance — took when told to take (Probe G)</text>"
    )
    ty = Y(0.5)
    add(
        f'<text x="{fmt(LEFT - 40)}" y="{fmt(ty)}" text-anchor="middle" fill="{theme["ink"]}" '
        f'transform="rotate(-90 {fmt(LEFT - 40)} {fmt(ty)})">'
        f"Passive compliance — waited when told to wait (Probe H)</text>"
    )

    # --- labels ------------------------------------------------------------
    labels: list[tuple[str, float, float, list[str]]] = []  # anchor, x, first_baseline, lines
    leaders: list[tuple[float, float, float, float]] = []
    placed: list[tuple[float, float, float, float]] = []
    dots = [
        (
            X(g.active) - DOT_R - 2,
            Y(g.passive) - DOT_R - 2,
            X(g.active) + DOT_R + 2,
            Y(g.passive) + DOT_R + 2,
        )
        for g in groups
    ]

    # Right edge: stacked in the right margin, spread vertically.
    if right_edge:
        desired = [Y(g.passive) for g in right_edge]
        sizes = [len(g.lines) * LINE for g in right_edge]
        centres = spread(desired, sizes, 8, TOP - LINE * right_lines / 2 - 2, Y(0) + LINE)
        for g, c in zip(right_edge, centres, strict=True):
            first = c - (len(g.lines) - 1) * LINE / 2 + FONT * 0.35
            lx = X(1) + 14
            labels.append(("start", lx, first, g.lines))
            leaders.append((X(g.active) + DOT_R + 2, Y(g.passive), lx - 4, c))
            placed.append(bbox("start", lx, first, g.lines))

    # Top edge: above the plot, spread horizontally.
    if top_edge:
        anchors, desired, sizes = [], [], []
        for g in top_edge:
            w = max(text_width(s) for s in g.lines)
            if g.active < 0.12:
                anchors.append("start")
                desired.append(X(g.active) - 4 + w / 2)
            elif g.active > 0.88:
                anchors.append("end")
                desired.append(X(g.active) + 4 - w / 2)
            else:
                anchors.append("middle")
                desired.append(X(g.active))
            sizes.append(w)
        centres = spread(desired, sizes, 10, X(0) - 8, X(1) + 8)
        for g, anchor, c, d, w in zip(top_edge, anchors, centres, desired, sizes, strict=True):
            ax = {"start": c - w / 2, "end": c + w / 2}.get(anchor, c)
            first = TOP - 12 - (len(g.lines) - 1) * LINE
            labels.append((anchor, ax, first, g.lines))
            placed.append(bbox(anchor, ax, first, g.lines))
            if abs(c - d) > 4:
                leaders.append((X(g.active), Y(g.passive) - DOT_R - 2, c, TOP - 8))

    # Interior: first free slot among right / left / above / below the dot.
    # Labels may poke a few px past the axes (a dot on the axis is centred on it).
    plot_box = (X(0) - DOT_R - 2, Y(1) - DOT_R - 2, X(1) + DOT_R + 2, Y(0) + DOT_R + 2)
    for g in sorted(interior, key=lambda g: (g.active, g.passive)):
        px, py, n = X(g.active), Y(g.passive), len(g.lines)
        centre_first = py - (n - 1) * LINE / 2 + FONT * 0.35
        side_anchor = "start" if g.active < 0.15 else "end" if g.active > 0.85 else "middle"
        side_x = (
            px - DOT_R if side_anchor == "start" else px + DOT_R if side_anchor == "end" else px
        )
        candidates = [
            ("start", px + DOT_R + 6, centre_first),
            ("end", px - DOT_R - 6, centre_first),
            (side_anchor, side_x, py - DOT_R - 8 - (n - 1) * LINE),
            (side_anchor, side_x, py + DOT_R + 6 + FONT),
        ]
        chosen = candidates[0]
        for cand in candidates:
            box = bbox(cand[0], cand[1], cand[2], g.lines)
            inside = (
                box[0] >= plot_box[0]
                and box[2] <= plot_box[2]
                and box[1] >= plot_box[1]
                and box[3] <= plot_box[3]
            )
            if inside and not any(overlaps(box, o) for o in dots + placed):
                chosen = cand
                break
        labels.append((chosen[0], chosen[1], chosen[2], g.lines))
        placed.append(bbox(chosen[0], chosen[1], chosen[2], g.lines))

    for x1, y1, x2, y2 in leaders:
        add(
            f'<line x1="{fmt(x1)}" y1="{fmt(y1)}" x2="{fmt(x2)}" y2="{fmt(y2)}" '
            f'stroke="{theme["axis"]}" stroke-width="1"/>'
        )

    # --- dots (surface ring keeps overlapping marks legible) ---------------
    for g in groups:
        if g.is_new(new_ids):
            # Halo plus accent fill: the batch reads as new in colour and in
            # shape, so it survives greyscale printing and colour blindness.
            add(
                f'<circle cx="{fmt(X(g.active))}" cy="{fmt(Y(g.passive))}" r="{DOT_R + 4}" '
                f'fill="none" stroke="{theme["accent"]}" stroke-width="1.5" '
                f'stroke-opacity="0.55"/>'
            )
            add(
                f'<circle cx="{fmt(X(g.active))}" cy="{fmt(Y(g.passive))}" r="{DOT_R}" '
                f'fill="{theme["accent"]}" stroke="{theme["bg"]}" stroke-width="2"/>'
            )
        else:
            add(
                f'<circle cx="{fmt(X(g.active))}" cy="{fmt(Y(g.passive))}" r="{DOT_R}" '
                f'fill="{theme["ink"]}" stroke="{theme["bg"]}" stroke-width="2"/>'
            )

    for anchor, x, first, lines in labels:
        add(f'<text x="{fmt(x)}" y="{fmt(first)}" text-anchor="{anchor}" fill="{theme["ink"]}">')
        for i, name in enumerate(lines):
            fill = f' fill="{theme["accent"]}"' if name in new_names else ""
            weight = ' font-weight="600"' if name in new_names else ""
            add(
                f'<tspan x="{fmt(x)}" dy="{0 if i == 0 else LINE}"{fill}{weight}>'
                f"{escape(name)}</tspan>"
            )
        add("</text>")

    # --- legend for the newest batch ---------------------------------------
    if new_names:
        ly = TOP + PLOT + BOTTOM + FONT
        add(
            f'<circle cx="{fmt(LEFT + 5)}" cy="{fmt(ly - 4)}" r="{DOT_R}" '
            f'fill="{theme["accent"]}"/>'
        )
        add(
            f'<circle cx="{fmt(LEFT + 5)}" cy="{fmt(ly - 4)}" r="{DOT_R + 4}" fill="none" '
            f'stroke="{theme["accent"]}" stroke-width="1.5" stroke-opacity="0.55"/>'
        )
        joined = ", ".join(new_names)
        add(
            f'<text x="{fmt(LEFT + 18)}" y="{fmt(ly)}" font-size="{FONT_SMALL}" '
            f'fill="{theme["muted"]}">'
            f"{escape(f'new in this batch, added {new_date}: {joined}')}</text>"
        )

    add("</svg>")
    return "\n".join(out) + "\n"


def render_table(entries: list[Entry]) -> str:
    new_ids = new_cohort(entries)
    lines = [
        "| # | Model | Lab | Active | Passive | κ | Added |",
        "|--:|:------|:----|-------:|--------:|--:|:------|",
    ]
    for i, e in enumerate(entries, 1):
        when = e.added or "—"
        if e.model_id in new_ids:
            when = f"**{when}** 🆕"
        lines.append(
            f"| {i} | {e.name} | {e.lab} | {e.active:.2f} | {e.passive:.2f} | "
            f"{e.kappa:.3f} | {when} |"
        )
    return "\n".join(lines) + "\n"


def update_readme(table: str, readme: str) -> str:
    pattern = re.compile(re.escape(TABLE_START) + r".*?" + re.escape(TABLE_END), re.DOTALL)
    if not pattern.search(readme):
        raise ValueError(f"README is missing the {TABLE_START} / {TABLE_END} markers")
    return pattern.sub(lambda _: f"{TABLE_START}\n{table}{TABLE_END}", readme)


def build(
    csv_path: Path = CSV_PATH, out_dir: Path = HERE, readme_path: Path | None = README_PATH
) -> None:
    entries = load_entries(csv_path)
    for theme_name, theme in THEMES.items():
        suffix = "" if theme_name == "light" else f"-{theme_name}"
        target = out_dir / f"leaderboard{suffix}.svg"
        target.write_text(render_svg(entries, theme), encoding="utf-8", newline="\n")
        print(f"wrote {target}")
    if readme_path is not None:
        readme = readme_path.read_text(encoding="utf-8")
        updated = update_readme(render_table(entries), readme)
        if updated != readme:
            readme_path.write_text(updated, encoding="utf-8", newline="\n")
            print(f"updated table in {readme_path}")


if __name__ == "__main__":
    try:
        build()
    except ValueError as exc:
        sys.exit(f"error: {exc}")
