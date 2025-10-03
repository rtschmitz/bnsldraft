#!/usr/bin/env python3
"""
Flask Draft App — Baseball Draft Framework
-----------------------------------------
A single-file Flask application to run a simple league draft with:
- CSV import for players and draft order
- Substring search over player names
- Team selection dropdown (MLB teams)
- Current pick tracking (round/pick/team)
- Draft button appears *only* when your selected team is on the clock
- Owned players are grayed out, with a toggle to hide them

Quickstart
==========
1) Ensure you have Flask installed:  
   pip install Flask

2) Save this file as app.py and run it:  
   python app.py

3) On first run, if no `players.csv` and `draft_order.csv` are present, 
   sample CSVs will be generated automatically for a demo 8-team, 10-round draft.

4) Visit http://127.0.0.1:5000/ in your browser.

CSV Schemas
===========
players.csv columns (exact, case-insensitive accepted):
- Player name
- DOB
- position
- franchise
- Draft eligible?

Note: `franchise` can be blank for undrafted players. `Draft eligible?` can be true/false, 1/0, yes/no.

draft_order.csv columns:
- Round
- Pick
- Team

Database
========
- SQLite file `draft.db` is created beside this script.
- Tables: players, draft_order
- `draft_order.player_id` is NULL until the pick is made; after drafting, it stores the chosen player's id.

To reset: delete `draft.db` and restart the app (it will re-import CSVs / regenerate sample CSVs if missing).
"""

from __future__ import annotations
import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List
from draft_order_page import order_bp

from flask import (
    Flask, request, jsonify, session, redirect, url_for, render_template_string, abort
)

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "draft.db"
PLAYERS_CSV = APP_DIR / "players.csv"
DRAFT_ORDER_CSV = APP_DIR / "draft_order.csv"


app = Flask(__name__)
app.config["DB_PATH"] = str(DB_PATH)
app.register_blueprint(order_bp)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

MLB_TEAMS = [
    "Arizona Diamondbacks","Atlanta Braves","Baltimore Orioles","Boston Red Sox",
    "Chicago Cubs","Chicago White Sox","Cincinnati Reds","Cleveland Guardians",
    "Colorado Rockies","Detroit Tigers","Houston Astros","Kansas City Royals",
    "Los Angeles Angels","Los Angeles Dodgers","Miami Marlins","Milwaukee Brewers",
    "Minnesota Twins","New York Mets","New York Yankees","Oakland Athletics",
    "Philadelphia Phillies","Pittsburgh Pirates","San Diego Padres","San Francisco Giants",
    "Seattle Mariners","St. Louis Cardinals","Tampa Bay Rays","Texas Rangers",
    "Toronto Blue Jays","Washington Nationals",
]


# Map all teams to four test emails (round-robin) so you can exercise the system easily.
_TEST_EMAILS = [
    "ryanschmitz43@yahoo.com",
    "ryanschmitz43@gmail.com",
    "schmitz@ucsb.edu",
    "condor2199@yahoo.com",
]
TEAM_EMAILS = {team: _TEST_EMAILS[i % len(_TEST_EMAILS)] for i, team in enumerate(MLB_TEAMS)}


# -------------------------
# Utility Helpers
# -------------------------

import smtplib
from email.message import EmailMessage

def init_meta():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT
      )
    """)
    conn.commit()
    conn.close()

def get_meta(key: str) -> str | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_meta WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_meta(key: str, value: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO app_meta(key, value) VALUES(?,?)
      ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()

def send_email(to_addr: str, subject: str, body: str):
    """
    Minimal SMTP sender. Configure via env:
      SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM
    Extra toggles:
      SMTP_STARTTLS=1 (default) -> attempt STARTTLS if server advertises it
      SMTP_REQUIRE_TLS=0 (default) -> if 1, fail if STARTTLS not available
    Falls back to console print if SMTP env isn’t provided.
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "0") or "0")
    user = os.environ.get("SMTP_USERNAME")
    pw   = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM", "draftbot@localhost")

    if not host or not port:
        print(f"[EMAIL-DRYRUN] To: {to_addr}\nSubject: {subject}\n\n{body}\n---")
        return

    starttls_wanted = (os.environ.get("SMTP_STARTTLS", "1").lower() in ("1", "true", "yes"))
    require_tls     = (os.environ.get("SMTP_REQUIRE_TLS", "0").lower() in ("1", "true", "yes"))

    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    import smtplib
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.ehlo()
        if starttls_wanted:
            try:
                if s.has_extn("starttls"):
                    s.starttls()
                    s.ehlo()
                elif require_tls:
                    raise RuntimeError("STARTTLS required but not supported by server")
            except Exception as e:
                if require_tls:
                    raise
                # Otherwise continue without TLS in dev
                print(f"[EMAIL] STARTTLS not used: {e}")
        if user and pw:
            s.login(user, pw)
        s.send_message(msg)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def emails_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()

