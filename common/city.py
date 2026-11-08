"""City model shared by the control center (planning) and the testbench (plant).

20 intersections on a 4 x 5 grid. Rows are east-west arterials (the MAIN phase),
columns are north-south side streets (the SIDE phase). Eastbound is the
coordinated direction, so the upstream neighbour of (r, c) is (r, c-1).
Column 2 is also a north-south feeder: (r>=1, 2) gets a second upstream (r-1, 2).
That gives the brief's shape: most intersections one upstream, a few two,
and the corridor heads (column 0) none.
"""
from dataclasses import dataclass, field

ROWS, COLS = 4, 5
SPACING_M = 110.0          # block length; 110 m at 50 km/h is ~8 s travel
CYCLE_S = 40.0             # common background cycle on the shared clock
OFFSET_STEP_S = 8.0        # green-wave offset per column (= travel time)
N_CHANNELS = 100

# Per-intersection timing (seconds), identical for all controllers in this demo
TIMING = dict(
    cycle_s=CYCLE_S,
    main_min_s=14.0,       # end of base main green = decision point
    main_max_ext_s=8.0,    # main green can grow to 22 s (side keeps >= 8 s)
    side_min_s=8.0,
    amber_s=3.0,
    allred_s=2.0,
    sample_at_s=13.0,      # all 8 detectors sampled at this cycle instant
    upstream_wait_max_s=2.5,
)

# Facts from the brief (worst-case values assumed, see Task 2 Q2)
DELIVERY_INTRA_S = 1.0
DELIVERY_INTER_S = 3.0
DEADLINE_LOCAL_S = 2.0
DEADLINE_UPSTREAM_S = 5.0
CENTER_RTT_S = 10.0
BUFFER_SLOTS = 10
SYNC_TOLERANCE_S = 0.100   # neighbours aligned within 100 ms (Task 3)
EVAL_TIME_S = 0.05         # budget for rule evaluation on the controller

# Cabinets with poor sky view (urban canyon) -> no GNSS, must use PTP from peers
NO_GNSS = {8, 13, 14}

DETECTORS = {
    1: ("D1", "stop-bar, eastbound approach (west side)"),
    2: ("D2", "stop-bar, westbound approach (east side)"),
    3: ("D3", "stop-bar, northbound approach (south side)"),
    4: ("D4", "stop-bar, southbound approach (north side)"),
    5: ("D5", "exit, eastbound departures (east side)"),
    6: ("D6", "exit, westbound departures (west side)"),
    7: ("D7", "exit, northbound departures (north side)"),
    8: ("D8", "exit, southbound departures (south side)"),
}
MEASURES = {  # name -> (unit, min, max)
    "occupancy": ("%", 0, 100),
    "gap": ("s", 0, 99),
    "count": ("veh", 0, 50),
    "presence": ("", 0, 1),
}


def tsc_id(r: int, c: int) -> int:
    return r * COLS + c + 1


def rc(tid: int):
    return (tid - 1) // COLS, (tid - 1) % COLS


@dataclass
class Intersection:
    id: int
    row: int
    col: int
    upstream: list = field(default_factory=list)
    neighbours: list = field(default_factory=list)   # grid-adjacent (peer network)
    offset_s: float = 0.0
    channel: int = 0
    gnss: bool = True

    @property
    def name(self):
        return f"I{self.id}"


def build_city():
    city = {}
    for r in range(ROWS):
        for c in range(COLS):
            i = tsc_id(r, c)
            up = []
            if c >= 1:
                up.append(tsc_id(r, c - 1))
            if c == 2 and r >= 1:
                up.append(tsc_id(r - 1, c))
            nb = []
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < ROWS and 0 <= cc < COLS:
                    nb.append(tsc_id(rr, cc))
            city[i] = Intersection(
                id=i, row=r, col=c, upstream=up, neighbours=nb,
                offset_s=(c * OFFSET_STEP_S) % CYCLE_S, channel=i,
                gnss=i not in NO_GNSS)
    return city


CITY = build_city()


def upstream_exit_detector(receiver: int, upstream: int) -> int:
    """Exit detector at `upstream` that sees vehicles heading to `receiver`."""
    r1, c1 = rc(receiver)
    r0, c0 = rc(upstream)
    if r0 == r1 and c0 == c1 - 1:
        return 5   # eastbound departures
    if c0 == c1 and r0 == r1 - 1:
        return 8   # southbound departures
    raise ValueError(f"I{upstream} is not upstream of I{receiver}")


def city_json():
    return {
        "rows": ROWS, "cols": COLS, "spacing_m": SPACING_M,
        "timing": TIMING, "no_gnss": sorted(NO_GNSS),
        "intersections": [
            dict(id=x.id, row=x.row, col=x.col, upstream=x.upstream,
                 neighbours=x.neighbours, offset_s=x.offset_s,
                 channel=x.channel, gnss=x.gnss) for x in CITY.values()],
        "detectors": {k: v[1] for k, v in DETECTORS.items()},
    }
