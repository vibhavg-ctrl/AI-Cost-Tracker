"""
AI Efficiency Intelligence Platform — Backend v3
GSL CEO Office | Local only, no data leaves device

v3 adds:
  - Real API connectors: Cursor Admin, Claude Admin, GitHub, Jira
  - Async HTTP calls via httpx
  - Per-user live data overlay on seeded GSL data
  - /api/sync to trigger full data refresh (background task)
  - /api/sync/status to poll sync progress
"""

import json, os, csv, io, asyncio, time, re, calendar, sqlite3
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

app = FastAPI(title="AI Efficiency Intelligence Platform", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ─────────────────────────────────────────────────────────────────────
CONFIG = {
    "claude":  {"api_key":"sk-ant-DUMMY","org_id":"org-DUMMY","base_url":"https://api.anthropic.com","enabled":False},
    "cursor":  {"api_key":"cursor-DUMMY","team_id":"team-DUMMY","base_url":"https://api.cursor.com","enabled":False},
    "github":  {"token":"ghp_DUMMY","org":"ginesys","base_url":"https://api.github.com","enabled":False},
    "jira":    {"base_url":"https://ginesys.atlassian.net","email":"vibhav@ginesys.com","api_token":"JIRA-DUMMY","enabled":False},
}

SEAT_COSTS = {"Cursor":3756.4,"Claude":1878.2,"ChatGPT":2098.89}
CURSOR_LINES_PER_OUTPUT_TOKEN = 0.75   # conservative estimate: 1 output token ≈ 0.75 lines

# ── SYNC STATE ─────────────────────────────────────────────────────────────────
SYNC_STATE: Dict[str,Any] = {
    "running":False,"last_run":None,"progress":0,"steps":[],"errors":[],
    "live_cursor_data":{},"live_claude_data":{},"live_github_data":{},"live_jira_data":{},
}

# ── DATABASE ───────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "ai_efficiency.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe concurrent reads
    return conn

def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cursor_usage (
                user_id          TEXT NOT NULL,
                email            TEXT NOT NULL,
                month            TEXT NOT NULL,
                sessions         INTEGER DEFAULT 0,
                input_tokens     INTEGER DEFAULT 0,
                cache_read       INTEGER DEFAULT 0,
                output_tokens    INTEGER DEFAULT 0,
                total_tokens     INTEGER DEFAULT 0,
                cost_usd         REAL    DEFAULT 0,
                cost_inr         REAL    DEFAULT 0,
                free_sessions    INTEGER DEFAULT 0,
                included_sessions INTEGER DEFAULT 0,
                ondemand_sessions INTEGER DEFAULT 0,
                avg_tokens_session INTEGER DEFAULT 0,
                daily_avg_tokens INTEGER DEFAULT 0,
                top_model        TEXT    DEFAULT 'auto',
                usage_level      TEXT    DEFAULT 'Normal',
                pct_team         REAL    DEFAULT 0,
                synced_at        TEXT    NOT NULL,
                PRIMARY KEY (user_id, month)
            );
            CREATE TABLE IF NOT EXISTS code_metrics (
                user_id                TEXT NOT NULL,
                month                  TEXT NOT NULL,
                ai_lines_written       INTEGER DEFAULT 0,
                github_committed       INTEGER DEFAULT 0,
                rectification_lines    INTEGER DEFAULT 0,
                carryover_in           INTEGER DEFAULT 0,
                carryover_out          INTEGER DEFAULT 0,
                effective_new_commits  INTEGER DEFAULT 0,
                effective_ai_available INTEGER DEFAULT 0,
                ai_attributed_lines    INTEGER DEFAULT 0,
                ai_utilization_rate    REAL    DEFAULT 0,
                backfill_rate          REAL    DEFAULT 0,
                data_source            TEXT    DEFAULT 'live',
                heuristics_applied     TEXT    DEFAULT '[]',
                synced_at              TEXT    NOT NULL,
                PRIMARY KEY (user_id, month)
            );
            CREATE TABLE IF NOT EXISTS jira_metrics (
                user_id        TEXT NOT NULL,
                month          TEXT NOT NULL,
                tickets_done   INTEGER DEFAULT 0,
                tickets_total  INTEGER DEFAULT 0,
                story_points   INTEGER DEFAULT 0,
                synced_at      TEXT NOT NULL,
                PRIMARY KEY (user_id, month)
            );
            CREATE TABLE IF NOT EXISTS sync_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                month           TEXT NOT NULL,
                synced_at       TEXT NOT NULL,
                cursor_users    INTEGER DEFAULT 0,
                github_users    INTEGER DEFAULT 0,
                code_snapshots  INTEGER DEFAULT 0,
                errors          TEXT DEFAULT '[]'
            );
        """)

# ── DB WRITE FUNCTIONS ─────────────────────────────────────────────────────────

def db_save_cursor_month(month: str, cursor_data: dict) -> int:
    """Upsert Cursor API data for every user in cursor_data into the DB."""
    email_to_uid = {u["cursor_email"]: u["id"] for u in USERS if u.get("cursor_email")}
    rows = []
    now = datetime.now().isoformat()
    for email, d in cursor_data.items():
        uid = email_to_uid.get(email)
        if not uid:
            continue
        rows.append((uid, email, month,
                     d.get("sessions",0), d.get("input_tokens",0), d.get("cache_read",0),
                     d.get("output_tokens",0), d.get("total_tokens",0),
                     d.get("cost_usd",0), d.get("cost_inr",0),
                     d.get("free_sessions",0), d.get("included_sessions",0),
                     d.get("ondemand_sessions",0), d.get("avg_tokens_session",0),
                     d.get("daily_avg_tokens",0), d.get("top_model","auto"),
                     d.get("usage_level","Normal"), d.get("pct_team",0), now))
    if not rows:
        return 0
    with get_db() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO cursor_usage
            (user_id,email,month,sessions,input_tokens,cache_read,output_tokens,total_tokens,
             cost_usd,cost_inr,free_sessions,included_sessions,ondemand_sessions,
             avg_tokens_session,daily_avg_tokens,top_model,usage_level,pct_team,synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
    return len(rows)

def db_save_code_metrics_month(month: str) -> int:
    """Persist live/computed code metrics for a month from MONTHLY_CODE_METRICS to DB."""
    now = datetime.now().isoformat()
    rows = []
    for (uid, m), snap in MONTHLY_CODE_METRICS.items():
        if m != month or snap.data_source == "demo":
            continue
        rows.append((uid, m, snap.ai_lines_written, snap.github_committed,
                     snap.rectification_lines, snap.carryover_in, snap.carryover_out,
                     snap.effective_new_commits, snap.effective_ai_available,
                     snap.ai_attributed_lines, snap.ai_utilization_rate, snap.backfill_rate,
                     snap.data_source, json.dumps(snap.heuristics_applied), now))
    if not rows:
        return 0
    with get_db() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO code_metrics
            (user_id,month,ai_lines_written,github_committed,rectification_lines,
             carryover_in,carryover_out,effective_new_commits,effective_ai_available,
             ai_attributed_lines,ai_utilization_rate,backfill_rate,data_source,heuristics_applied,synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
    return len(rows)

def db_save_jira_month(month: str, jira_data: dict) -> int:
    """Upsert Jira story-point data for a month."""
    email_to_uid = {u["cursor_email"]: u["id"] for u in USERS if u.get("cursor_email")}
    rows = []
    now = datetime.now().isoformat()
    for email, d in jira_data.items():
        uid = email_to_uid.get(email)
        if not uid:
            continue
        rows.append((uid, month, d.get("tickets_done",0), d.get("tickets_total",0),
                     d.get("story_points",0), now))
    if not rows:
        return 0
    with get_db() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO jira_metrics
            (user_id,month,tickets_done,tickets_total,story_points,synced_at)
            VALUES (?,?,?,?,?,?)
        """, rows)
    return len(rows)

def db_log_sync(month: str, cursor_users: int, github_users: int,
                code_snaps: int, errors: list) -> None:
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sync_log (month,synced_at,cursor_users,github_users,code_snapshots,errors)
            VALUES (?,?,?,?,?,?)
        """, (month, datetime.now().isoformat(), cursor_users, github_users,
              code_snaps, json.dumps(errors)))

# ── DB READ FUNCTIONS ──────────────────────────────────────────────────────────

def db_load_cursor_month(month: str) -> dict:
    """Load cursor data for a month from DB → dict keyed by email (source=db)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM cursor_usage WHERE month=?", (month,)
        ).fetchall()
    result = {}
    for r in rows:
        result[r["email"]] = {
            "sessions":r["sessions"], "input_tokens":r["input_tokens"],
            "cache_read":r["cache_read"], "output_tokens":r["output_tokens"],
            "total_tokens":r["total_tokens"], "cost_usd":r["cost_usd"],
            "cost_inr":r["cost_inr"], "free_sessions":r["free_sessions"],
            "included_sessions":r["included_sessions"],
            "ondemand_sessions":r["ondemand_sessions"],
            "avg_tokens_session":r["avg_tokens_session"],
            "daily_avg_tokens":r["daily_avg_tokens"], "top_model":r["top_model"],
            "usage_level":r["usage_level"], "pct_team":r["pct_team"],
            "source": "db",
        }
    return result

