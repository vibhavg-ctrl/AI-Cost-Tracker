# ⚡ AI Efficiency Intelligence Platform

> Track AI tool ROI across your engineering org. Cross-reference Cursor, Claude & ChatGPT spend against real delivery output.

![Python](https://img.shields.io/badge/python-3.9+-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688) ![No Docker](https://img.shields.io/badge/no-docker-lightgrey) ![Localhost](https://img.shields.io/badge/runs-localhost%20only-orange)

---

## Quick Start

```bash
git clone https://github.com/vibhavg-ctrl/ai-efficiency-platform
cd ai-efficiency-platform
bash start.sh
```

| | URL |
|---|---|
| Dashboard | http://127.0.0.1:8901 |
| API | http://127.0.0.1:8900 |
| API Docs | http://127.0.0.1:8900/docs |

Setup time: ~30 seconds. No database, no Docker, no build step.

---

## The Problem This Solves

Seat cost tracking tells you *who has access* and *what it costs*. It does **not** tell you whether the money is well spent.

This platform closes that loop by cross-referencing AI tool usage against actual engineering output — commits shipped, tickets closed, and code quality signals.

---

## What It Measures

| Metric | Formula | What It Tells You |
|--------|---------|-------------------|
| **AI utilization rate** | AI-attributed committed lines / AI lines written | Are generated lines making it into the codebase? |
| **Carryover buffer** | AI lines written - lines committed (carries forward) | Accumulated AI work not yet shipped |
| **Backfill rate** | Rectification lines / total committed | How much committed code is fixing prior-month AI output? |
| **Cost per ticket** | Total AI cost / Jira tickets completed | What does each deliverable cost in AI spend? |
| **Seat vs. token cost** | Fixed subscription vs. on-demand overage | Where is the real spend going? |
| **Per-engineer breakdown** | Usage, cost and efficiency score per user | Who is getting value and who isn't? |

### Code Intelligence: Carryover Logic

A key insight in this platform — AI-written code that isn't committed in month N doesn't disappear. It **carries over** as a buffer into month N+1.

```
Month N:
  AI lines written:   7,000
  GitHub committed:   5,000
  → Carryover out:    2,000

Month N+1:
  AI lines written:        5,500
  Carryover in:            2,000
  Effective AI available:  7,500
  GitHub committed:        8,000  (of which 1,500 are rectifications)
  Effective new commits:   6,500
  AI attributed:           min(6,500, 7,500) = 6,500
  Utilization rate:        6,500 / 7,500 = 86.7%
```

Rectification detection uses two heuristics:
- **Keyword match** — commit messages containing `fix`, `bug`, `patch`, `hotfix`, `revert`, `correct`, `rectif`, `amend`
- **PR back-reference** — commit references a PR number from the prior month (`fixes #123`, `closes #456`)

---

## Integrations

| Tool | What It Provides | How to Connect |
|------|-----------------|----------------|
| **Cursor** | Token usage, sessions, model breakdown | Cursor Admin API key via Settings |
| **Claude** | Team plan usage, token cost per user | Anthropic Admin API key via Settings |
| **GitHub** | Commits, PRs, lines added/removed | GitHub PAT with `repo` scope via Settings |
| **Jira** | Tickets, story points, sprint velocity | Atlassian API token via Settings |

All integrations are optional. The platform works fully on demo data out of the box. API keys entered via the Settings page stay in memory only — never written to disk or logged.

---

## Dashboard Views

| View | What You See |
|------|-------------|
| **Overview** | Org-wide AI spend, sessions, top/bottom users by token usage |
| **Team drilldown** | Per-team metrics, member table with cost and usage breakdown |
| **User profile** | Full per-user view — Cursor, Claude, GitHub, cost |
| **Code intelligence** | AI utilization rate, carryover buffer, backfill rate |
| **Cost analysis** | Seat vs. token cost, per-user spend table |
| **Settings** | API key configuration, live/demo toggle |

---

## Project Structure

```
ai-efficiency-platform/
├── start.sh               # one-command launcher
├── backend/
│   ├── app.py             # FastAPI — all logic here
│   └── requirements.txt
├── frontend/
│   └── index.html         # React SPA, no build step needed
├── exports/               # CSV exports land here
└── data/                  # future: local data cache
```

---

## Connecting Real Data

### 1. Add your team roster

Edit the `USERS` list in `backend/app.py`:

```python
{
    "id":           "EMP001",
    "name":         "Alex Rivera",
    "title":        "SDE-2",
    "location":     "HQ",
    "team":         "Backend",
    "tools":        ["Cursor", "Claude"],
    "cost_per_mo":  5300.0,            # total monthly seat cost
    "cursor_email": "alex@yourco.com", # must match Cursor admin email
}
```

### 2. Update seat costs

```python
SEAT_COSTS = {
    "Cursor":  3500.0,   # your monthly cost per seat
    "Claude":  1800.0,
    "ChatGPT": 2000.0,
}
```

### 3. Connect live APIs (optional)

Use the **Settings** page in the dashboard, or edit `CONFIG` in `app.py` directly:

```python
CONFIG = {
    "claude":  {"api_key": "sk-ant-YOUR-KEY", "org_id": "org-YOUR-ORG", "enabled": True},
    "cursor":  {"api_key": "YOUR-CURSOR-KEY", "team_id": "YOUR-TEAM-ID", "enabled": True},
    "github":  {"token": "ghp_YOUR-TOKEN", "org": "your-github-org", "enabled": True},
    "jira":    {"base_url": "https://your-org.atlassian.net", "email": "you@yourco.com", "api_token": "YOUR-TOKEN", "enabled": True},
}
```

---

## API Reference

Base URL: `http://127.0.0.1:8900/api` — Interactive docs at `/docs`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service status |
| `/users` | GET | All users. Filter with `?team=Backend` |
| `/teams` | GET | Team summaries |
| `/user/{id}` | GET | Single user with cost breakdown |
| `/team/{name}` | GET | Team with full member details |
| `/overview` | GET | Org-wide dashboard data |
| `/cost-breakdown` | GET | Per-user seat + token cost |
| `/code-intelligence` | GET | AI utilization + carryover data |
| `/code-intelligence/month/{YYYY-MM}` | GET | Monthly snapshot for all users |
| `/export` | GET | Download CSV of all data |
| `/sync` | POST | Trigger live data sync |
| `/sync/status` | GET | Poll sync progress |
| `/config/update` | POST | Update API keys at runtime |

All GET endpoints accept `?month=YYYY-MM`.

---

## Requirements

- Python 3.9+
- macOS or Linux (Windows: use WSL)
- No database, no Docker, no build step required

---

## Security

- Runs on `127.0.0.1` only — not accessible from other machines
- No telemetry, no external calls when using demo data
- API keys entered via Settings are in-memory only during runtime
- Export CSVs save to your local filesystem only

---

## Planned Enhancements

- [ ] SQLite-backed time-series storage for historical trend analysis
- [ ] Automated data pull via cron/scheduled tasks
- [ ] Alerting when utilization rate drops below threshold
- [ ] Sprint-over-sprint velocity comparison
- [ ] Excel export with formatting
- [ ] User management page (add/remove users without editing code)
- [ ] Docker support

---

## Contributing

Pull requests are welcome. For major changes, open an issue first.

1. Fork the repo
2. Create your branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m 'Add my feature'`
4. Push: `git push origin feature/my-feature`
5. Open a Pull Request

---

## License

MIT — see [LICENSE](LICENSE)
