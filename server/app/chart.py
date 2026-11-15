"""Activity charts: press counts and delivery delay per time bucket, laid out as SVG geometry.

Everything is computed in the display time zone on naive local wall-clock times.
Delay = time the server received a press minus the time the button was pressed.
"""
import math
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

GROUPS = ["hour", "day", "week", "month"]
MAX_BUCKETS = 400
W, H = 1000, 250                                   # SVG user units
LEFT, RIGHT, TOP, BOTTOM = 52, 8, 12, 28
MAX_BAR, GAP, RADIUS = 24, 2, 4
LATE_S = 60                                        # a press delivered later than this counts as "late"


def _floor(t: datetime, group: str) -> datetime:
    if group == "hour":
        return t.replace(minute=0, second=0, microsecond=0)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if group == "week":
        return day - timedelta(days=day.weekday())            # weeks start on Monday
    return day.replace(day=1) if group == "month" else day


def _next(t: datetime, group: str) -> datetime:
    if group == "month":
        return (t.replace(day=28) + timedelta(days=4)).replace(day=1)
    return t + {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}[group]


def _d(t: datetime) -> str:
    return f"{t:%b} {t.day}"


def _axis_label(t: datetime, group: str) -> str:
    if group == "hour":
        return f"{_d(t)} {t:%H:%M}"
    return f"{t:%b %Y}" if group == "month" else _d(t)


def _tip_label(t: datetime, group: str) -> str:
    if group == "hour":
        return f"{t:%a} {_d(t)}, {t:%H}:00 to {_next(t, group):%H}:00"
    if group == "day":
        return f"{t:%a} {_d(t)}, {t.year}"
    if group == "week":
        return f"Week of {_d(t)}, {t.year}"
    return f"{t:%B %Y}"


def fmt_delay(seconds: float | None) -> str:
    """0.42 -> '420 ms', 75 -> '1 min 15 s', 11520 -> '3 h 12 min'."""
    if seconds is None:
        return "unknown"
    if seconds < 1:
        return f"{round(seconds * 1000)} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60)} s"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60)} min"
    return f"{int(seconds // 86400)} d {int(seconds % 86400 // 3600)} h"


def _nice_step(top: float, whole: bool) -> float:
    """Smallest of 1, 2, 5 x 10^k that gives at most four grid steps."""
    raw = max(top, 1e-9) / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw - 1e-12)
    return max(1, round(step)) if whole else step