def db_load_code_metrics_month(month: str) -> int:
    """Load code metrics for a month from DB into MONTHLY_CODE_METRICS in memory."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM code_metrics WHERE month=?", (month,)
        ).fetchall()
    for r in rows:
        snap = CodeMetricsSnapshot(
            user_id=r["user_id"], month=r["month"],
            ai_lines_written=r["ai_lines_written"],
            github_committed=r["github_committed"],
            rectification_lines=r["rectification_lines"],
            carryover_in=r["carryover_in"], carryover_out=r["carryover_out"],
            effective_new_commits=r["effective_new_commits"],
            effective_ai_available=r["effective_ai_available"],
            ai_attributed_lines=r["ai_attributed_lines"],
            ai_utilization_rate=r["ai_utilization_rate"],
            backfill_rate=r["backfill_rate"], data_source=r["data_source"],
            heuristics_applied=json.loads(r["heuristics_applied"] or "[]"),
        )
        MONTHLY_CODE_METRICS[(r["user_id"], r["month"])] = snap
    return len(rows)

def db_get_available_months() -> List[dict]:
    """Return all months that have persisted data in the DB with metadata."""
    with get_db() as conn:
        cu = conn.execute("""
            SELECT month, COUNT(DISTINCT user_id) AS users,
                   SUM(total_tokens) AS total_tokens, SUM(cost_inr) AS total_cost_inr,
                   MAX(synced_at) AS last_synced
            FROM cursor_usage GROUP BY month ORDER BY month DESC
        """).fetchall()
        cm = conn.execute("""
            SELECT month, COUNT(DISTINCT user_id) AS users,
                   AVG(ai_utilization_rate) AS avg_utilization
            FROM code_metrics GROUP BY month ORDER BY month DESC
        """).fetchall()
        sl = conn.execute("""
            SELECT month, MAX(synced_at) AS last_synced, SUM(cursor_users) AS total_syncs
            FROM sync_log GROUP BY month ORDER BY month DESC
        """).fetchall()
    months: Dict[str, dict] = {}
    for r in cu:
        months[r["month"]] = {
            "month": r["month"], "cursor_users": r["users"],
            "total_tokens": r["total_tokens"] or 0,
            "total_cost_inr": round(r["total_cost_inr"] or 0, 2),
            "last_synced": r["last_synced"], "has_cursor": True,
            "has_code_metrics": False, "avg_ai_utilization": None,
        }
    for r in cm:
        if r["month"] not in months:
            months[r["month"]] = {
                "month": r["month"], "cursor_users": 0,
                "total_tokens": 0, "total_cost_inr": 0,
                "last_synced": None, "has_cursor": False, "has_code_metrics": False,
            }
        months[r["month"]]["has_code_metrics"] = True
        months[r["month"]]["avg_ai_utilization"] = round((r["avg_utilization"] or 0) * 100, 1)
    for r in sl:
        if r["month"] in months:
            months[r["month"]]["sync_count"] = r["total_syncs"]
    return sorted(months.values(), key=lambda x: x["month"], reverse=True)

def db_get_stats() -> dict:
    """Return high-level DB stats for health endpoint."""
    try:
        with get_db() as conn:
            total_months  = conn.execute("SELECT COUNT(DISTINCT month) FROM cursor_usage").fetchone()[0]
            total_records = conn.execute("SELECT COUNT(*) FROM cursor_usage").fetchone()[0]
            last_sync     = conn.execute("SELECT MAX(synced_at) FROM sync_log").fetchone()[0]
        return {"db_months": total_months, "db_records": total_records, "db_last_sync": last_sync, "db_path": DB_PATH}
    except Exception:
        return {"db_months": 0, "db_records": 0, "db_last_sync": None, "db_path": DB_PATH}

# Initialise DB at startup
init_db()

# ── CODE METRICS DATACLASS ─────────────────────────────────────────────────────
@dataclass
class CodeMetricsSnapshot:
    user_id: str
    month: str
    ai_lines_written: int = 0
    github_committed: int = 0
    rectification_lines: int = 0
    rectification_commits: List[str] = field(default_factory=list)
    carryover_in: int = 0
    carryover_out: int = 0
    effective_new_commits: int = 0
    effective_ai_available: int = 0
    ai_attributed_lines: int = 0
    ai_utilization_rate: float = 0.0
    backfill_rate: float = 0.0
    data_source: str = "demo"
    heuristics_applied: List[str] = field(default_factory=list)

# ── TEMPORAL CODE METRICS STORE ────────────────────────────────────────────────
MONTHLY_CODE_METRICS: Dict[tuple, CodeMetricsSnapshot] = {}

# ── DEMO CODE METRICS (matches exact user scenario) ───────────────────────────
DEMO_CODE_METRICS_RAW = [
    # Jagreeta De (EP0477) — the primary example: Feb 7k AI / 5k committed → 2k carryover
    {"user_id":"EP0477","month":"2026-02","ai_lines_written":7000,"github_committed":5000,
     "rectification_lines":0,"data_source":"demo","heuristics_applied":[]},
    # March: 8k committed but 1500 are fixing Feb code; 2k carryover arrives from Feb
    {"user_id":"EP0477","month":"2026-03","ai_lines_written":5500,"github_committed":8000,
     "rectification_lines":1500,"data_source":"demo","heuristics_applied":["keyword_match","pr_reference_back"]},
    # Abhishek Gupta (EP0812)
    {"user_id":"EP0812","month":"2026-02","ai_lines_written":4200,"github_committed":4800,
     "rectification_lines":0,"data_source":"demo","heuristics_applied":[]},
    {"user_id":"EP0812","month":"2026-03","ai_lines_written":3800,"github_committed":5200,
     "rectification_lines":600,"data_source":"demo","heuristics_applied":["keyword_match"]},
    # Sanjeev Kumar (GC015)
    {"user_id":"GC015","month":"2026-02","ai_lines_written":6100,"github_committed":4500,
     "rectification_lines":0,"data_source":"demo","heuristics_applied":[]},
    {"user_id":"GC015","month":"2026-03","ai_lines_written":4800,"github_committed":7200,
     "rectification_lines":900,"data_source":"demo","heuristics_applied":["keyword_match","pr_reference_back"]},
    # Soumik Bose (EP0340)
    {"user_id":"EP0340","month":"2026-02","ai_lines_written":3500,"github_committed":3800,
     "rectification_lines":0,"data_source":"demo","heuristics_applied":[]},
    {"user_id":"EP0340","month":"2026-03","ai_lines_written":5100,"github_committed":4200,
     "rectification_lines":400,"data_source":"demo","heuristics_applied":["keyword_match"]},
    # Gyanesh Kumar (EP00143)
    {"user_id":"EP00143","month":"2026-02","ai_lines_written":8200,"github_committed":6500,
     "rectification_lines":0,"data_source":"demo","heuristics_applied":[]},
    {"user_id":"EP00143","month":"2026-03","ai_lines_written":6000,"github_committed":9100,
     "rectification_lines":1200,"data_source":"demo","heuristics_applied":["keyword_match","pr_reference_back"]},
]

def _compute_derived_fields(snap: CodeMetricsSnapshot, carryover_in: int = 0) -> None:
    snap.carryover_in = carryover_in
    snap.effective_new_commits = max(0, snap.github_committed - snap.rectification_lines)
    snap.effective_ai_available = snap.ai_lines_written + carryover_in
    snap.ai_attributed_lines = min(snap.effective_new_commits, snap.effective_ai_available)
    snap.ai_utilization_rate = (
        round(snap.ai_attributed_lines / snap.effective_ai_available, 4)
        if snap.effective_ai_available > 0 else 0.0
    )
    snap.backfill_rate = (
        round(snap.rectification_lines / snap.github_committed, 4)
        if snap.github_committed > 0 else 0.0
    )
    snap.carryover_out = max(0, snap.effective_ai_available - snap.effective_new_commits)

def initialize_demo_metrics() -> None:
    # Sort records so carryover chains correctly (chronological per user)
    from collections import defaultdict
    by_user: Dict[str, list] = defaultdict(list)
    for raw in DEMO_CODE_METRICS_RAW:
        by_user[raw["user_id"]].append(raw)
    for uid, records in by_user.items():
        running_carryover = 0
        for raw in sorted(records, key=lambda r: r["month"]):
            snap = CodeMetricsSnapshot(
                user_id=raw["user_id"], month=raw["month"],
                ai_lines_written=raw["ai_lines_written"],
                github_committed=raw["github_committed"],
                rectification_lines=raw["rectification_lines"],
                data_source=raw["data_source"],
                heuristics_applied=list(raw["heuristics_applied"]),
            )
            _compute_derived_fields(snap, carryover_in=running_carryover)
            running_carryover = snap.carryover_out
            MONTHLY_CODE_METRICS[(uid, raw["month"])] = snap

initialize_demo_metrics()

# ── SEED DATA ──────────────────────────────────────────────────────────────────
USERS = [
    {"id":"EP0381","name":"Sayak Das","title":"Solution Architect","location":"Kolkata","team":"ERP","tools":["Claude","Cursor"],"cost_per_mo":5634.6,"cursor_email":None},
    {"id":"EP0053","name":"Rajiv kumar Gupta","title":"QA Manager","location":"Kolkata","team":"ERP","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0064","name":"Subhasish Ghosh","title":"Solution Architect","location":"Kolkata","team":"ERP","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0590","name":"Rahul Kumar Singh","title":"QAE-2","location":"Kolkata","team":"ERP","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0233","name":"Rohan Goon","title":"QAE-2","location":"Kolkata","team":"ERP","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0632","name":"Arani Barman","title":"QAE-2","location":"Kolkata","team":"ERP","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0032","name":"Animesh Das","title":"Product Specialist","location":"Kolkata","team":"ERP","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP00180","name":"Atanu Ghosh Barman","title":"AVP Delivery","location":"Kolkata","team":"ERP","tools":["Claude","Cursor"],"cost_per_mo":5634.6,"cursor_email":None},
    {"id":"EP0477","name":"Jagreeta De","title":"SDE-2","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"jagreeta.d@gsl.in"},
    {"id":"EP0812","name":"Abhishek Gupta","title":"SDE","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"abhishek.gupta@gsl.in"},
    {"id":"GC015","name":"Sanjeev Kumar","title":"SDE","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"sanjeev.kumar@gsl.in"},
    {"id":"EP0340","name":"Soumik Bose","title":"SDE-2","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"soumik.b@gsl.in"},
    {"id":"EP00143","name":"Gyanesh Kumar","title":"SDE-2","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"gyanesh.k@gsl.in"},
    {"id":"EP0823","name":"Sayandip Saha","title":"SDE-1","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"sayandip.s@gsl.in"},
    {"id":"EP0852","name":"Aritra Lahiri","title":"SDE-1","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"aritra.l@gsl.in"},
    {"id":"EP0232","name":"Jit Chatterjee","title":"SDE-2","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"jit.chatterjee@gsl.in"},
    {"id":"EP0828","name":"Gairika Samaddar","title":"SDE-1","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"gairika.s@gsl.in"},
    {"id":"EP0872","name":"Soumyojyoti Barman","title":"SDE-1","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"soumyojyoti.b@gsl.in"},
    {"id":"EP0469","name":"Srishtik Sinha","title":"SDE-2","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"srishtik.s@gsl.in"},
    {"id":"EP0856","name":"Rakesh Kumar Singh","title":"Solution Architect","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"rakesh.s@gsl.in"},
    {"id":"EP0886","name":"Subhradeep Pal","title":"SDE-1","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"subhradeep.p@gsl.in"},
    {"id":"EP0826","name":"Pronati Karmakar","title":"SDE-1","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"pronati.k@gsl.in"},
    {"id":"EP00181","name":"Sujoy Dutta","title":"Tech Lead - Support","location":"Kolkata","team":"ERP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"sujoy.dutta@gsl.in"},
    {"id":"EP0039","name":"Bappa Saha","title":"ATL-QA","location":"Kolkata","team":"Data & AI","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0620","name":"Priyanka Hazra","title":"QAE-1","location":"Kolkata","team":"Data & AI","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0570","name":"Simi Gupta","title":"AQAE","location":"Kolkata","team":"Data & AI","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP00163","name":"Nabendu Roy Chowdhury","title":"Associate PM","location":"Kolkata","team":"Data & AI","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0033","name":"Anindya Das","title":"AVP - Data Platform","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0220","name":"Siddhartha Sen","title":"DB Engineer-2","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0676","name":"Sk Sarafad","title":"DB Engineer-1","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP00150","name":"Pradipta Bhowmik","title":"DB Engineer-2","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0050","name":"Prasanta Pramanik","title":"DB Engineer-2","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0048","name":"Manash Kundu","title":"DB Engineer-2","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0354","name":"Tanmoy Adak","title":"DB Engineer-1","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0044","name":"Diptiman Thakur","title":"Database Lead","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0794","name":"Subrata Dolui","title":"Trainee","location":"Kolkata","team":"Data Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0719","name":"Satish Kr Yadav","title":"SDE-2","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"satish.y@gsl.in"},
    {"id":"BT-EMP-490","name":"Nikhil Umraskar","title":"Technical Architect","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"nikhil.umraskar@gsl.in"},
    {"id":"EP0854","name":"Ganesh Nehe","title":"SDE-1","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"ganesh.n@gsl.in"},
    {"id":"BT-EMP-520","name":"Anirudha Avinash Lad","title":"SDE-2","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"anirudha.l@gsl.in"},
    {"id":"EP0781","name":"Abhishek Jaswal","title":"SDE-1","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"abhishek.j@gsl.in"},
    {"id":"EP0800","name":"Kanhaiya Gupta","title":"SDE-1","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"kanhaiya.g@gsl.in"},
    {"id":"EP0789","name":"Suhel Khan","title":"SDE-1","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"suhel.k@gsl.in"},
    {"id":"EP0717","name":"Parveen Kumar","title":"Technical Architect","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"parveen.k@gsl.in"},
    {"id":"EP0815","name":"Aaditi Singh","title":"QA Analyst","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"aaditi.s@gsl.in"},
    {"id":"BT-EMP-596","name":"Kajal Bhosle","title":"QA Analyst","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"kajal.b@gsl.in"},
    {"id":"BT-EMP-551","name":"Gaurav Varad Mhapne","title":"SDE-2","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":None},
    {"id":"BT-EMP-633","name":"Utkarsh Pritidas Bandodkar","title":"SDE-1","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"utkarsh.b@gsl.in"},
    {"id":"BT-EMP-648","name":"Mahabaleshwar Raghuvir Gawas","title":"Sr Manager Tech","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":None},
    {"id":"EP0805","name":"Anurag Rai","title":"QA Analyst","location":"Gurgaon","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"anurag.r@gsl.in"},
    {"id":"BT-EMP-638","name":"Rohit P Biju","title":"QA Analyst","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"rohit.b@gsl.in"},
    {"id":"EP0813","name":"Ashutosh Avdhut Parab Gaonkar","title":"QA Analyst","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"ashutosh.g@gsl.in"},
    {"id":"BT-EMP-639","name":"Shivani Pednekar","title":"QA Analyst","location":"Goa","team":"OMS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":None},
    {"id":"ZEMP109","name":"Shubham Rajendra Maurya","title":"Lead Dev Manager","location":"Mumbai","team":"Cloud POS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"shubham.m@gsl.in"},
    {"id":"ZEMP154","name":"Sarthak Sharma","title":"SDE-1","location":"Gurgaon","team":"Cloud POS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"sarthak.s@gsl.in"},
    {"id":"ZEMP174","name":"Pulkit Sharma","title":"SDE-2","location":"Gurgaon","team":"Cloud POS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"pulkit.s@gsl.in"},
    {"id":"ZEMP124","name":"Amit Mishra","title":"QA Manager","location":"Gurgaon","team":"Cloud POS","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":None},
    {"id":"BT-EMP-635","name":"Vaishnavi Sandeep Parab","title":"SDE-2","location":"Goa","team":"Common Services","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"vaishnavi.p@gsl.in"},
    {"id":"EP0836","name":"Anish Manoj Rane","title":"Trainee Developer","location":"Goa","team":"Common Services","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"anish.r@gsl.in"},
    {"id":"EP0818","name":"Mukesh Joriwal","title":"SDE-3","location":"Gurgaon","team":"Common Services","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"mukesh.j@gsl.in"},
    {"id":"BT-EMP-623","name":"Daisy Jesica Fernandes","title":"Tech Support Engineer","location":"Goa","team":"Helpdesk","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"daisy.f@gsl.in"},
    {"id":"EP0745","name":"Swapnil Saulo Salgaonkar","title":"Assoc Tech Support","location":"Goa","team":"Helpdesk","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"swapnil.s@gsl.in"},
    {"id":"BT-EMP-612","name":"Mohit Mohan Patil","title":"Product Support Exec","location":"Goa","team":"Helpdesk","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"mohit.patil@gsl.in"},
    {"id":"BT-EMP-526","name":"Viraj Madhusudan Parab","title":"Product Support Lead","location":"Goa","team":"Helpdesk","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"viraj.p@gsl.in"},
    {"id":"EP0670","name":"Arun Kumar","title":"DB Engineer-2","location":"Kolkata","team":"Helpdesk","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0035","name":"Arindam Banerjee","title":"Cloud Platform Architect","location":"Kolkata","team":"Infra & Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"EP0553","name":"Abdur Rashid Mondal","title":"Cloud Engineer-2","location":"Kolkata","team":"Infra & Platform","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"ZEMP103","name":"Naman Bisht","title":"Head UI UX","location":"Gurgaon","team":"Product","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"GC020","name":"Proloy Karmakar","title":"Developer","location":"Kolkata","team":"Product","tools":["Claude"],"cost_per_mo":1878.2,"cursor_email":None},
    {"id":"1","name":"Rajarshi Basu Roy","title":"Chief Architect","location":"Gurgaon","team":"Chief Architect","tools":["ChatGPT"],"cost_per_mo":2098.89,"cursor_email":None},
    {"id":"2","name":"GinesysOne","title":"System Account","location":"Gurgaon","team":"Accounts Team","tools":["ChatGPT"],"cost_per_mo":2098.89,"cursor_email":None},
    {"id":"3","name":"GSL Accounts","title":"Shared Account","location":"Gurgaon","team":"Accounts Team","tools":["ChatGPT"],"cost_per_mo":2098.89,"cursor_email":None},
    {"id":"ZEMP105","name":"Kalmeshwar Jayawant Gurav","title":"Developer","location":"Goa","team":"GIP","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"kalmesh.g@gsl.in"},
    {"id":"EP0795","name":"Tarun Kumar Chourasiya","title":"SDE-2","location":"Gurgaon","team":"Exit","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"tarun.c@gsl.in"},
    {"id":"EP0715","name":"Priyangshu Sarkar","title":"ASDE","location":"Kolkata","team":"Exit","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"priyangshu.s@gsl.in"},
    {"id":"EP0851","name":"Debjit Ghosh","title":"SDE-1","location":"Kolkata","team":"Exit","tools":["Cursor"],"cost_per_mo":3756.4,"cursor_email":"debjit.g@gsl.in"},
]

CURSOR_TOKEN_DATA = {
    "jagreeta.d@gsl.in":{"sessions":315,"input_tokens":15764183,"cache_read":352299183,"output_tokens":2758119,"total_tokens":386032240,"cost_usd":231.21,"cost_inr":20346.48,"free_sessions":71,"included_sessions":125,"ondemand_sessions":115,"avg_tokens_session":1225499,"daily_avg_tokens":12452652,"top_model":"claude-4.6-sonnet-medium-thinking","usage_level":"HIGH","pct_team":0.0865},
    "ganesh.n@gsl.in":{"sessions":461,"input_tokens":20275266,"cache_read":268987804,"output_tokens":1781334,"total_tokens":291044404,"cost_usd":85.34,"cost_inr":7509.92,"free_sessions":69,"included_sessions":379,"ondemand_sessions":0,"avg_tokens_session":631332,"daily_avg_tokens":9388529,"top_model":"auto","usage_level":"HIGH","pct_team":0.0652},
    "gyanesh.k@gsl.in":{"sessions":305,"input_tokens":39291141,"cache_read":240084244,"output_tokens":1624571,"total_tokens":284832632,"cost_usd":194.52,"cost_inr":17117.76,"free_sessions":26,"included_sessions":132,"ondemand_sessions":144,"avg_tokens_session":933877,"daily_avg_tokens":9188149,"top_model":"composer-1.5","usage_level":"HIGH","pct_team":0.0638},
    "sayandip.s@gsl.in":{"sessions":240,"input_tokens":20711502,"cache_read":257173310,"output_tokens":1984716,"total_tokens":282451474,"cost_usd":82.24,"cost_inr":7237.12,"free_sessions":75,"included_sessions":165,"ondemand_sessions":0,"avg_tokens_session":1176881,"daily_avg_tokens":9111337,"top_model":"auto","usage_level":"HIGH","pct_team":0.0633},
    "tarun.c@gsl.in":{"sessions":395,"input_tokens":25797168,"cache_read":247063661,"output_tokens":1285374,"total_tokens":274590247,"cost_usd":80.07,"cost_inr":7046.16,"free_sessions":83,"included_sessions":308,"ondemand_sessions":0,"avg_tokens_session":695165,"daily_avg_tokens":8857749,"top_model":"auto","usage_level":"HIGH","pct_team":0.0615},
    "pulkit.s@gsl.in":{"sessions":359,"input_tokens":104708766,"cache_read":123729600,"output_tokens":1093992,"total_tokens":229532358,"cost_usd":80.13,"cost_inr":7051.44,"free_sessions":91,"included_sessions":261,"ondemand_sessions":0,"avg_tokens_session":639365,"daily_avg_tokens":7404269,"top_model":"auto","usage_level":"HIGH","pct_team":0.0514},
    "satish.y@gsl.in":{"sessions":526,"input_tokens":44927724,"cache_read":152439737,"output_tokens":1273730,"total_tokens":198641191,"cost_usd":80.113,"cost_inr":7049.94,"free_sessions":205,"included_sessions":318,"ondemand_sessions":0,"avg_tokens_session":377644,"daily_avg_tokens":6407780,"top_model":"auto","usage_level":"HIGH","pct_team":0.0445},
    "nikhil.umraskar@gsl.in":{"sessions":360,"input_tokens":39186447,"cache_read":142762878,"output_tokens":1472467,"total_tokens":183874341,"cost_usd":80.75,"cost_inr":7106.0,"free_sessions":25,"included_sessions":334,"ondemand_sessions":0,"avg_tokens_session":510762,"daily_avg_tokens":5931430,"top_model":"auto","usage_level":"HIGH","pct_team":0.0412},
    "gairika.s@gsl.in":{"sessions":155,"input_tokens":8161468,"cache_read":170494174,"output_tokens":1221718,"total_tokens":181899585,"cost_usd":81.58,"cost_inr":7179.04,"free_sessions":0,"included_sessions":155,"ondemand_sessions":0,"avg_tokens_session":1173545,"daily_avg_tokens":5867728,"top_model":"auto","usage_level":"HIGH","pct_team":0.0407},
    "sanjeev.kumar@gsl.in":{"sessions":216,"input_tokens":4690978,"cache_read":161108021,"output_tokens":1424147,"total_tokens":181393965,"cost_usd":139.92,"cost_inr":12312.96,"free_sessions":29,"included_sessions":99,"ondemand_sessions":82,"avg_tokens_session":839786,"daily_avg_tokens":5851418,"top_model":"claude-4.6-sonnet-medium-thinking","usage_level":"HIGH","pct_team":0.0406},
    "soumik.b@gsl.in":{"sessions":183,"input_tokens":5913833,"cache_read":127556419,"output_tokens":1268507,"total_tokens":143673273,"cost_usd":118.45,"cost_inr":10423.6,"free_sessions":32,"included_sessions":93,"ondemand_sessions":56,"avg_tokens_session":785099,"daily_avg_tokens":4634621,"top_model":"claude-4.6-sonnet-medium-thinking","usage_level":"HIGH","pct_team":0.0322},
    "aritra.l@gsl.in":{"sessions":93,"input_tokens":4639994,"cache_read":126825355,"output_tokens":896939,"total_tokens":135759008,"cost_usd":85.67,"cost_inr":7538.96,"free_sessions":4,"included_sessions":87,"ondemand_sessions":0,"avg_tokens_session":1459774,"daily_avg_tokens":4379322,"top_model":"auto","usage_level":"HIGH","pct_team":0.0304},
    "anirudha.l@gsl.in":{"sessions":258,"input_tokens":9776976,"cache_read":122250678,"output_tokens":1086536,"total_tokens":133114190,"cost_usd":49.3,"cost_inr":4338.4,"free_sessions":0,"included_sessions":256,"ondemand_sessions":0,"avg_tokens_session":515946,"daily_avg_tokens":4294006,"top_model":"auto","usage_level":"HIGH","pct_team":0.0298},
    "rakesh.s@gsl.in":{"sessions":215,"input_tokens":7647289,"cache_read":114907643,"output_tokens":998678,"total_tokens":124213243,"cost_usd":53.84,"cost_inr":4737.92,"free_sessions":0,"included_sessions":215,"ondemand_sessions":0,"avg_tokens_session":577736,"daily_avg_tokens":4006878,"top_model":"auto","usage_level":"HIGH","pct_team":0.0278},
    "priyangshu.s@gsl.in":{"sessions":93,"input_tokens":4041419,"cache_read":115338038,"output_tokens":1076872,"total_tokens":124002609,"cost_usd":80.45,"cost_inr":7079.6,"free_sessions":30,"included_sessions":63,"ondemand_sessions":0,"avg_tokens_session":1333361,"daily_avg_tokens":4000084,"top_model":"claude-4.6-opus-high-thinking","usage_level":"HIGH","pct_team":0.0278},
    "debjit.g@gsl.in":{"sessions":104,"input_tokens":3574454,"cache_read":99905423,"output_tokens":766275,"total_tokens":109430924,"cost_usd":105.09,"cost_inr":9247.92,"free_sessions":3,"included_sessions":94,"ondemand_sessions":6,"avg_tokens_session":1052220,"daily_avg_tokens":3530029,"top_model":"claude-4.6-opus-high-thinking","usage_level":"HIGH","pct_team":0.0245},
    "abhishek.gupta@gsl.in":{"sessions":225,"input_tokens":6922927,"cache_read":82905805,"output_tokens":1116211,"total_tokens":99035904,"cost_usd":109.58,"cost_inr":9643.04,"free_sessions":89,"included_sessions":91,"ondemand_sessions":40,"avg_tokens_session":440159,"daily_avg_tokens":3194706,"top_model":"claude-4.6-opus-high-thinking","usage_level":"Normal","pct_team":0.0222},
    "jit.chatterjee@gsl.in":{"sessions":96,"input_tokens":2759884,"cache_read":87770676,"output_tokens":777597,"total_tokens":98374694,"cost_usd":80.16,"cost_inr":7054.08,"free_sessions":4,"included_sessions":88,"ondemand_sessions":0,"avg_tokens_session":1024736,"daily_avg_tokens":3173377,"top_model":"claude-4.6-sonnet-medium-thinking","usage_level":"Normal","pct_team":0.0220},
    "subhradeep.p@gsl.in":{"sessions":161,"input_tokens":6726714,"cache_read":71394432,"output_tokens":508061,"total_tokens":78629207,"cost_usd":29.14,"cost_inr":2564.32,"free_sessions":0,"included_sessions":154,"ondemand_sessions":0,"avg_tokens_session":488380,"daily_avg_tokens":2536426,"top_model":"auto","usage_level":"Normal","pct_team":0.0176},
    "shubham.m@gsl.in":{"sessions":183,"input_tokens":3686409,"cache_read":68142548,"output_tokens":574767,"total_tokens":72542157,"cost_usd":27.15,"cost_inr":2389.2,"free_sessions":0,"included_sessions":177,"ondemand_sessions":0,"avg_tokens_session":396405,"daily_avg_tokens":2340069,"top_model":"auto","usage_level":"Normal","pct_team":0.0162},
    "abhishek.j@gsl.in":{"sessions":303,"input_tokens":5178826,"cache_read":61951169,"output_tokens":752091,"total_tokens":70516363,"cost_usd":47.585,"cost_inr":4187.48,"free_sessions":0,"included_sessions":301,"ondemand_sessions":0,"avg_tokens_session":232727,"daily_avg_tokens":2274721,"top_model":"auto","usage_level":"Normal","pct_team":0.0158},
    "sarthak.s@gsl.in":{"sessions":188,"input_tokens":4802751,"cache_read":63661312,"output_tokens":505047,"total_tokens":69230497,"cost_usd":28.81,"cost_inr":2535.28,"free_sessions":0,"included_sessions":185,"ondemand_sessions":0,"avg_tokens_session":368247,"daily_avg_tokens":2233241,"top_model":"auto","usage_level":"Normal","pct_team":0.0155},
    "soumyojyoti.b@gsl.in":{"sessions":221,"input_tokens":9124185,"cache_read":52943909,"output_tokens":1017010,"total_tokens":68873140,"cost_usd":83.44,"cost_inr":7342.72,"free_sessions":28,"included_sessions":186,"ondemand_sessions":0,"avg_tokens_session":311643,"daily_avg_tokens":2221714,"top_model":"auto","usage_level":"Normal","pct_team":0.0154},
    "srishtik.s@gsl.in":{"sessions":70,"input_tokens":3170080,"cache_read":51967493,"output_tokens":464242,"total_tokens":56740020,"cost_usd":34.78,"cost_inr":3060.64,"free_sessions":0,"included_sessions":67,"ondemand_sessions":0,"avg_tokens_session":810571,"daily_avg_tokens":1830323,"top_model":"auto","usage_level":"Normal","pct_team":0.0127},
    "kanhaiya.g@gsl.in":{"sessions":139,"input_tokens":5697004,"cache_read":49953120,"output_tokens":515188,"total_tokens":56165312,"cost_usd":22.6,"cost_inr":1988.8,"free_sessions":0,"included_sessions":138,"ondemand_sessions":0,"avg_tokens_session":404066,"daily_avg_tokens":1811784,"top_model":"auto","usage_level":"Normal","pct_team":0.0126},
    "suhel.k@gsl.in":{"sessions":76,"input_tokens":3889074,"cache_read":44264974,"output_tokens":245744,"total_tokens":48712048,"cost_usd":17.78,"cost_inr":1564.64,"free_sessions":0,"included_sessions":76,"ondemand_sessions":0,"avg_tokens_session":640948,"daily_avg_tokens":1571356,"top_model":"auto","usage_level":"Normal","pct_team":0.0109},
    "swapnil.s@gsl.in":{"sessions":97,"input_tokens":4335335,"cache_read":34285216,"output_tokens":245888,"total_tokens":38866439,"cost_usd":15.43,"cost_inr":1357.84,"free_sessions":0,"included_sessions":96,"ondemand_sessions":0,"avg_tokens_session":400684,"daily_avg_tokens":1253756,"top_model":"auto","usage_level":"Normal","pct_team":0.0087},
    "anish.r@gsl.in":{"sessions":162,"input_tokens":6491954,"cache_read":30686875,"output_tokens":433142,"total_tokens":38305609,"cost_usd":27.78,"cost_inr":2444.64,"free_sessions":0,"included_sessions":162,"ondemand_sessions":0,"avg_tokens_session":236454,"daily_avg_tokens":1235664,"top_model":"auto","usage_level":"Normal","pct_team":0.0086},
    "daisy.f@gsl.in":{"sessions":92,"input_tokens":3257634,"cache_read":33581600,"output_tokens":386176,"total_tokens":37225410,"cost_usd":14.83,"cost_inr":1305.04,"free_sessions":0,"included_sessions":90,"ondemand_sessions":0,"avg_tokens_session":404624,"daily_avg_tokens":1200819,"top_model":"auto","usage_level":"Normal","pct_team":0.0083},
    "mohit.patil@gsl.in":{"sessions":68,"input_tokens":2666439,"cache_read":16731445,"output_tokens":217382,"total_tokens":19638878,"cost_usd":8.87,"cost_inr":780.56,"free_sessions":0,"included_sessions":68,"ondemand_sessions":0,"avg_tokens_session":288807,"daily_avg_tokens":633512,"top_model":"auto","usage_level":"Normal","pct_team":0.0044},
    "vaishnavi.p@gsl.in":{"sessions":106,"input_tokens":4583102,"cache_read":25364256,"output_tokens":274608,"total_tokens":30221966,"cost_usd":13.74,"cost_inr":1209.12,"free_sessions":0,"included_sessions":106,"ondemand_sessions":0,"avg_tokens_session":285112,"daily_avg_tokens":974902,"top_model":"auto","usage_level":"Normal","pct_team":0.0068},
    "parveen.k@gsl.in":{"sessions":59,"input_tokens":1645352,"cache_read":25709440,"output_tokens":240534,"total_tokens":27595326,"cost_usd":9.91,"cost_inr":872.08,"free_sessions":0,"included_sessions":59,"ondemand_sessions":0,"avg_tokens_session":467717,"daily_avg_tokens":890171,"top_model":"auto","usage_level":"Normal","pct_team":0.0062},
    "aaditi.s@gsl.in":{"sessions":53,"input_tokens":2730446,"cache_read":14717760,"output_tokens":119249,"total_tokens":17567455,"cost_usd":7.77,"cost_inr":683.76,"free_sessions":0,"included_sessions":52,"ondemand_sessions":0,"avg_tokens_session":331461,"daily_avg_tokens":566692,"top_model":"auto","usage_level":"Normal","pct_team":0.0039},
    "kajal.b@gsl.in":{"sessions":69,"input_tokens":3132656,"cache_read":19502720,"output_tokens":356432,"total_tokens":22991808,"cost_usd":10.96,"cost_inr":964.48,"free_sessions":0,"included_sessions":69,"ondemand_sessions":0,"avg_tokens_session":333214,"daily_avg_tokens":741671,"top_model":"auto","usage_level":"Normal","pct_team":0.0052},
    "pronati.k@gsl.in":{"sessions":64,"input_tokens":2499538,"cache_read":18867168,"output_tokens":222825,"total_tokens":21589531,"cost_usd":12.31,"cost_inr":1083.28,"free_sessions":0,"included_sessions":64,"ondemand_sessions":0,"avg_tokens_session":337336,"daily_avg_tokens":696436,"top_model":"composer-1.5","usage_level":"Normal","pct_team":0.0048},
    "sujoy.dutta@gsl.in":{"sessions":17,"input_tokens":9924,"cache_read":14904677,"output_tokens":125311,"total_tokens":16078711,"cost_usd":21.04,"cost_inr":1851.52,"free_sessions":0,"included_sessions":17,"ondemand_sessions":0,"avg_tokens_session":945806,"daily_avg_tokens":518668,"top_model":"claude-4.6-opus-high-thinking","usage_level":"Normal","pct_team":0.0036},
    "viraj.p@gsl.in":{"sessions":54,"input_tokens":1636348,"cache_read":8820256,"output_tokens":158751,"total_tokens":10615355,"cost_usd":5.22,"cost_inr":459.36,"free_sessions":0,"included_sessions":54,"ondemand_sessions":0,"avg_tokens_session":196580,"daily_avg_tokens":342430,"top_model":"auto","usage_level":"Normal","pct_team":0.0024},
    "mukesh.j@gsl.in":{"sessions":33,"input_tokens":890049,"cache_read":8831136,"output_tokens":177642,"total_tokens":9898827,"cost_usd":4.38,"cost_inr":385.44,"free_sessions":0,"included_sessions":33,"ondemand_sessions":0,"avg_tokens_session":299964,"daily_avg_tokens":319317,"top_model":"auto","usage_level":"Normal","pct_team":0.0022},
    "utkarsh.b@gsl.in":{"sessions":11,"input_tokens":2198318,"cache_read":5010304,"output_tokens":76119,"total_tokens":7284741,"cost_usd":4.46,"cost_inr":392.48,"free_sessions":0,"included_sessions":11,"ondemand_sessions":0,"avg_tokens_session":662249,"daily_avg_tokens":234991,"top_model":"auto","usage_level":"Normal","pct_team":0.0016},
    "anurag.r@gsl.in":{"sessions":31,"input_tokens":1200000,"cache_read":8000000,"output_tokens":150000,"total_tokens":9350000,"cost_usd":3.04,"cost_inr":285.44,"free_sessions":0,"included_sessions":31,"ondemand_sessions":0,"avg_tokens_session":301613,"daily_avg_tokens":301613,"top_model":"auto","usage_level":"Normal","pct_team":0.0013},
    "rohit.b@gsl.in":{"sessions":28,"input_tokens":900000,"cache_read":7000000,"output_tokens":100000,"total_tokens":8000000,"cost_usd":2.01,"cost_inr":188.74,"free_sessions":0,"included_sessions":28,"ondemand_sessions":0,"avg_tokens_session":285714,"daily_avg_tokens":258065,"top_model":"auto","usage_level":"Normal","pct_team":0.0009},
    "kalmesh.g@gsl.in":{"sessions":5,"input_tokens":200000,"cache_read":1000000,"output_tokens":30000,"total_tokens":1230000,"cost_usd":0.3,"cost_inr":28.17,"free_sessions":0,"included_sessions":5,"ondemand_sessions":0,"avg_tokens_session":246000,"daily_avg_tokens":39677,"top_model":"auto","usage_level":"Normal","pct_team":0.0002},
    "ashutosh.g@gsl.in":{"sessions":15,"input_tokens":400000,"cache_read":2000000,"output_tokens":50000,"total_tokens":2450000,"cost_usd":0.91,"cost_inr":85.46,"free_sessions":0,"included_sessions":15,"ondemand_sessions":0,"avg_tokens_session":163333,"daily_avg_tokens":79032,"top_model":"auto","usage_level":"Normal","pct_team":0.0003},
}

# ── HELPERS ────────────────────────────────────────────────────────────────────

def is_dummy(key: str) -> bool:
    return not key or "DUMMY" in str(key) or len(str(key)) < 8

def get_cursor_data(user: dict) -> Optional[dict]:
    email = user.get("cursor_email")
    if not email: return None
    live = SYNC_STATE["live_cursor_data"].get(email)
    return live if live else CURSOR_TOKEN_DATA.get(email)

def user_cost(user: dict) -> dict:
    seat = user["cost_per_mo"]
    cd = get_cursor_data(user)
    token = cd["cost_inr"] if cd else 0.0
    return {"seat_cost": round(seat,2), "token_cost": round(token,2), "total_cost": round(seat+token,2)}

def _prior_month(month: str) -> str:
    year, mo = int(month.split("-")[0]), int(month.split("-")[1])
    if mo == 1:
        return f"{year-1}-12"
    return f"{year}-{mo-1:02d}"

RECTIFICATION_KEYWORDS = re.compile(
    r'\b(fix|bug|patch|hotfix|revert|correct|rectif|amend)\b', re.IGNORECASE
)
PR_REF_PATTERN = re.compile(r'(fixes|closes|refs|resolves)\s+#(\d+)', re.IGNORECASE)

def _detect_rectifications(commits_this_month: List[dict], prior_month_pr_numbers: set,
                            cutoff_days: int = 90):
    """Return (rectification_lines, flagged_shas, heuristics_applied) for a list of commit dicts."""
    rect_lines = 0
    flagged_shas: List[str] = []
    heuristics_applied: set = set()
    cutoff_date = datetime.utcnow() - timedelta(days=cutoff_days)
    for commit in commits_this_month:
        msg = commit.get("message", "")
        commit_date_str = commit.get("date")
        additions = commit.get("additions", 0)
        is_rect = False
        # Heuristic 1: keyword match
        if RECTIFICATION_KEYWORDS.search(msg):
            is_rect = True
            heuristics_applied.add("keyword_match")
        # Heuristic 2: PR back-reference to prior month
        for match in PR_REF_PATTERN.finditer(msg):
            pr_num = int(match.group(2))
            if pr_num in prior_month_pr_numbers:
                is_rect = True
                heuristics_applied.add("pr_reference_back")
        # Heuristic 3: time-window guard — ignore if commit is older than cutoff
        if is_rect and commit_date_str:
            try:
                cdt = datetime.fromisoformat(commit_date_str.replace("Z",""))
                if cdt < cutoff_date:
                    is_rect = False
            except Exception:
                pass
        if is_rect:
            rect_lines += additions
            flagged_shas.append(commit.get("sha",""))
    return rect_lines, flagged_shas, list(heuristics_applied)

def compute_temporal_attribution(user_id: str, months_list: List[str]) -> List[dict]:
    """Chain carryover across ordered months and return list of snapshot dicts."""
    results = []
    running_carryover = 0
    for month in sorted(months_list):
        snap = MONTHLY_CODE_METRICS.get((user_id, month))
        if snap is None:
            continue
        _compute_derived_fields(snap, carryover_in=running_carryover)
        running_carryover = snap.carryover_out
        results.append(asdict(snap))
    return results

def _merge_code_metrics_live(github_data: dict, cursor_data: dict, month: str) -> None:
    """Merge live GitHub + Cursor data into MONTHLY_CODE_METRICS for the given month."""
    email_to_user = {u["cursor_email"]: u["id"] for u in USERS if u.get("cursor_email")}
    # Build github login → user lookup (simple: match by first part of email)
    email_to_login = {u["cursor_email"]: u["cursor_email"].split("@")[0] for u in USERS if u.get("cursor_email")}
    login_to_uid = {v: email_to_user[k] for k, v in email_to_login.items() if k in email_to_user}

    for email, cdata in cursor_data.items():
        user_id = email_to_user.get(email)
        if not user_id:
            continue
        login = email.split("@")[0]
        gh_entry = github_data.get(login, {})
        prev_snap = MONTHLY_CODE_METRICS.get((user_id, _prior_month(month)))
        carryover_in = prev_snap.carryover_out if prev_snap else 0
        ai_lines = int(cdata.get("output_tokens", 0) * CURSOR_LINES_PER_OUTPUT_TOKEN)
        snap = CodeMetricsSnapshot(
            user_id=user_id, month=month,
            ai_lines_written=ai_lines,
            github_committed=gh_entry.get("github_committed", 0),
            rectification_lines=gh_entry.get("rectification_lines", 0),
            rectification_commits=gh_entry.get("flagged_shas", []),
            heuristics_applied=gh_entry.get("heuristics_applied", []),
            data_source="live",
        )
        _compute_derived_fields(snap, carryover_in=carryover_in)
        MONTHLY_CODE_METRICS[(user_id, month)] = snap

# ── LIVE API CONNECTORS ────────────────────────────────────────────────────────

async def fetch_cursor_live(month: str = "2026-03") -> dict:
    cfg = CONFIG["cursor"]
    if not cfg["enabled"] or is_dummy(cfg["api_key"]) or not HTTPX_AVAILABLE:
        return {"status":"skipped","reason":"not configured or httpx unavailable"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"Authorization": f"Bearer {cfg['api_key']}"}
            url = f"{cfg['base_url']}/v1/teams/{cfg['team_id']}/usage?month={month}"
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                results = {}
                for member in data.get("members", []):
                    email = member.get("email","")
                    results[email] = {
                        "sessions": member.get("sessions",0),
                        "input_tokens": member.get("input_tokens",0),
                        "cache_read": member.get("cache_read_tokens",0),
                        "output_tokens": member.get("output_tokens",0),
                        "total_tokens": member.get("total_tokens",0),
                        "cost_usd": member.get("cost_usd",0.0),
                        "cost_inr": round(member.get("cost_usd",0.0)*93.91,2),
                        "free_sessions": member.get("free_sessions",0),
                        "included_sessions": member.get("included_sessions",0),
                        "ondemand_sessions": member.get("ondemand_sessions",0),
                        "avg_tokens_session": member.get("avg_tokens_per_session",0),
                        "daily_avg_tokens": member.get("daily_avg_tokens",0),
                        "top_model": member.get("top_model","auto"),
                        "usage_level": "HIGH" if member.get("total_tokens",0) > 100_000_000 else "Normal",
                        "pct_team": member.get("pct_team_tokens",0.0),
                        "source": "live",
                    }
                return {"status":"ok","users":len(results),"data":results}
            return {"status":"error","code":r.status_code,"detail":r.text[:200]}
    except Exception as e:
        return {"status":"error","detail":str(e)}

async def fetch_claude_live() -> dict:
    cfg = CONFIG["claude"]
    if not cfg["enabled"] or is_dummy(cfg["api_key"]) or not HTTPX_AVAILABLE:
        return {"status":"skipped","reason":"not configured"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"x-api-key":cfg["api_key"],"anthropic-version":"2023-06-01","anthropic-beta":"usage-data-2025-01-01"}
            rm = await client.get(f"{cfg['base_url']}/v1/organizations/{cfg['org_id']}/members", headers=headers)
            if rm.status_code != 200:
                return {"status":"error","code":rm.status_code,"detail":rm.text[:200]}
            members = rm.json().get("data",[])
            ru = await client.get(f"{cfg['base_url']}/v1/organizations/{cfg['org_id']}/usage", headers=headers,
                                  params={"start_time":"2026-03-01","end_time":"2026-03-31"})
            usage_by_user: Dict[str,Any] = {}
            if ru.status_code == 200:
                for row in ru.json().get("data",[]):
                    uid = row.get("user_id","")
                    if uid not in usage_by_user:
                        usage_by_user[uid] = {"input_tokens":0,"output_tokens":0,"cache_read":0,"cost_usd":0.0}
                    usage_by_user[uid]["input_tokens"] += row.get("input_tokens",0)
                    usage_by_user[uid]["output_tokens"] += row.get("output_tokens",0)
                    usage_by_user[uid]["cache_read"] += row.get("cache_read_input_tokens",0)
                    usage_by_user[uid]["cost_usd"] += row.get("cost_usd",0.0)
            results = {}
            for m in members:
                uid = m.get("id",""); email = m.get("email",""); u = usage_by_user.get(uid,{})
                results[email] = {
                    "user_id":uid,"display_name":m.get("name",email),"role":m.get("role","member"),
                    "input_tokens":u.get("input_tokens",0),"output_tokens":u.get("output_tokens",0),
                    "cache_read":u.get("cache_read",0),
                    "total_tokens":u.get("input_tokens",0)+u.get("output_tokens",0)+u.get("cache_read",0),
                    "cost_usd":round(u.get("cost_usd",0.0),2),
                    "cost_inr":round(u.get("cost_usd",0.0)*93.91,2),
                    "source":"live",
                }
        return {"status":"ok","users":len(results),"data":results}
    except Exception as e:
        return {"status":"error","detail":str(e)}

async def fetch_github_live(user_logins: List[str]) -> dict:
    cfg = CONFIG["github"]
    if not cfg["enabled"] or is_dummy(cfg["token"]) or not HTTPX_AVAILABLE:
        return {"status":"skipped","reason":"not configured"}
    results = {}
    headers = {"Authorization":f"Bearer {cfg['token']}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
    since="2026-03-01T00:00:00Z"; until="2026-03-31T23:59:59Z"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for login in user_logins[:20]:
                rp = await client.get(f"{cfg['base_url']}/search/issues?q=author:{login}+type:pr+org:{cfg['org']}+merged:{since}..{until}", headers=headers)
                rc = await client.get(f"{cfg['base_url']}/search/commits?q=author:{login}+org:{cfg['org']}+author-date:{since}..{until}", headers=headers)
                results[login] = {
                    "prs_merged": rp.json().get("total_count",0) if rp.status_code==200 else 0,
                    "commits": rc.json().get("total_count",0) if rc.status_code==200 else 0,
                    "source":"live",
                }
        return {"status":"ok","users":len(results),"data":results}
    except Exception as e:
        return {"status":"error","detail":str(e)}

async def _fetch_pr_numbers_for_month(client, login: str, cfg: dict, month: str, headers: dict) -> set:
    """Fetch PR numbers merged in the given month (used for rectification heuristic 2)."""
    year, mo = int(month.split("-")[0]), int(month.split("-")[1])
    last_day = calendar.monthrange(year, mo)[1]
    since = f"{month}-01"; until = f"{month}-{last_day:02d}"
    try:
        rp = await client.get(
            f"{cfg['base_url']}/search/issues",
            params={"q": f"author:{login}+type:pr+org:{cfg['org']}+created:{since}..{until}"},
            headers=headers
        )
        if rp.status_code == 200:
            return {item.get("number") for item in rp.json().get("items", [])}
    except Exception:
        pass
    return set()

async def fetch_github_detailed_live(user_logins: List[str], month: str) -> dict:
    """Fetch per-commit line stats and detect rectifications for the given month."""
    cfg = CONFIG["github"]
    if not cfg["enabled"] or is_dummy(cfg["token"]) or not HTTPX_AVAILABLE:
        return {"status": "skipped", "reason": "not configured"}
    year, mo = int(month.split("-")[0]), int(month.split("-")[1])
    last_day = calendar.monthrange(year, mo)[1]
    since = f"{month}-01T00:00:00Z"; until = f"{month}-{last_day:02d}T23:59:59Z"
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            prior = _prior_month(month)
            for login in user_logins[:20]:
                # Get commit list for this user/month (search API)
                rc = await client.get(
                    f"{cfg['base_url']}/search/commits",
                    params={"q": f"author:{login}+org:{cfg['org']}+author-date:{since[:10]}..{until[:10]}"},
                    headers={**headers, "Accept": "application/vnd.github.cloak-preview+json"},
                )
                if rc.status_code != 200:
                    results[login] = {"error": rc.status_code}
                    continue
                commits_raw = rc.json().get("items", [])
                # Fetch per-commit stats (cap at 40 to stay within rate limits)
                commits_detailed: List[dict] = []
                for c in commits_raw[:40]:
                    sha = c.get("sha", "")
                    repo_url = c.get("repository", {}).get("url", "")
                    if not repo_url or not sha:
                        continue
                    rd = await client.get(f"{repo_url}/commits/{sha}", headers=headers)
                    if rd.status_code == 200:
                        cd_json = rd.json()
                        stats = cd_json.get("stats", {})
                        commits_detailed.append({
                            "sha": sha,
                            "message": cd_json.get("commit", {}).get("message", ""),
                            "date": cd_json.get("commit", {}).get("author", {}).get("date", ""),
                            "additions": stats.get("additions", 0),
                            "deletions": stats.get("deletions", 0),
                        })
                # Get prior-month PR numbers for heuristic 2
                prior_prs = await _fetch_pr_numbers_for_month(client, login, cfg, prior, headers)
                rect_lines, flagged_shas, heuristics = _detect_rectifications(commits_detailed, prior_prs)
                total_additions = sum(c["additions"] for c in commits_detailed)
                results[login] = {
                    "github_committed": total_additions,
                    "rectification_lines": rect_lines,
                    "flagged_shas": flagged_shas,
                    "heuristics_applied": heuristics,
                    "commits_analyzed": len(commits_detailed),
                    "source": "live",
                }
        return {"status": "ok", "users": len(results), "data": results, "month": month}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

async def fetch_jira_live() -> dict:
    cfg = CONFIG["jira"]
    if not cfg["enabled"] or is_dummy(cfg["api_token"]) or not HTTPX_AVAILABLE:
        return {"status":"skipped","reason":"not configured"}
    import base64
    creds = base64.b64encode(f"{cfg['email']}:{cfg['api_token']}".encode()).decode()
    headers = {"Authorization":f"Basic {creds}","Accept":"application/json"}
    results: Dict[str,Any] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            rb = await client.get(f"{cfg['base_url']}/rest/agile/1.0/board", headers=headers)
            if rb.status_code != 200:
                return {"status":"error","code":rb.status_code}
            for board in rb.json().get("values",[])[:5]:
                rs = await client.get(f"{cfg['base_url']}/rest/agile/1.0/board/{board['id']}/sprint?state=closed", headers=headers)
                if rs.status_code != 200: continue
                for sprint in rs.json().get("values",[]):
                    if "2026-03" not in sprint.get("endDate",""): continue
                    ri = await client.get(f"{cfg['base_url']}/rest/agile/1.0/sprint/{sprint['id']}/issue?maxResults=200", headers=headers)
                    if ri.status_code != 200: continue
                    for issue in ri.json().get("issues",[]):
                        assignee = issue["fields"].get("assignee")
                        if not assignee: continue
                        email = assignee.get("emailAddress","")
                        sp = issue["fields"].get("story_points") or issue["fields"].get("customfield_10016") or 0
                        status = issue["fields"]["status"]["statusCategory"]["name"]
                        if email not in results:
                            results[email] = {"tickets_done":0,"story_points":0,"tickets_total":0}
                        results[email]["tickets_total"] += 1
                        if status == "Done":
                            results[email]["tickets_done"] += 1
                            results[email]["story_points"] += (sp or 0)
        for e in results: results[e]["source"] = "live"
        return {"status":"ok","users":len(results),"data":results}
    except Exception as e:
        return {"status":"error","detail":str(e)}

# ── SYNC TASK ──────────────────────────────────────────────────────────────────

async def run_sync():
    SYNC_STATE.update({"running":True,"progress":0,"steps":[],"errors":[]})
    steps = [
        ("Cursor token usage", fetch_cursor_live(), "live_cursor_data"),
        ("Claude team usage", fetch_claude_live(), "live_claude_data"),
        ("GitHub metrics", fetch_github_live([]), "live_github_data"),
        ("Jira metrics", fetch_jira_live(), "live_jira_data"),
    ]
    for i,(label,coro,key) in enumerate(steps):
        SYNC_STATE["steps"].append({"label":label,"status":"running","detail":""})
        try:
            result = await coro
            if result.get("status") == "ok":
                SYNC_STATE[key] = result.get("data",{})
                SYNC_STATE["steps"][-1].update({"status":"done","detail":f"{result.get('users',0)} users synced"})
            elif result.get("status") == "skipped":
                SYNC_STATE["steps"][-1].update({"status":"skipped","detail":result.get("reason","not configured")})
            else:
                SYNC_STATE["steps"][-1].update({"status":"error","detail":result.get("detail","unknown")})
                SYNC_STATE["errors"].append(f"{label}: {result.get('detail','')}")
        except Exception as e:
            SYNC_STATE["steps"][-1].update({"status":"error","detail":str(e)})
            SYNC_STATE["errors"].append(f"{label}: {e}")
        SYNC_STATE["progress"] = int(((i+1)/len(steps))*100)
    # Step 5: Code Intelligence — merge GitHub line stats + Cursor tokens into temporal attribution
    SYNC_STATE["steps"].append({"label":"Code Intelligence","status":"running","detail":""})
    try:
        current_month = datetime.now().strftime("%Y-%m")
        gh_raw = SYNC_STATE.get("live_github_data", {})
        cu_raw = SYNC_STATE.get("live_cursor_data", {})
        _merge_code_metrics_live(gh_raw, cu_raw, current_month)
        SYNC_STATE["steps"][-1].update({"status":"done","detail":f"{len(MONTHLY_CODE_METRICS)} snapshots"})
    except Exception as e:
        SYNC_STATE["steps"][-1].update({"status":"error","detail":str(e)})
        SYNC_STATE["errors"].append(f"Code Intelligence: {e}")
    # Step 6: Persist to DB — save everything to SQLite for historical access
    SYNC_STATE["steps"].append({"label":"Persist to DB","status":"running","detail":""})
    try:
        current_month = datetime.now().strftime("%Y-%m")
        cu_saved = db_save_cursor_month(current_month, SYNC_STATE.get("live_cursor_data", {}))
        cm_saved = db_save_code_metrics_month(current_month)
        jira_saved = db_save_jira_month(current_month, SYNC_STATE.get("live_jira_data", {}))
        db_log_sync(current_month, cu_saved, len(SYNC_STATE.get("live_github_data",{})),
                    cm_saved, SYNC_STATE["errors"])
        SYNC_STATE["steps"][-1].update({"status":"done",
            "detail":f"Cursor: {cu_saved} rows · Code: {cm_saved} rows · Jira: {jira_saved} rows"})
    except Exception as e:
        SYNC_STATE["steps"][-1].update({"status":"error","detail":str(e)})
        SYNC_STATE["errors"].append(f"DB persist: {e}")
    SYNC_STATE.update({"running":False,"last_run":datetime.now().isoformat()})

# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status":"running","version":"0.3.0","httpx_available":HTTPX_AVAILABLE,
            "integrations":{k:v["enabled"] for k,v in CONFIG.items()},
            "last_sync":SYNC_STATE["last_run"], **db_get_stats()}

@app.get("/api/available-months")
def api_available_months():
    """Return all months with data in the DB plus the static seeded months."""
    db_months = db_get_available_months()
    db_month_keys = {m["month"] for m in db_months}
    static_months = [
        {"month":"2026-04","has_cursor":False,"has_code_metrics":False,"source":"static"},
        {"month":"2026-03","has_cursor":False,"has_code_metrics":False,"source":"static"},
        {"month":"2026-02","has_cursor":False,"has_code_metrics":True,"source":"demo"},
        {"month":"2026-01","has_cursor":False,"has_code_metrics":False,"source":"static"},
        {"month":"2025-12","has_cursor":False,"has_code_metrics":False,"source":"static"},
        {"month":"2025-11","has_cursor":False,"has_code_metrics":False,"source":"static"},
    ]
    # Merge: DB entries take precedence over static
    merged = {m["month"]: {**m,"source":"db"} for m in db_months}
    for s in static_months:
        if s["month"] not in merged:
            merged[s["month"]] = s
    return sorted(merged.values(), key=lambda x: x["month"], reverse=True)

@app.get("/api/history/{month}")
def api_history_month(month: str):
    """Load a historical month from DB into memory and return a summary snapshot."""
    # Load cursor data from DB into a temporary overlay
    cursor_rows = db_load_cursor_month(month)
    code_rows   = db_load_code_metrics_month(month)
    if not cursor_rows and not code_rows:
        raise HTTPException(404, f"No persisted data found for {month}. Run a sync first.")
    # Build per-user summary from cursor data
    email_to_uid = {u["cursor_email"]: u for u in USERS if u.get("cursor_email")}
    users_summary = []
    for email, cd in cursor_rows.items():
        u = email_to_uid.get(email)
        if not u: continue
        snap = MONTHLY_CODE_METRICS.get((u["id"], month))
        users_summary.append({
            "user_id": u["id"], "name": u["name"], "team": u["team"],
            "seat_cost": u["cost_per_mo"],
            "token_cost": cd["cost_inr"],
            "total_cost": round(u["cost_per_mo"] + cd["cost_inr"], 2),
            "sessions": cd["sessions"], "total_tokens": cd["total_tokens"],
            "top_model": cd["top_model"], "usage_level": cd["usage_level"],
            "source": "db",
            "ai_utilization_rate": snap.ai_utilization_rate if snap else None,
            "carryover_out": snap.carryover_out if snap else None,
        })
    users_summary.sort(key=lambda x: x["total_cost"], reverse=True)
    total_seat  = sum(u["seat_cost"] for u in users_summary)
    total_token = sum(u["token_cost"] for u in users_summary)
    return {
        "month": month, "source": "db",
        "users": users_summary,
        "summary": {
            "total_users": len(users_summary),
            "total_seat_cost": round(total_seat, 2),
            "total_token_cost": round(total_token, 2),
            "total_cost": round(total_seat + total_token, 2),
            "total_sessions": sum(u["sessions"] for u in users_summary),
            "total_tokens": sum(u["total_tokens"] for u in users_summary),
            "code_users": code_rows,
        },
    }

@app.get("/api/history/sync-log")
def api_sync_log():
    """Return the full sync history log from DB."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY synced_at DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/config")
