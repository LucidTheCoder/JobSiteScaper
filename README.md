# NZ Bank Job Scraper

Monitors NZ bank career pages and sends new job listings to a Discord channel.

**Sites monitored:** Heartland Bank · MTF Finance · Avanti Finance · Kiwibank · BNZ · ANZ · Westpac · ASB

---

## Setup

### 1. Install dependencies

```bash
pip install requests beautifulsoup4 lxml rapidfuzz
```

### 2. Configure

```bash
cp config.example.json config.json
```

Open `config.json` and set:

| Field | Description |
|---|---|
| `discord_webhook` | Your Discord webhook URL |
| `keywords` | Job title keywords to match (fuzzy) |
| `location` | Location filter, e.g. `"Auckland"` or `"New Zealand"` |
| `interval_minutes` | How often to scan (default: 30) |
| `fuzzy_threshold` | Match sensitivity 0–100 (default: 80) |
| `sites` | Set any site to `false` to skip it |

### 3. Run

**GUI (recommended):**
```bash
python gui.py
```

**Headless (one scan):**
```bash
python job_scraper.py --once
```

**Headless (continuous loop):**
```bash
python job_scraper.py
```

**Reset seen jobs** (re-announce everything on next scan):
```bash
python job_scraper.py --reset
```

---

## How it works

Each site uses a different scraping method depending on what the site exposes:

| Site | Method |
|---|---|
| Heartland, Westpac, BNZ | Workday RSS feed |
| Kiwibank | Cornerstone OnDemand API |
| ANZ | SuccessFactors HTML |
| MTF Finance | Static HTML |
| Avanti Finance | RSS feed |
| ASB | Direct careers site HTML |

All sites are scraped **concurrently** so a full scan typically completes in under 30 seconds.

---

## Files

| File | Description |
|---|---|
| `job_scraper.py` | Scraper logic |
| `gui.py` | Tkinter GUI |
| `config.example.json` | Config template — copy to `config.json` |
| `config.json` | **Your config — never commit this** |
| `jobs.json` | All current matched jobs (auto-generated) |
| `seen_jobs.json` | Tracks which jobs have been announced (auto-generated) |

> `config.json`, `jobs.json`, and `seen_jobs.json` are excluded from git via `.gitignore`.