def _layout(starts: list, values: list, group: str, value_text, whole: bool, unit: str = "") -> dict:
    """values[i] is the bar height for bucket i, or None for no bar. value_text(i) is the tooltip headline."""
    peak = max((v for v in values if v is not None), default=0)
    step = _nice_step(peak if peak else 1, whole)
    top = max(step, math.ceil(peak / step - 1e-9) * step)
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM
    base = TOP + plot_h
    slot = plot_w / len(starts)
    bar_w = max(1.0, min(MAX_BAR, slot - GAP))
    bars = []
    for i, s in enumerate(starts):
        v = values[i]
        path = ""
        if v:
            h = max(1.0, plot_h * v / top)
            x = LEFT + i * slot + (slot - bar_w) / 2
            r = min(RADIUS, bar_w / 2, h)                    # rounded at the data end, square on the baseline
            path = (f"M{x:.1f},{base} V{base - h + r:.1f} Q{x:.1f},{base - h:.1f} {x + r:.1f},{base - h:.1f} "
                    f"H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{base - h:.1f} {x + bar_w:.1f},{base - h + r:.1f} V{base} Z")
        bars.append(dict(i=i, path=path, label=_tip_label(s, group), value=value_text(i),
                         hit_x=round(LEFT + i * slot, 1), hit_w=round(slot, 1)))
    every = max(1, -(-len(starts) // 8))             # about eight x labels
    ticks, v = [], 0.0
    while v <= top + 1e-9:
        ticks.append(dict(y=round(base - plot_h * v / top, 1), text=f"{v:g}{unit}"))
        v += step
    return dict(
        bars=bars, y_ticks=ticks, w=W, h=H, left=LEFT, right=W - RIGHT, top=TOP, base=base, plot_h=plot_h,
        x_labels=[dict(x=round(LEFT + (i + 0.5) * slot, 1), text=_axis_label(s, group))
                  for i, s in enumerate(starts) if i % every == 0])


def build(presses: list[tuple], start: str, end: str, group: str, tzname: str) -> dict | None:
    """presses: (pressed_at, received_at, approximate) in naive UTC, already filtered.
    start/end: ISO dates or ''. Returns None if there is nothing to draw."""
    tz = ZoneInfo(tzname)
    rows = [(p.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None),
             None if approx else max(0.0, (r - p).total_seconds())) for p, r, approx in presses]
    now = datetime.now(tz).replace(tzinfo=None)
    if not rows and not start:
        return None
    lo = datetime.combine(date.fromisoformat(start), datetime.min.time()) if start else min(t for t, _ in rows)
    hi = datetime.combine(date.fromisoformat(end), datetime.min.time()) + timedelta(days=1) if end else max(now, lo)
    if hi <= lo:
        return None

    span = hi - lo
    auto = "hour" if span <= timedelta(days=2) else "day" if span <= timedelta(days=92) else \
        "week" if span <= timedelta(days=730) else "month"
    group = group if group in GROUPS else auto
    while True:                                      # coarsen until the bars fit
        starts, t = [], _floor(lo, group)
        while t < hi and len(starts) <= MAX_BUCKETS:
            starts.append(t)
            t = _next(t, group)
        if len(starts) <= MAX_BUCKETS or group == "month":
            break
        group = GROUPS[GROUPS.index(group) + 1]

    index = {s: i for i, s in enumerate(starts)}
    counts = [0] * len(starts)
    delays: list[list[float]] = [[] for _ in starts]
    for t, delay in rows:
        i = index.get(_floor(t, group))
        if i is not None:
            counts[i] += 1
            if delay is not None:
                delays[i].append(delay)

    total, peak = sum(counts), max(counts)
    last = hi - timedelta(seconds=1)
    out = dict(group=group, total=total, peak=peak, tz=tzname,
               range=f"{_d(lo)}, {lo.year} to {_d(last)}, {last.year}",
               busiest=_tip_label(starts[counts.index(peak)], group) if peak else "")
    out["counts"] = _layout(starts, counts, group, lambda i: f"{counts[i]} press{'' if counts[i] == 1 else 'es'}", True)
    out["counts"]["table"] = [(b["label"], str(c)) for b, c in zip(out["counts"]["bars"], counts)]

    known = sorted(d for bucket in delays for d in bucket)
    if known:
        medians = [median(b) if b else None for b in delays]
        top = max(m for m in medians if m is not None)
        unit, div = ("ms", 0.001) if top < 2 else ("s", 1) if top < 120 else ("min", 60) if top < 7200 else ("h", 3600)

        def tip(i: int) -> str:
            return f"typical {fmt_delay(medians[i])}, slowest {fmt_delay(max(delays[i]))}" if delays[i] else "no timed presses"
        out["delay"] = _layout(starts, [None if m is None else m / div for m in medians], group, tip, False, f" {unit}")
        out["delay"]["table"] = [(b["label"], b["value"]) for b in out["delay"]["bars"] if "typical" in b["value"]]
        out["delay_stats"] = dict(
            typical=fmt_delay(median(known)), p95=fmt_delay(known[min(len(known) - 1, int(len(known) * 0.95))]),
            slowest=fmt_delay(known[-1]), late=sum(d > LATE_S for d in known), timed=len(known),
            untimed=total - sum(len(b) for b in delays))
    return out


def presets(tzname: str) -> list[tuple[str, str, str]]:
    """Quick date ranges as (label, start, end) ISO dates in the display time zone."""
    today = datetime.now(ZoneInfo(tzname)).date()
    out = [("Today", today, today)] + [(f"{n} days", today - timedelta(days=n - 1), today) for n in (7, 30, 90)]
    return [(label, a.isoformat(), b.isoformat()) for label, a, b in out] + [("All time", "", "")]