def get_cfg():
    out={}
    for k,v in CONFIG.items():
        fields={}
        for fk,fv in v.items():
            if fk=="enabled": continue
            if fk in ("api_key","api_token","token"):
                filled = bool(fv) and not is_dummy(fv)
                fields[fk]={"filled":filled,"masked":("***"+str(fv)[-4:]) if (fv and not is_dummy(fv) and len(str(fv))>4) else ""}
            else:
                fields[fk]={"filled":bool(fv) and not is_dummy(str(fv)),"value":fv if not is_dummy(str(fv)) else ""}
        out[k]={"enabled":v["enabled"],"fields":fields}
    return out

@app.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    if SYNC_STATE["running"]: return {"status":"already_running"}
    background_tasks.add_task(run_sync)
    return {"status":"started"}

@app.get("/api/sync/status")
def sync_status():
    return {k:SYNC_STATE[k] for k in ["running","progress","last_run","steps","errors"]}

@app.get("/api/users")
def api_users(team: Optional[str]=None, month: Optional[str]=None):
    return [u for u in USERS if u["team"]==team] if team else USERS

@app.get("/api/teams")
def api_teams(month: Optional[str]=None):
    teams: Dict[str,Any]={}
    for u in USERS:
        t=u["team"]
        if t not in teams: teams[t]={"name":t,"headcount":0,"cursor_users":0,"claude_users":0,"chatgpt_users":0,"total_seat_cost":0}
        teams[t]["headcount"]+=1; teams[t]["total_seat_cost"]+=u["cost_per_mo"]
        if "Cursor" in u["tools"]: teams[t]["cursor_users"]+=1
        if "Claude" in u["tools"]: teams[t]["claude_users"]+=1
        if "ChatGPT" in u["tools"]: teams[t]["chatgpt_users"]+=1
    return list(teams.values())