from zoneinfo import ZoneInfo
EASTERN = ZoneInfo("America/New_York")

def fmt_email_et(dt: datetime) -> str:
    """Return 'Fri, Nov 1, 2025 at 1:00 PM ET' in local Eastern time, no ISO."""
    local = dt.astimezone(EASTERN)
    # Try Unix-style %-d / %-I (no leading zeros); fall back on Windows-friendly format
    try:
        return local.strftime("%a, %b %-d, %Y at %-I:%M %p ET")
    except ValueError:
        # Windows doesn't support %-d / %-I; use zero-padded and tidy up.
        s = local.strftime("%a, %b %d, %Y at %I:%M %p ET")
        # remove leading zeros from day/hour (cosmetic)
        s = s.replace(" 0", " ")
        if s.startswith("0"):
            s = s[1:]
        return s


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
            UNIQUE(round, pick) ON CONFLICT IGNORE
        )
        """
    )
    conn.commit()
    conn.close()


def str_to_bool(x: str) -> bool:
    if x is None:
        return False
    s = str(x).strip().lower()
    return s in {"1","true","t","yes","y"}


def normalized_header_map(header: List[str]) -> Dict[str, int]:
    idx = {}
    for i, h in enumerate(header):
        key = h.strip().lower().replace("_", " ")
        idx[key] = i
    return idx


def import_players_from_csv(path: Path):
    conn = get_conn()
    cur = conn.cursor()

    if not path.exists():
        return

    with path.open(newline='', encoding='utf-8') as f:
        r = csv.reader(f)
        header = next(r)
        h = normalized_header_map(header)

        # Accept a few synonyms leniently
        name_key = next((k for k in h if k in {"player name","name"}), None)
        dob_key = next((k for k in h if k in {"dob","date of birth"}), None)
        pos_key = next((k for k in h if k in {"position"}), None)
        fr_key = next((k for k in h if k in {"franchise","team"}), None)
        el_key = next((k for k in h if k in {"draft eligible?","draft eligible","eligible"}), None)
        if not name_key or not pos_key or el_key is None:
            raise RuntimeError("players.csv missing required columns: Player name, position, Draft eligible?")

        for row in r:
            name = row[h[name_key]].strip()
            dob = row[h[dob_key]].strip() if dob_key else ""
            pos = row[h[pos_key]].strip()
            franchise = row[h[fr_key]].strip() if fr_key else ""
            eligible = 1 if str_to_bool(row[h[el_key]]) else 0
            cur.execute(
                "INSERT INTO players(name, dob, position, franchise, eligible) VALUES (?,?,?,?,?)",
                (name, dob, pos, franchise, eligible)
            )
    conn.commit()
    conn.close()


def import_draft_order_from_csv(path: Path):
    conn = get_conn()
    cur = conn.cursor()

    if not path.exists():
        return

    with path.open(newline='', encoding='utf-8') as f:
        r = csv.reader(f)
        header = next(r)
        h = normalized_header_map(header)

        round_key = next((k for k in h if k == "round"), None)
        pick_key = next((k for k in h if k == "pick"), None)
        team_key = next((k for k in h if k == "team"), None)
        if not (round_key and pick_key and team_key):
            raise RuntimeError("draft_order.csv missing required columns: Round, Pick, Team")

        for row in r:
            rnum = int(row[h[round_key]].strip())
            pnum = int(row[h[pick_key]].strip())
            team = row[h[team_key]].strip()
            cur.execute(
                "INSERT OR IGNORE INTO draft_order(round, pick, team) VALUES (?,?,?)",
                (rnum, pnum, team)
            )
    conn.commit()
    conn.close()


def get_current_pick() -> Dict[str, Any] | None:
    # Import here to avoid circulars at import time; we already registered the blueprint above.
    from draft_order_page import get_current_on_clock_pick
    current = get_current_on_clock_pick()  # time-aware, respects overrides + misses
    if current is None:
        return None

    # keep progress stats the same
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM draft_order")
    total_picks = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM draft_order WHERE player_id IS NOT NULL")
    made = cur.fetchone()[0]
    conn.close()

    current.update({
        "picks_made": made,
        "total_picks": total_picks,
    })
    return current

def notify_if_new_on_clock():
    """
    If the 'current pick' (earliest scheduled undrafted pick) changed since last notification,
    send an email to that team's address with the pick label and deadline.
    """
    # Import here to avoid circular import at module import time
    from draft_order_page import get_current_pick_info, EASTERN

    info = get_current_pick_info()
    if not info:
        return

    last_id = get_meta("last_on_clock_pick_id")
    cur_id = str(info["id"])

    if last_id == cur_id:
        return  # nothing new

    # Compose email
    team = info["team"]
    pick_label = info["pick_label"]
    deadline_iso = info["deadline_time_iso"]  # can be None for last pick

    pretty_deadline = None
    if deadline_iso:
        dt = datetime.fromisoformat(deadline_iso)
        pretty_deadline = fmt_email_et(dt)

    to_addr = TEAM_EMAILS.get(team)

    # Subject: keep concise; (optional) swap to pretty_deadline if you prefer
    subj_deadline = f" due {pretty_deadline}" if pretty_deadline else ""
    subject = f"BNSL Draft: Pick {pick_label}{subj_deadline}"

    # Body: use human-friendly ET string (no ISO T/offset gobbledygook)
    if pretty_deadline:
        body_deadline = f"Deadline: {pretty_deadline}\n"
    else:
        body_deadline = ""

    body = (
        f"You are on the clock for pick {pick_label} ({team}).\n"
        f"{body_deadline}\n"
        "Submit your selection at http://127.0.0.1:5000/ (Draft page).\n"
    )

    if to_addr:
        send_email(to_addr, subject, body)

    set_meta("last_on_clock_pick_id", cur_id)


def generate_sample_csvs(players_path: Path, order_path: Path):
    """Create small demo CSVs if not present: 8 teams x 10 rounds, ~36 sample players."""
    if not players_path.exists():
        sample_players = [
            ["Player name","DOB","position","franchise","Draft eligible?"],
            ["Jackson Miller","1998-05-14","C","", "true"],
            ["Ethan Carter","1997-07-02","1B","", "true"],
            ["Liam Rodriguez","1999-09-20","2B","", "true"],
            ["Noah Kim","1996-03-08","SS","", "true"],
            ["Mason Thompson","1995-11-25","3B","", "true"],
            ["Logan Rivera","1999-12-01","OF","", "true"],
            ["Carter James","1998-02-17","OF","", "true"],
            ["Aiden Brooks","2000-06-30","OF","", "true"],
            ["Oliver Chen","1997-01-10","SP","", "true"],
            ["Elijah Nguyen","1998-08-22","SP","", "true"],
            ["Lucas Patel","1996-10-05","SP","", "true"],
            ["Alexander Park","1995-04-18","RP","", "true"],
            ["Henry Scott","1994-12-29","RP","", "true"],
            ["Levi Turner","1997-09-13","C","", "true"],
            ["Mateo Garcia","1998-07-07","1B","", "true"],
            ["Sebastian Flores","1997-05-03","2B","", "true"],
            ["Benjamin Diaz","1999-10-12","SS","", "true"],
            ["Daniel Ortiz","1996-02-26","3B","", "true"],
            ["Michael Hughes","1995-08-09","OF","", "true"],
            ["Samuel Reed","1998-03-15","OF","", "true"],
            ["Wyatt Morales","1999-01-22","OF","", "true"],
            ["Julian Rivera","1997-11-04","SP","", "true"],
            ["Grayson Lee","1996-06-16","SP","", "true"],
            ["Hudson Clark","1995-09-27","SP","", "true"],
            ["Asher Gomez","1998-12-19","RP","", "true"],
            ["Caleb Perry","1997-04-08","RP","", "true"],
            ["Nolan Baker","1999-07-01","C","", "true"],
            ["Easton Ward","1996-05-21","1B","", "true"],
            ["Ezra Watson","1998-02-03","2B","", "true"],
            ["Adrian Torres","1997-10-14","SS","", "true"],
            ["Aaron Foster","1995-03-11","3B","", "true"],
            ["Leo Simmons","1998-08-28","OF","", "true"],
            ["Josiah Price","1996-01-31","OF","", "true"],
            ["Anthony Ross","1999-06-06","OF","", "true"],
            ["Dominic Bell","1997-09-09","SP","", "true"],
            ["Brayden Ward","1995-12-23","SP","", "true"],
            ["Ian Cooper","1998-03-03","RP","", "true"],
            ["Carson Gray","1996-07-17","RP","", "true"],
            # A couple owned / ineligible examples
            ["Prospect Owned","2002-01-01","SS","New York Yankees", "true"],
            ["Ineligible Guy","1990-01-01","OF","", "false"],
        ]
        with players_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(sample_players)

    if not order_path.exists():
        # 8 demo teams from MLB for readability
        teams = [
            "New York Yankees","Los Angeles Dodgers","Atlanta Braves","Chicago Cubs",
            "Houston Astros","Boston Red Sox","San Francisco Giants","St. Louis Cardinals",
        ]
        rows = [["Round","Pick","Team"]]
        rounds = 10
        for r in range(1, rounds + 1):
            # snake draft? Spec didn't require; use straight order for simplicity
            order = teams if r % 2 == 1 else teams  # keep straight order
            for i, t in enumerate(order, start=1):
                rows.append([r, i, t])
        with order_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)


# -------------------------
# Initial Setup on Startup
# -------------------------

init_db()
init_meta()

# Generate samples if missing
generate_sample_csvs(PLAYERS_CSV, DRAFT_ORDER_CSV)

# Import CSVs only when DB is empty
conn_chk = get_conn()
cur_chk = conn_chk.cursor()
cur_chk.execute("SELECT COUNT(*) FROM players")
players_count = cur_chk.fetchone()[0]
cur_chk.execute("SELECT COUNT(*) FROM draft_order")
order_count = cur_chk.fetchone()[0]
conn_chk.close()

if players_count == 0 and PLAYERS_CSV.exists():
    import_players_from_csv(PLAYERS_CSV)
if order_count == 0 and DRAFT_ORDER_CSV.exists():
    import_draft_order_from_csv(DRAFT_ORDER_CSV)


# -------------------------
# Routes / API
# -------------------------

INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>League Draft</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 24px; }
    .topbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 12px; }
    .pill { padding: 6px 10px; border-radius: 999px; background: #f2f2f2; display: inline-flex; gap: 8px; align-items: center; }
    .owned { opacity: 0.45; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid #e5e5e5; padding: 8px 10px; text-align: left; }
    th { background: #fafafa; position: sticky; top: 0; z-index: 1; }
    .btn { padding: 6px 10px; border: 1px solid #333; background: #fff; border-radius: 6px; cursor: pointer; }
    .btn[disabled] { opacity: 0.5; cursor: not-allowed; }
    .muted { color: #666; }
    .green { color: #0a7a0a; font-weight: 600; }
    .danger { color: #b00020; }
    .flex { display: flex; align-items: center; gap: 8px; }
    .right { margin-left: auto; }
    .badge { font-size: 12px; background: #eef7ff; color: #184a7d; border: 1px solid #cfe5ff; padding: 2px 6px; border-radius: 4px; }
    .kbd { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; border:1px solid #ccc; padding:2px 4px; border-radius:4px; background:#f9f9f9; }
    .row-hover:hover { background: #fcfcfc; }
  </style>
</head>
  <div class="pill" style="display:inline-block; margin-bottom:12px;">
    <a href="/order" style="text-decoration:none; color:#184a7d;">View Draft Order & Times →</a>
  </div>
  <p class="muted" style="margin-top:16px;">
    Tip: Use <span class="kbd">/</span> to focus search. Owned players are grayed out. Draft button appears only when your selected team is on the clock and the player is eligible.
  </p>
<body>
  <h1>League Draft</h1>
  <div id="status" class="pill">
    <span>Current Pick:</span>
    <span id="current-pick">Loading…</span>
    <span id="picks-progress" class="badge"></span>
  </div>

  <div class="topbar">
    <label class="pill">Your Team:
      <select id="team-select" style="margin-left:8px;"></select>
    </label>
    <label class="pill">
      <input type="checkbox" id="hide-owned" /> Hide owned players
    </label>
    <div class="pill right">
      <span>Search:</span>
      <input id="search" type="text" placeholder="Type a player name…" style="border:1px solid #ddd; padding:6px 8px; border-radius:6px; min-width: 260px;" />
      <span class="muted">(substring match)</span>
    </div>
  </div>
  <div class="pill" id="login-pill" style="margin-top:8px;">
    <span id="login-status">🔒 Not logged in</span>
  </div>


  <table>
    <thead>
      <tr>
        <th style="width:28%;">Player</th>
        <th style="width:12%;">DOB</th>
        <th style="width:10%;">Pos</th>
        <th style="width:25%;">Franchise</th>
        <th style="width:10%;">Eligible</th>
        <th style="width:15%;">Action</th>
      </tr>
    </thead>
    <tbody id="players-body"></tbody>
  </table>



<script>
const playersBody = document.getElementById('players-body');
const searchInput = document.getElementById('search');
const hideOwned = document.getElementById('hide-owned');
const teamSelect = document.getElementById('team-select');
const currentPickSpan = document.getElementById('current-pick');
const picksProgress = document.getElementById('picks-progress');

let state = {
  search: '',
  hideOwned: false,
  myTeam: null,
  currentPick: null,
  authedForSelected: false,  // ← add here
  authedEmail: ''            // ← and here
};


function focusSearchSlashShortcut(e){
  if (e.key === '/') { e.preventDefault(); searchInput.focus(); }
}
document.addEventListener('keydown', focusSearchSlashShortcut);

function setSelectOptions(teams) {
  teamSelect.innerHTML = '<option value="">— Select Team —</option>' + teams.map(t => `<option value="${t}">${t}</option>`).join('');
}

async function fetchDraftStatus() {
  try {
    const res = await fetch('/api/draft_status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (data.teams) setSelectOptions(data.teams);
    if (data.selected_team) teamSelect.value = data.selected_team;

    state.myTeam = data.selected_team || null;
    state.currentPick = data.current || null;
    state.authedForSelected = !!data.authed_for_selected;
    state.authedEmail = data.authed_email || "";

    const loginStatus = document.getElementById('login-status');
    if (state.authedForSelected && state.myTeam) {
      loginStatus.textContent = `🔓 Logged in as ${state.authedEmail} for ${state.myTeam}`;
    } else {
      loginStatus.textContent = `🔒 Not logged in`;
    }

    if (!data.current) {
      currentPickSpan.textContent = 'Draft complete';
      picksProgress.textContent = `${data.picks_made}/${data.total_picks}`;
    } else {
      currentPickSpan.textContent = `Round ${data.current.round}, Pick ${data.current.pick} — ${data.current.team}`;
      picksProgress.textContent = `${data.picks_made}/${data.total_picks}`;
    }
  } catch (err) {
    console.error('fetchDraftStatus failed:', err);
    // Keep the UI usable even if status fails
    state.currentPick = null;
    state.authedForSelected = false;
    state.authedEmail = "";
    const loginStatus = document.getElementById('login-status');
    loginStatus.textContent = `🔒 Not logged in`;
    currentPickSpan.textContent = 'Status unavailable';
    picksProgress.textContent = `—`;
  }
}


async function fetchPlayers() {
  const params = new URLSearchParams({
    search: state.search,
    hide_owned: state.hideOwned ? '1' : '0',
  });
  const res = await fetch('/api/players?' + params.toString());
  const data = await res.json();
  renderPlayers(data.players);
}

function renderPlayers(players) {
  playersBody.innerHTML = '';
  const canDraftNow = state.currentPick 
    && state.myTeam 
    && state.currentPick.team === state.myTeam
    && state.authedForSelected; // must be logged in for that team
  
  for (const p of players) {
    const tr = document.createElement('tr');
    tr.className = 'row-hover' + (p.franchise ? ' owned' : '');

    const eligible = !!p.eligible;
    const alreadyOwned = !!p.franchise;

    const actionCell = document.createElement('td');
    if (canDraftNow && !alreadyOwned && eligible) {
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.textContent = 'Draft';
      btn.onclick = async () => {
        btn.disabled = true;
        const resp = await fetch('/api/draft', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ player_id: p.id })
        });
        if (resp.ok) {
          await fetchDraftStatus();
          await fetchPlayers();
        } else {
          const msg = await resp.text();
          alert('Draft failed: ' + msg);
          btn.disabled = false;
        }
      };
      actionCell.appendChild(btn);
    } else if (alreadyOwned) {
      actionCell.innerHTML = '<span class="muted">Owned</span>';
    } else if (!eligible) {
      actionCell.innerHTML = '<span class="danger">Ineligible</span>';
    } else {
      actionCell.innerHTML = '<span class="muted">—</span>';
    }

    tr.innerHTML = `
      <td>${p.name}</td>
      <td>${p.dob || ''}</td>
      <td>${p.position || ''}</td>
      <td>${p.franchise || ''}</td>
      <td>${p.eligible ? '<span class="green">Yes</span>' : '<span class="danger">No</span>'}</td>
    `;
    tr.appendChild(actionCell);
    playersBody.appendChild(tr);
  }
}

// Debounce helper
function debounce(fn, ms) {
  let t; return function(...args){ clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
}

const debouncedFetch = debounce(() => { state.search = searchInput.value; fetchPlayers(); }, 120);
searchInput.addEventListener('input', debouncedFetch);

hideOwned.addEventListener('change', () => { state.hideOwned = hideOwned.checked; fetchPlayers(); });

teamSelect.addEventListener('change', async () => {
  const t = teamSelect.value || '';
  // Always set the selected team (this also clears old auth on backend)
  await fetch('/api/select_team', { 
    method: 'POST', 
    headers: {'Content-Type':'application/json'}, 
    body: JSON.stringify({ team: t }) 
  });

  // If a team is chosen, prompt for email and try to log in
  if (t) {
    const email = window.prompt(`Enter the manager email for ${t} to unlock drafting:`);
    if (email && email.trim()) {
      const resp = await fetch('/api/login_team', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ team: t, email: email.trim() })
      });
      if (!resp.ok) {
        const msg = await resp.text();
        alert('Login failed: ' + msg);
      }
    }
  }
  await fetchDraftStatus();
  await fetchPlayers();
});


async function boot() {
  await fetchDraftStatus().catch(() => {});
  await fetchPlayers().catch((e) => console.error('fetchPlayers failed:', e));
}


boot();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.get("/api/players")
def api_players():
    search = (request.args.get("search") or "").strip().lower()
    hide_owned = request.args.get("hide_owned") == "1"

    conn = get_conn()
    cur = conn.cursor()
    q = "SELECT id, name, dob, position, franchise, eligible FROM players"
    params: List[Any] = []

    clauses = []
    if search:
        clauses.append("LOWER(name) LIKE ?")
        params.append(f"%{search}%")
    if hide_owned:
        clauses.append("(franchise IS NULL OR franchise = '')")

    if clauses:
        q += " WHERE " + " AND ".join(clauses)

    q += " ORDER BY name COLLATE NOCASE ASC"

    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()

    players = []
    for r in rows:
        players.append({
            "id": r[0],
            "name": r[1],
            "dob": r[2],
            "position": r[3],
            "franchise": r[4],
            "eligible": int(r[5]),
        })
    return jsonify({"players": players})


@app.get("/api/draft_status")
def api_draft_status():
    try:
        cur_pick = get_current_pick()

        # totals
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM draft_order WHERE player_id IS NOT NULL")
        made = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(*) FROM draft_order")
        total = int(cur.fetchone()[0] or 0)
        conn.close()

        selected_team = session.get("selected_team", "") or ""
        authed_team = session.get("authed_team", "") or ""
        authed_email = session.get("authed_email", "") or ""

        # Ensure cur_pick is either None or a plain dict (already is),
        # but *never* something non-serializable.
        payload = {
            "current": cur_pick if cur_pick else None,
            "selected_team": selected_team,
            "teams": MLB_TEAMS,
            "picks_made": made,
            "total_picks": total,
            "authed_team": authed_team,
            "authed_email": authed_email,
            "authed_for_selected": bool(selected_team) and (authed_team == selected_team),
        }
        return jsonify(payload)
    except Exception as e:
        # Never break the UI: return a safe default payload the frontend can handle.
        app.logger.exception("api/draft_status failed: %s", e)
        return jsonify({
            "current": None,
            "selected_team": session.get("selected_team", "") or "",
            "teams": MLB_TEAMS,
            "picks_made": 0,
            "total_picks": 0,
            "authed_team": "",
            "authed_email": "",
            "authed_for_selected": False,
        }), 200


@app.post("/api/login_team")
def api_login_team():
    data = request.get_json(force=True, silent=True) or {}
    team = (data.get("team") or "").strip()
    email = (data.get("email") or "").strip()

    if not team or team not in MLB_TEAMS:
        return ("Unknown or missing team", 400)
    expected = TEAM_EMAILS.get(team)
    if not expected:
        return ("No email configured for team", 400)

    if emails_equal(email, expected):
        session["authed_team"] = team
        session["authed_email"] = email
        # also set selected team if not set
        if session.get("selected_team") != team:
            session["selected_team"] = team
        return jsonify({"ok": True}), 200
    else:
        return ("Invalid email for this team", 401)


@app.post("/api/select_team")
def api_select_team():
    data = request.get_json(force=True, silent=True) or {}
    team = (data.get("team") or "").strip()
    if team and team not in MLB_TEAMS:
        return ("Unknown team", 400)
    # selecting a new team clears any previous auth
    session["selected_team"] = team
    if session.get("authed_team") != team:
        session.pop("authed_team", None)
        session.pop("authed_email", None)
    return ("", 204)

@app.post("/api/draft")
def api_draft():
    data = request.get_json(force=True, silent=True) or {}
    player_id = data.get("player_id")
    if not isinstance(player_id, int):
        return ("Missing or invalid player_id", 400)

    # Load current pick
    current = get_current_pick()
    if current is None:
        return ("Draft is complete", 400)

    my_team = session.get("selected_team")
    if session.get("authed_team") != my_team:
        return ("Not logged in for this team", 401)
    if not my_team:
        return ("Select your team first", 400)
    if my_team != current["team"]:
        return ("Not your pick", 403)

    conn = get_conn()
    cur = conn.cursor()

    # Validate player availability & eligibility
    cur.execute("SELECT id, name, franchise, eligible FROM players WHERE id=?", (player_id,))
    prow = cur.fetchone()
    if not prow:
        conn.close()
        return ("Player not found", 404)
    if prow[2]:
        conn.close()
        return ("Player already owned", 409)
    if int(prow[3]) != 1:
        conn.close()
        return ("Player is not draft-eligible", 409)

    # Assign player to franchise and mark draft pick
    try:
        cur.execute("UPDATE players SET franchise=? WHERE id=?", (my_team, player_id))
        cur.execute(
            "UPDATE draft_order SET player_id=?, drafted_at=? WHERE id=?",
            (player_id, datetime.utcnow().isoformat(timespec='seconds'), current["id"])
        )
        conn.commit()
        try:
            notify_if_new_on_clock()
        except Exception as e:
            # Keep drafting robust even if email fails
            print(f"[notify] failed: {e}")

    except Exception as e:
        conn.rollback()
        conn.close()
        return (f"Failed to draft: {e}", 500)

    conn.close()
    return ("", 204)

@app.post("/tasks/scan_on_clock")
def scan_on_clock():
    try:
        notify_if_new_on_clock()
        return ("", 204)
    except Exception as e:
        return (f"scan failed: {e}", 500)

@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    # Helpful banner explaining sample CSVs
    print("\n*** Flask Draft App ***")
    print("If players.csv and draft_order.csv are not present, sample files were generated.")
    print("To reset: delete draft.db and restart.\n")
    app.run(debug=True)

