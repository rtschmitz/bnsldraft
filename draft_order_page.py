# draft_order_page.py
from __future__ import annotations
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from flask import Blueprint, current_app, request, jsonify, render_template_string

from zoneinfo import ZoneInfo  # Python 3.9+
EASTERN = ZoneInfo("America/New_York")


# ===== Time rules =====
# Start: Nov 1, 2025 at 9:00 AM EST (fixed -05:00, no DST wiggles)
DRAFT_START = datetime(2025, 11, 1, 9, 0, 0, tzinfo=EASTERN)

# Draft window: 9am..6pm inclusive (10 normal picks/day), then the "end-of-day miss slot" at 7pm
DAY_FIRST_HOUR = 9
DAY_LAST_HOUR = 18  # 6pm
END_OF_DAY_MISS_HOUR = 19  # 7pm
PICKS_PER_DAY = DAY_LAST_HOUR - DAY_FIRST_HOUR + 1  # 10/hourly slots per day

order_bp = Blueprint("order_bp", __name__)

# Small HTML (kept here to avoid new templates folder)
ORDER_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Draft Order & Times</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 24px; }
    a { color: #184a7d; text-decoration: none; }
    .nav { margin-bottom: 16px; }
    .pill { padding: 6px 10px; border-radius: 999px; background: #f2f2f2; display: inline-block; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid #e5e5e5; padding: 8px 10px; text-align: left; }
    th { background: #fafafa; position: sticky; top: 0; z-index: 1; }
    .muted { color: #666; }
    .pagination { margin-top: 14px; display: flex; gap: 8px; align-items: center; }
    .btn { padding: 6px 10px; border: 1px solid #333; background: #fff; border-radius: 6px; cursor: pointer; }
    .btn[disabled]{opacity: 0.5; cursor: not-allowed;}
  </style>
</head>
<body>
  <div class="nav">
    <a href="/">← Back to Player Draft</a>
  </div>
  <h1>Draft Order & Times</h1>
  <p class="muted">Times shown in EST. Missed picks roll to the end of the day (7:00 PM). If that is missed, they roll to the end of the next day, and so on. Draft order never changes.</p>

  <table>
    <thead>
      <tr>
        <th style="width:12%;">Pick</th>
        <th style="width:28%;">Team</th>
        <th style="width:30%;">Time / Player</th>
        <th style="width:30%;">Status</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        <td>{{ row.pick_label }}</td>
        <td>{{ row.team }}</td>
        <td>
          {% if row.player %}
            {{ row.player }}
          {% else %}
            {{ row.time_display }}
          {% endif %}
        </td>
        <td class="muted">{{ row.status }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <div class="pagination">
    <form method="get">
      <input type="hidden" name="per" value="{{ per }}">
      <button class="btn" name="page" value="{{ prev_page }}" {% if prev_page < 1 %}disabled{% endif %}>Prev</button>
      <span>Page {{ page }} / {{ pages }}</span>
      <button class="btn" name="page" value="{{ next_page }}" {% if next_page > pages %}disabled{% endif %}>Next</button>
    </form>
  </div>
</body>
</html>
"""

def get_conn() -> sqlite3.Connection:
    # reuse the app's DB path via current_app.config
    conn = sqlite3.connect(current_app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn

def base_slot_for_index(i: int) -> datetime:
    """
    Initial designated time for pick index i (0-based) ignoring misses.
    9am..6pm hourly; after 6pm, next day 9am.
    """
    day_idx, offset_in_day = divmod(i, PICKS_PER_DAY)
    slot_hour = DAY_FIRST_HOUR + offset_in_day
    start_day = (DRAFT_START + timedelta(days=day_idx)).date()
    return datetime(start_day.year, start_day.month, start_day.day, slot_hour, 0, 0, tzinfo=EASTERN)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            dob TEXT,
            position TEXT,
            franchise TEXT,
            eligible INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_order (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round INTEGER NOT NULL,
            pick INTEGER NOT NULL,
            team TEXT NOT NULL,
            player_id INTEGER,
            drafted_at TEXT,
            label TEXT,                         -- NEW: human display (e.g., 'C2.01')
            UNIQUE(round, pick) ON CONFLICT IGNORE
        )
        """
    )
    # Ensure 'label' exists if table pre-existed
    cur.execute("PRAGMA table_info(draft_order)")
    cols = {row[1] for row in cur.fetchall()}
    if "label" not in cols:
        cur.execute("ALTER TABLE draft_order ADD COLUMN label TEXT")
    conn.commit()
    conn.close()


def end_of_day(dt: datetime) -> datetime:
    """Return the end-of-day miss slot at 7pm for the date of dt."""
    d = dt.date()
    return datetime(d.year, d.month, d.day, END_OF_DAY_MISS_HOUR, 0, 0, tzinfo=EASTERN)

def end_of_next_day(dt: datetime) -> datetime:
    """7pm on the next calendar day."""
    return end_of_day(dt + timedelta(days=1))

def fmt_est(dt: datetime) -> str:
    return dt.strftime("%a %b %-d, %Y • %-I:%M %p ET")

def compute_rows(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    DST-aware Eastern schedule with miss rollups:
      • Base schedule: hourly 9:00–18:00 local (10 slots/day).
      • A pick is 'missed' if not selected by the *following pick's designated time*.
        (Designated time = override if present, else base slot.)
      • Same-day misses queue at 19:00, 20:00, 21:00, … in miss order.
      • If an evening slot is also missed (no selection by its *next* pick time),
        it moves to the END of the NEXT day’s evening queue, after that day’s same-day misses.
      • Draft order never changes.
    """
    if now is None:
        now = datetime.now(tz=EASTERN)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, round, pick, team, player_id, drafted_at, label
      FROM draft_order
      ORDER BY round ASC, pick ASC
    """)    
    picks = cur.fetchall()

    # Player names
    cur.execute("SELECT id, name FROM players")
    player_name_by_id = {r["id"]: r["name"] for r in cur.fetchall()}

    # --- manual overrides table (if present) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pick_overrides (
            draft_order_id INTEGER PRIMARY KEY,
            scheduled_time TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.execute("SELECT draft_order_id, scheduled_time FROM pick_overrides")
    overrides_raw = cur.fetchall()
    conn.close()

    # Normalize overrides to Eastern
    overrides: Dict[int, datetime] = {}
    for r in overrides_raw:
        try:
            dt = datetime.fromisoformat(r["scheduled_time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            overrides[r["draft_order_id"]] = dt.astimezone(EASTERN)
        except Exception:
            pass

    total = len(picks)

    # 1) Compute each pick's DESIGNATED time = override if present, else base slot.
    designated: List[datetime] = []
    for idx, rec in enumerate(picks):
        bt = base_slot_for_index(idx)
        designated.append(overrides.get(rec["id"], bt))

    # 2) Next-deadline for pick i is the designated time of i+1 (very important!)
    next_deadlines: List[datetime] = []
    for i in range(total):
        if i + 1 < total:
            next_deadlines.append(designated[i + 1])
        else:
            # last pick: no "next pick" deadline
            next_deadlines.append(designated[i] + timedelta(days=36500))

    # 3) Classify undrafted picks: missed vs not-yet-missed (as of now)
    undrafted_idxs = [i for i, r in enumerate(picks) if not r["player_id"]]
    missed_by_day: Dict[tuple[int,int,int], List[int]] = {}  # key by (Y,M,D) to avoid tz drift
    scheduled_time: Dict[int, datetime] = {}
    overridden_idx: set[int] = set()

    # If a pick itself has an override, that is its current designated time for display,
    # but it still becomes 'missed' based on the NEXT pick's designated time.
    for i in undrafted_idxs:
        rec = picks[i]
        if rec["id"] in overrides:
            overridden_idx.add(i)

    for i in undrafted_idxs:
        if now >= next_deadlines[i]:
            d = designated[i].astimezone(EASTERN).date()
            missed_by_day.setdefault((d.year, d.month, d.day), []).append(i)
        else:
            # Not missed: show the pick's designated time (override or base)
            scheduled_time[i] = designated[i]

    # 4) Build evening queues day-by-day
    carryover_eod: List[int] = []  # re-missed evening picks → next day's evening
    # Start iterating from the earliest day that appears in the schedule (min of designated/base)
    earliest_day = (min(designated).astimezone(EASTERN)).date() if designated else DRAFT_START.date()
    day = earliest_day
    safety_days = 0

    while len(scheduled_time) < len(undrafted_idxs) and safety_days < 3650:
        todays_misses = missed_by_day.get((day.year, day.month, day.day), [])
        evening_queue = todays_misses + carryover_eod
        new_carryover: List[int] = []

        for j, idx in enumerate(evening_queue):
            # If the pick has a manual override in the future, keep its designated override for display
            # but it can STILL re-miss if that override is before now+next slot. We keep it simple:
            # evening queue only applies to actually missed picks (we're already here),
            # so assign an evening slot unless it's re-missed immediately.
            slot_dt = datetime(day.year, day.month, day.day, END_OF_DAY_MISS_HOUR + j, 0, 0, tzinfo=EASTERN)

            # The "next pick time" for this evening slot:
            if j + 1 < len(evening_queue):
                next_deadline = datetime(day.year, day.month, day.day, END_OF_DAY_MISS_HOUR + j + 1, 0, 0, tzinfo=EASTERN)
            else:
                nd = day + timedelta(days=1)
                next_deadline = datetime(nd.year, nd.month, nd.day, DAY_FIRST_HOUR, 0, 0, tzinfo=EASTERN)

            if now >= next_deadline:
                # Re-missed → push to next day evening tail
                new_carryover.append(idx)
            else:
                scheduled_time[idx] = slot_dt

        carryover_eod = new_carryover
        day = day + timedelta(days=1)
        safety_days += 1

    # Any leftovers: put on the next day 7pm onwards (extreme backstop)
    if carryover_eod:
        for j, idx in enumerate(carryover_eod):
            nd = day
            slot_dt = datetime(nd.year, nd.month, nd.day, END_OF_DAY_MISS_HOUR + j, 0, 0, tzinfo=EASTERN)
            scheduled_time[idx] = slot_dt

    # 5) Build rows
    rows: List[Dict[str, Any]] = []
    for i, rec in enumerate(picks):
        pick_label = rec["label"] or f"{rec['round']}.{rec['pick']}"
        if rec["player_id"]:
            player = player_name_by_id.get(rec["player_id"], f"Player #{rec['player_id']}")
            status = f"Selected at {rec['drafted_at'] or '—'}"
            rows.append({
                "pick_label": pick_label,
                "team": rec["team"],
                "player": player,
                "time_display": "",
                "status": status,
            })
        else:
            t = scheduled_time.get(i, designated[i])  # always show designated/scheduled
            is_evening = t.astimezone(EASTERN).hour >= END_OF_DAY_MISS_HOUR
            status_txt = "Missed → end of day" if is_evening and now >= designated[i] else "Scheduled"
            # Mark manual debug overrides explicitly
            if rec["id"] in overrides and t == overrides[rec["id"]]:
                status_txt = "Overridden (debug)"
            rows.append({
                "pick_label": pick_label,
                "team": rec["team"],
                "player": None,
                "time_display": fmt_est(t),
                "status": status_txt,
            })

    return rows

def _load_picks_and_overrides():
    """Internal: returns (picks_rows, designated_times, next_deadlines) using overrides when present."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, round, pick, team, player_id, drafted_at, label
      FROM draft_order
      ORDER BY round ASC, pick ASC
    """)    
    picks = cur.fetchall()

    # overrides
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pick_overrides (
            draft_order_id INTEGER PRIMARY KEY,
            scheduled_time TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.execute("SELECT draft_order_id, scheduled_time FROM pick_overrides")
    overrides_raw = cur.fetchall()
    conn.close()

    overrides: Dict[int, datetime] = {}
    for r in overrides_raw:
        try:
            dt = datetime.fromisoformat(r["scheduled_time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            overrides[r["draft_order_id"]] = dt.astimezone(EASTERN)
        except Exception:
            pass

    # designated = override if present else base slot
    designated: List[datetime] = []
    for idx, rec in enumerate(picks):
        bt = base_slot_for_index(idx)
        designated.append(overrides.get(rec["id"], bt))

    # next deadline = next pick’s designated time (very important for misses)
    next_deadlines: List[datetime] = []
    for i in range(len(picks)):
        if i + 1 < len(picks):
            next_deadlines.append(designated[i + 1])
        else:
            next_deadlines.append(designated[i] + timedelta(days=36500))

    return picks, designated, next_deadlines


def get_current_on_clock_pick(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Current pick = the undrafted pick with the *earliest scheduled time*
    (considering overrides, misses -> evening queue, and re-miss carryover).
    Managers can pick early, so we return it even if its scheduled time is in the future.
    """
    if now is None:
        now = datetime.now(tz=EASTERN)

    picks, designated = _load_picks_overrides_and_designated()
    scheduled_time = _compute_scheduled_times(now)

    # Among undrafted picks, select the one with the minimum scheduled_time; tie-breaker: draft order
    best_idx = None
    best_key = None
    for i, rec in enumerate(picks):
        if rec["player_id"]:
            continue
        t = scheduled_time.get(i, designated[i])
        key = (t, i)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = i

    if best_idx is None:
        return None

    rec = picks[best_idx]
    return {"id": rec["id"], "round": rec["round"], "pick": rec["pick"], "team": rec["team"]}


def get_current_pick_info(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Returns info about the *current* pick by earliest scheduled time, plus its deadline:
      {
        "id", "round", "pick", "team",
        "pick_label",           # e.g., "3.3"
        "scheduled_time_iso",   # ISO 8601, Eastern local
        "deadline_time_iso"     # ISO 8601, Eastern local (designated time of next pick in ORDER)
      }
    Managers can pick early; we still return the earliest-by-time pick even if its scheduled time is future.
    """
    if now is None:
        now = datetime.now(tz=EASTERN)

    picks, designated = _load_picks_overrides_and_designated()
    if not picks:
        return None

    scheduled_time = _compute_scheduled_times(now)

    best_idx = None
    best_key = None
    for i, rec in enumerate(picks):
        if rec["player_id"]:
            continue
        t = scheduled_time.get(i, designated[i])
        key = (t, i)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = i

    if best_idx is None:
        return None

    rec = picks[best_idx]
    lbl = f"{rec['round']}.{rec['pick']}"
    sched = (scheduled_time.get(best_idx, designated[best_idx])).astimezone(EASTERN)

    if best_idx + 1 < len(picks):
        deadline = designated[best_idx + 1].astimezone(EASTERN)
        deadline_iso = deadline.isoformat(timespec="minutes")
    else:
        deadline_iso = None

    return {
        "id": rec["id"],
        "round": rec["round"],
        "pick": rec["pick"],
        "team": rec["team"],
        "pick_label": lbl,
        "scheduled_time_iso": sched.isoformat(timespec="minutes"),
        "deadline_time_iso": deadline_iso,
    }


# --- Shared scheduling helpers for main draft page ---

def _load_picks_overrides_and_designated():
    """Return (picks_rows, designated_times:list[datetime]) where designated=override or base slot."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, round, pick, team, player_id, drafted_at, label
      FROM draft_order
      ORDER BY round ASC, pick ASC
    """)    
    picks = cur.fetchall()

    # load overrides
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pick_overrides (
            draft_order_id INTEGER PRIMARY KEY,
            scheduled_time TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.execute("SELECT draft_order_id, scheduled_time FROM pick_overrides")
    overrides_raw = cur.fetchall()
    conn.close()

    overrides: Dict[int, datetime] = {}
    for r in overrides_raw:
        try:
            dt = datetime.fromisoformat(r["scheduled_time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            overrides[r["draft_order_id"]] = dt.astimezone(EASTERN)
        except Exception:
            pass

    designated: List[datetime] = []
    for idx, rec in enumerate(picks):
        bt = base_slot_for_index(idx)
        designated.append(overrides.get(rec["id"], bt))
    return picks, designated


def _compute_scheduled_times(now: datetime) -> Dict[int, datetime]:
    """
    For each undrafted pick (by index), compute the *current* scheduled time:
      - Start at designated times (override or base).
      - If 'missed' (now >= next pick's designated time), move to evening queue:
          same day 7pm, 8pm, ... in miss order.
      - If an evening slot is also missed, cascade to the *next day's* evening tail.
      - Always return a time that is >= now or the next valid slot in the future.
    """
    picks, designated = _load_picks_overrides_and_designated()
    total = len(picks)

    # Next pick deadlines by designated time
    next_deadlines: List[datetime] = []
    for i in range(total):
        if i + 1 < total:
            next_deadlines.append(designated[i + 1])
        else:
            next_deadlines.append(designated[i] + timedelta(days=36500))

    undrafted_idxs = [i for i, r in enumerate(picks) if not r["player_id"]]

    # Classify initial misses by the *day of the pick's designated time*
    missed_by_day: Dict[tuple[int,int,int], List[int]] = {}
    scheduled_time: Dict[int, datetime] = {}

    for i in undrafted_idxs:
        if now >= next_deadlines[i]:
            d = designated[i].astimezone(EASTERN).date()
            missed_by_day.setdefault((d.year, d.month, d.day), []).append(i)
        else:
            scheduled_time[i] = designated[i]

    # Evening queues day-by-day, with carryover for re-misses
    if designated:
        day = (min(designated).astimezone(EASTERN)).date()
    else:
        day = DRAFT_START.date()

    carryover_eod: List[int] = []
    safety_days = 0
    while len(scheduled_time) < len(undrafted_idxs) and safety_days < 3650:
        todays_misses = missed_by_day.get((day.year, day.month, day.day), [])
        evening_queue = todays_misses + carryover_eod
        new_carryover: List[int] = []

        for j, idx in enumerate(evening_queue):
            slot_dt = datetime(day.year, day.month, day.day, END_OF_DAY_MISS_HOUR + j, 0, 0, tzinfo=EASTERN)
            # Next pick time for this evening slot:
            if j + 1 < len(evening_queue):
                next_deadline = datetime(day.year, day.month, day.day, END_OF_DAY_MISS_HOUR + j + 1, 0, 0, tzinfo=EASTERN)
            else:
                nd = day + timedelta(days=1)
                next_deadline = datetime(nd.year, nd.month, nd.day, DAY_FIRST_HOUR, 0, 0, tzinfo=EASTERN)

            if now >= next_deadline:
                new_carryover.append(idx)  # re-missed -> push to next day tail
            else:
                scheduled_time[idx] = slot_dt

        carryover_eod = new_carryover
        day = day + timedelta(days=1)
        safety_days += 1

    # Any leftovers (extreme): shove onto the next day starting 7pm onward
    if carryover_eod:
        for j, idx in enumerate(carryover_eod):
            nd = day
            scheduled_time[idx] = datetime(nd.year, nd.month, nd.day, END_OF_DAY_MISS_HOUR + j, 0, 0, tzinfo=EASTERN)

    return scheduled_time


# --------- Routes ----------

@order_bp.route("/order")
def order_page():
    # simple server-side pagination
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per = max(5, min(50, int(request.args.get("per", "25"))))
    except ValueError:
        per = 25

    rows = compute_rows()
    total = len(rows)
    pages = max(1, math.ceil(total / per))
    page = min(page, pages)

    start = (page - 1) * per
    end = start + per
    page_rows = rows[start:end]

    return render_template_string(
        ORDER_HTML,
        rows=page_rows,
        page=page, per=per, pages=pages,
        prev_page=page - 1, next_page=page + 1
    )

@order_bp.get("/api/order")
def api_order():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per = max(5, min(100, int(request.args.get("per", "50"))))
    except ValueError:
        per = 50

    rows = compute_rows()
    total = len(rows)
    pages = max(1, math.ceil(total / per))
    page = min(page, pages)
    start = (page - 1) * per
    end = start + per
    return jsonify({
        "page": page,
        "per": per,
        "pages": pages,
        "total": total,
        "rows": rows[start:end],
    })