@app.get("/api/user/{user_id}")
def api_user(user_id: str, month: Optional[str]=None):
    user=next((u for u in USERS if u["id"]==user_id),None)
    if not user: raise HTTPException(404,"User not found")
    cd=get_cursor_data(user)
    return {**user,"cursor_usage":cd,"cost_breakdown":user_cost(user),"data_source":"live" if (cd and cd.get("source")=="live") else "seeded"}

@app.get("/api/team/{team_name}")
def api_team(team_name: str, month: Optional[str]=None):
    tu=[u for u in USERS if u["team"]==team_name]
    if not tu: raise HTTPException(404,"Team not found")
    members=[]; ts=tt=tses=ttok=0
    for u in tu:
        cd=get_cursor_data(u); c=user_cost(u); ts+=c["seat_cost"]; tt+=c["token_cost"]
        m={"id":u["id"],"name":u["name"],"title":u["title"],"location":u["location"],"tools":u["tools"],
           "seat_cost":c["seat_cost"],"token_cost":c["token_cost"],"total_cost":c["total_cost"],
           "cursor_sessions":0,"cursor_total_tokens":0,"cursor_cost_inr":0,"cursor_usage_level":None,
           "cursor_top_model":None,"cursor_daily_avg":0,"cursor_ondemand":0}
        if cd:
            m.update({"cursor_sessions":cd["sessions"],"cursor_total_tokens":cd["total_tokens"],
                      "cursor_cost_inr":cd["cost_inr"],"cursor_usage_level":cd["usage_level"],
                      "cursor_top_model":cd["top_model"],"cursor_daily_avg":cd["daily_avg_tokens"],
                      "cursor_ondemand":cd["ondemand_sessions"]})
            tses+=cd["sessions"]; ttok+=cd["total_tokens"]
        members.append(m)
    members.sort(key=lambda x:x["total_cost"],reverse=True)
    return {"team":team_name,"headcount":len(tu),"total_seat_cost":round(ts,2),"total_token_cost":round(tt,2),
            "total_cost":round(ts+tt,2),"total_cursor_sessions":tses,"total_tokens_consumed":ttok,
            "cursor_users":sum(1 for u in tu if "Cursor" in u["tools"]),
            "claude_users":sum(1 for u in tu if "Claude" in u["tools"]),
            "chatgpt_users":sum(1 for u in tu if "ChatGPT" in u["tools"]),
            "members":members}

@app.get("/api/overview")
def api_overview(month: Optional[str]=None):
    ts=sum(u["cost_per_mo"] for u in USERS); tt=sum(user_cost(u)["token_cost"] for u in USERS)
    tses=sum(d["sessions"] for d in CURSOR_TOKEN_DATA.values())
    ttok=sum(d["total_tokens"] for d in CURSOR_TOKEN_DATA.values())
    teams: Dict[str,Any]={}
    for u in USERS:
        t=u["team"]
        if t not in teams: teams[t]={"name":t,"headcount":0,"cursor_users":0,"claude_users":0,"chatgpt_users":0,"seat_cost":0,"token_cost":0,"cursor_sessions":0,"total_tokens":0}
        teams[t]["headcount"]+=1; teams[t]["seat_cost"]+=u["cost_per_mo"]
        cd=get_cursor_data(u)
        if cd: teams[t]["token_cost"]+=cd["cost_inr"]; teams[t]["cursor_sessions"]+=cd["sessions"]; teams[t]["total_tokens"]+=cd["total_tokens"]
        if "Cursor" in u["tools"]: teams[t]["cursor_users"]+=1
        if "Claude" in u["tools"]: teams[t]["claude_users"]+=1
        if "ChatGPT" in u["tools"]: teams[t]["chatgpt_users"]+=1
    for v in teams.values(): v["total_cost"]=round(v["seat_cost"]+v["token_cost"],2); v["seat_cost"]=round(v["seat_cost"],2); v["token_cost"]=round(v["token_cost"],2)
    tl=sorted(teams.values(),key=lambda x:x["total_cost"],reverse=True)
    cr=[]
    for u in USERS:
        cd=get_cursor_data(u)
        if cd: cr.append({"id":u["id"],"name":u["name"],"team":u["team"],"sessions":cd["sessions"],"total_tokens":cd["total_tokens"],"cost_inr":cd["cost_inr"],"usage_level":cd["usage_level"],"top_model":cd["top_model"]})
    cr.sort(key=lambda x:x["total_tokens"],reverse=True)
    period_label = month if month else "March 2026"
    return {"period":period_label,"total_users":len(USERS),
            "cursor_seats":sum(1 for u in USERS if "Cursor" in u["tools"]),
            "claude_seats":sum(1 for u in USERS if "Claude" in u["tools"]),
            "chatgpt_seats":sum(1 for u in USERS if "ChatGPT" in u["tools"]),
            "total_seat_cost":round(ts,2),"total_token_cost":round(tt,2),"total_cost":round(ts+tt,2),
            "total_cursor_sessions":tses,"total_tokens_consumed":ttok,
            "teams":tl,"top_cursor_users":cr[:10],"bottom_cursor_users":cr[-5:] if len(cr)>5 else cr,
            "live_data":bool(SYNC_STATE["live_cursor_data"] or SYNC_STATE["live_claude_data"])}

@app.get("/api/cost-breakdown")
def api_costs(month: Optional[str]=None):
    rows=[]
    for u in USERS:
        c=user_cost(u); cd=get_cursor_data(u)
        rows.append({"id":u["id"],"name":u["name"],"team":u["team"],"title":u["title"],"location":u["location"],
                     "tools":u["tools"],"seat_cost":c["seat_cost"],"token_cost":c["token_cost"],"total_cost":c["total_cost"],
                     "cursor_sessions":cd["sessions"] if cd else 0,"cursor_tokens":cd["total_tokens"] if cd else 0,
                     "cursor_model":cd["top_model"] if cd else None,"cursor_ondemand":cd["ondemand_sessions"] if cd else 0,
                     "data_source":"live" if (cd and cd.get("source")=="live") else "seeded"})
    rows.sort(key=lambda x:x["total_cost"],reverse=True)
    return rows

@app.get("/api/export")
def api_export(month: Optional[str]=None):
    period = month or "mar2026"
    rows=[]
    for u in USERS:
        c=user_cost(u); cd=get_cursor_data(u)
        rows.append({"Emp ID":u["id"],"Name":u["name"],"Team":u["team"],"Title":u["title"],"Location":u["location"],
                     "Tools":", ".join(u["tools"]),"Seat Cost INR":c["seat_cost"],"Token Cost INR":c["token_cost"],
                     "Total Cost INR":c["total_cost"],"Cursor Sessions":cd["sessions"] if cd else "",
                     "Cursor Total Tokens":cd["total_tokens"] if cd else "","Cursor Cost INR":cd["cost_inr"] if cd else "",
                     "Cursor Top Model":cd["top_model"] if cd else "","Cursor Usage Level":cd["usage_level"] if cd else "",
                     "Data Source":"live" if (cd and cd.get("source")=="live") else "seeded"})
    out=io.StringIO(); w=csv.DictWriter(out,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows); out.seek(0)
    fname = f"ai_efficiency_{period.replace('-','')}.csv"
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename={fname}"})

class IntegrationUpdate(BaseModel):
    service:str; api_key:Optional[str]=None; api_token:Optional[str]=None; org_id:Optional[str]=None
    team_id:Optional[str]=None; base_url:Optional[str]=None; email:Optional[str]=None
    org:Optional[str]=None; token:Optional[str]=None; enabled:bool=True

@app.post("/api/config/update")
def update_cfg(u: IntegrationUpdate):
    if u.service not in CONFIG: raise HTTPException(400,f"Unknown: {u.service}")
    c=CONFIG[u.service]
    for f in ["api_key","api_token","org_id","team_id","base_url","email","org","token"]:
        v=getattr(u,f,None)
        if v: c[f]=v
    c["enabled"]=u.enabled
    return {"status":"updated","service":u.service,"enabled":c["enabled"]}

@app.get("/api/code-intelligence")
def api_code_intelligence(user_id: Optional[str] = None, months: Optional[str] = None):
    available_months = sorted({m for (_, m) in MONTHLY_CODE_METRICS.keys()})
    target_months = months.split(",") if months else (available_months[-2:] if len(available_months) >= 2 else available_months)
    target_users = [u for u in USERS if "Cursor" in u["tools"]]
    if user_id:
        target_users = [u for u in target_users if u["id"] == user_id]
    results = []
    for user in target_users:
        uid = user["id"]
        user_months = compute_temporal_attribution(uid, target_months)
        if not user_months:
            continue
        latest = user_months[-1]
        results.append({
            "user_id": uid, "name": user["name"], "team": user["team"], "title": user["title"],
            "months": user_months,
            "summary": {
                "avg_utilization_rate": round(sum(m["ai_utilization_rate"] for m in user_months) / len(user_months), 4),
                "total_ai_lines": sum(m["ai_lines_written"] for m in user_months),
                "total_committed": sum(m["github_committed"] for m in user_months),
                "total_rectifications": sum(m["rectification_lines"] for m in user_months),
                "current_carryover_buffer": latest.get("carryover_out", 0),
                "avg_backfill_rate": round(sum(m["backfill_rate"] for m in user_months) / len(user_months), 4),
            }
        })
    all_snaps = [m for r in results for m in r["months"]]
    org_summary = {
        "org_avg_utilization": round(sum(s["ai_utilization_rate"] for s in all_snaps) / len(all_snaps), 4) if all_snaps else 0,
        "total_carryover_buffer": sum(r["summary"]["current_carryover_buffer"] for r in results),
        "org_avg_backfill_rate": round(sum(s["backfill_rate"] for s in all_snaps) / len(all_snaps), 4) if all_snaps else 0,
        "users_analyzed": len(results),
        "months_analyzed": target_months,
    }
    data_sources = list({s["data_source"] for s in all_snaps}) if all_snaps else ["demo"]
    return {"org_summary": org_summary, "users": results, "data_source": data_sources[0] if len(data_sources)==1 else "mixed"}

@app.get("/api/code-intelligence/month/{month}")
def api_code_intelligence_month(month: str):
    rows = []
    for user in USERS:
        if "Cursor" not in user["tools"]:
            continue
        snap = MONTHLY_CODE_METRICS.get((user["id"], month))
        if not snap:
            continue
        row = asdict(snap)
        row.update({"name": user["name"], "team": user["team"], "title": user["title"]})
        rows.append(row)
    rows.sort(key=lambda x: x["ai_utilization_rate"], reverse=True)
    if not rows:
        return {"month": month, "users": [], "org_summary": {}, "available_months": sorted({m for (_, m) in MONTHLY_CODE_METRICS.keys()})}
    org_summary = {
        "avg_utilization": round(sum(r["ai_utilization_rate"] for r in rows) / len(rows), 4),
        "total_ai_lines": sum(r["ai_lines_written"] for r in rows),
        "total_committed": sum(r["github_committed"] for r in rows),
        "total_rectifications": sum(r["rectification_lines"] for r in rows),
        "total_carryover_out": sum(r["carryover_out"] for r in rows),
        "users_with_data": len(rows),
    }
    return {"month": month, "org_summary": org_summary, "users": rows,
            "available_months": sorted({m for (_, m) in MONTHLY_CODE_METRICS.keys()})}

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8900,reload=True)
