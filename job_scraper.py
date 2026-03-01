import requests
from bs4 import BeautifulSoup
import json
import time
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from rapidfuzz import fuzz

# Force UTF-8 output on Windows to avoid charmap encoding errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ==============================
# LOAD CONFIG
# ==============================
with open("config.json", "r") as f:
    config = json.load(f)

KEYWORDS        = [k.lower() for k in config["keywords"]]
DISCORD_WEBHOOK = config.get("discord_webhook", "")
INTERVAL        = config.get("interval_minutes", 30) * 60
FUZZY_THRESHOLD = config.get("fuzzy_threshold", 80)
ENABLED_SITES   = config.get("sites", {})
LOCATION        = config.get("location", "Auckland")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-NZ,en;q=0.9",
}

SEEN_FILE = "seen_jobs.json"

# Discord hard limits
_DISCORD_DESC_LIMIT  = 4000
_DISCORD_EMBED_BATCH = 10


# ==============================
# UTILITIES
# ==============================

def fuzzy_match(text: str) -> bool:
    text = text.lower()
    return any(fuzz.partial_ratio(kw, text) >= FUZZY_THRESHOLD for kw in KEYWORDS)


def is_target_location(location: str) -> bool:
    loc = location.lower().strip()
    if not loc or loc in ("n/a", "new zealand", "nz", ""):
        return True
    return LOCATION.lower().strip() in loc


def safe_get(url: str, retries: int = 3, timeout: int = 25,
             extra_headers: dict = None, session: requests.Session = None):
    hdrs      = {**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*",
                 **(extra_headers or {})}
    requester = session or requests
    for attempt in range(retries):
        try:
            r = requester.get(url, headers=hdrs, timeout=timeout)
            r.raise_for_status()
            if not r.text.strip():
                print(f"    [WARNING] Empty response from {url}")
                return None
            return r
        except requests.exceptions.HTTPError as e:
            print(f"    [HTTP ERROR] {url} — {e}")
            break
        except requests.exceptions.ConnectionError:
            print(f"    [CONNECTION ERROR] {url} (attempt {attempt+1}/{retries})")
        except requests.exceptions.Timeout:
            print(f"    [TIMEOUT] {url} (attempt {attempt+1}/{retries})")
        except requests.exceptions.RequestException as e:
            print(f"    [REQUEST ERROR] {url} — {e}")
            break
        time.sleep(2 ** attempt)
    return None


# ==============================
# DISCORD
# ==============================

def _build_embeds(grouped: dict) -> list:
    embeds = []
    for site, jobs in grouped.items():
        lines  = [f"• [{j['title']}]({j['link']}) — {j['location']}" for j in jobs]
        chunk, length, part = [], 0, 1
        for line in lines:
            if length + len(line) + 1 > _DISCORD_DESC_LIMIT and chunk:
                title = site if part == 1 else f"{site} (cont.)"
                embeds.append({"title": title, "description": "\n".join(chunk),
                               "color": 0x1D82B6})
                chunk, length, part = [], 0, part + 1
            chunk.append(line)
            length += len(line) + 1
        if chunk:
            title = site if part == 1 else f"{site} (cont.)"
            embeds.append({"title": title, "description": "\n".join(chunk),
                           "color": 0x1D82B6})
    return embeds


def send_to_discord_grouped(new_jobs: list):
    if not DISCORD_WEBHOOK or not new_jobs:
        return
    grouped: dict = {}
    for job in new_jobs:
        grouped.setdefault(job["site"], []).append(job)
    all_embeds = _build_embeds(grouped)
    for i in range(0, len(all_embeds), _DISCORD_EMBED_BATCH):
        batch = all_embeds[i : i + _DISCORD_EMBED_BATCH]
        try:
            r = requests.post(DISCORD_WEBHOOK, json={"embeds": batch}, timeout=15)
            r.raise_for_status()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            sites = ", ".join({e_["title"].split(" (")[0] for e_ in batch})
            print(f"    [DISCORD ERROR] {sites}: {e}")
        time.sleep(0.5)


# ==============================
# SEEN JOBS PERSISTENCE
# ==============================

def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, ValueError):
        print(f"  [WARNING] {SEEN_FILE} corrupted — starting fresh.")
        return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def _parse_xml_safe(text: str):
    try:
        return BeautifulSoup(text, "xml")
    except Exception:
        return BeautifulSoup(text, "html.parser")


# ==============================
# WORKDAY HELPER  (Heartland, Westpac, BNZ)
#
# PRIMARY:  RSS feed — plain GET, no CSRF, no session tricks needed.
#           Workday exposes RSS at /en-US/{site_path}/jobs/rss for every tenant.
#
# FALLBACK: CXS JSON API — requires a CSRF token from the careers page cookie.
#           Used only if RSS returns nothing (e.g. tenant has disabled RSS).
# ==============================

def _workday_rss(site_name: str, tenant: str, site_path: str,
                 base_url: str, wd_subdomain: str) -> list:
    """Fetch jobs via Workday's public RSS feed (no auth required)."""
    jobs    = []
    rss_url = (f"https://{tenant}.{wd_subdomain}.myworkdayjobs.com"
               f"/en-US/{site_path}/jobs/rss")
    r = safe_get(rss_url, extra_headers={"Accept": "application/rss+xml,text/xml,*/*"})
    if r is None:
        return jobs

    soup  = _parse_xml_safe(r.text)
    items = soup.find_all("item")
    print(f"    [RSS] {site_name}: {len(items)} item(s) in feed.")

    for item in items:
        title_tag = item.find("title")
        link_tag  = item.find("link")
        desc_tag  = item.find("description")
        title     = title_tag.get_text(strip=True) if title_tag else ""
        link      = link_tag.get_text(strip=True)  if link_tag  else ""
        desc      = desc_tag.get_text(strip=True)  if desc_tag  else ""

        if not title or not link:
            continue

        # Strip the tracking query string Workday adds to RSS links
        link = link.split("?")[0]

        # Location: Workday RSS doesn't always include a dedicated field.
        # We scan the description for known NZ city names.
        location = "New Zealand"
        for city in ["Auckland", "Wellington", "Christchurch", "Hamilton",
                     "Dunedin", "Tauranga", "Napier", "Palmerston North",
                     "Nelson", "Rotorua", "Whangārei", "Whangarei", "Queenstown"]:
            if city.lower() in desc.lower() or city.lower() in title.lower():
                location = city
                break

        if fuzzy_match(title) and is_target_location(location):
            jobs.append({"site": site_name, "title": title,
                         "location": location, "link": link})
    return jobs


def _workday_cxs(site_name: str, tenant: str, site_path: str,
                 base_url: str, wd_subdomain: str) -> list:
    """
    Fallback: fetch jobs via Workday's CXS JSON API.
    Requires a CSRF token — works when Workday sets it as a server-side cookie.
    """
    jobs    = []
    api_url = (f"https://{tenant}.{wd_subdomain}.myworkdayjobs.com"
               f"/wday/cxs/{tenant}/{site_path}/jobs")
    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    offset  = 0
    origin  = f"https://{tenant}.{wd_subdomain}.myworkdayjobs.com"

    session    = requests.Session()
    csrf_token = ""
    try:
        init_r = session.get(
            base_url, timeout=25,
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"})
        for cookie in session.cookies:
            if "csrf" in cookie.name.lower():
                csrf_token = cookie.value
                break
        if not csrf_token and init_r.ok:
            m = re.search(
                r'(?:wdCsrfToken|CALYPSO_CSRF_TOKEN)["\s:=\']+([A-Za-z0-9_\-]{20,})',
                init_r.text)
            if m:
                csrf_token = m.group(1)
    except Exception as e:
        print(f"    [CSRF WARNING] {site_name}: {e}")

    post_headers = {
        "Content-Type":     "application/json",
        "Accept":           "application/json",
        "X-Workday-Client": "2023.35.4",
        "Origin":           origin,
        "Referer":          base_url + "/",
        **({"X-Calypso-CSRF-Token": csrf_token} if csrf_token else {}),
    }

    while True:
        payload["offset"] = offset
        try:
            resp = session.post(
                api_url, json=payload, headers=post_headers, timeout=25)
            if not resp.ok:
                print(f"    [CXS {resp.status_code}] {site_name} — API rejected request")
                break
            data = resp.json()
        except Exception as e:
            print(f"    [CXS ERROR] {site_name}: {e}")
            break

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for p in postings:
            title    = p.get("title", "").strip()
            location = p.get("locationsText", "N/A")
            ext_path = p.get("externalPath", "")
            link     = base_url.rstrip("/") + ext_path if ext_path else base_url
            if fuzzy_match(title) and is_target_location(location):
                jobs.append({"site": site_name, "title": title,
                             "location": location, "link": link})

        total   = data.get("total", 0)
        offset += len(postings)
        if offset >= total:
            break
        time.sleep(1)

    return jobs


def scrape_workday(site_name: str, tenant: str, site_path: str,
                   base_url: str, wd_subdomain: str = "wd3") -> list:
    # Try RSS first — no auth, no CSRF, always works
    jobs = _workday_rss(site_name, tenant, site_path, base_url, wd_subdomain)
    if jobs:
        print(f"  {site_name} — {len(jobs)} matching job(s) found (RSS).")
        return jobs

    # Fallback to CXS JSON API
    print(f"    [INFO] {site_name}: RSS empty, trying CXS API…")
    jobs = _workday_cxs(site_name, tenant, site_path, base_url, wd_subdomain)
    print(f"  {site_name} — {len(jobs)} matching job(s) found (CXS).")
    return jobs


# ==============================
# CORNERSTONE ON DEMAND HELPER  (Kiwibank)
# ==============================

def scrape_csod(site_name: str, client_id: str,
                base_url: str, site_id: int = 1) -> list:
    jobs     = []
    page     = 1
    per_page = 20
    api_base = f"{base_url}/ATS/careersite/searchJobsRequest.do"

    while True:
        params = {
            "reqPageSize": per_page,
            "reqPageNum": page,
            "c":          client_id,
            "site":       site_id,
            "lang":       "en-US",
        }
        try:
            r = requests.get(
                api_base, params=params, timeout=20,
                headers={**HEADERS,
                         "Accept": "application/json, text/javascript, */*",
                         "Referer": (f"{base_url}/ux/ats/careersite"
                                     f"/{site_id}/home?c={client_id}")})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"    [CSOD ERROR] {site_name}: {e}")
            break

        items = data.get("Requisitions", [])
        if not items:
            break

        for item in items:
            title    = item.get("RequisitionTitle", "").strip()
            location = item.get("CityName", "") or item.get("StateName", "") or "New Zealand"
            req_id   = item.get("RequisitionId", "")
            link     = (f"{base_url}/ux/ats/careersite/{site_id}/requisition"
                        f"?requisitionId={req_id}&c={client_id}") if req_id else base_url
            if fuzzy_match(title) and is_target_location(location):
                jobs.append({"site": site_name, "title": title,
                             "location": location, "link": link})

        total   = data.get("TotalRecords", 0)
        fetched = page * per_page
        if fetched >= total:
            break
        page += 1
        time.sleep(1)

    print(f"  {site_name} — {len(jobs)} matching job(s) found.")
    return jobs


# ==============================
# LINKEDIN GUEST API HELPER  (last-resort fallback only)
# ==============================

def scrape_linkedin(site_name: str, company_name: str) -> list:
    """LinkedIn guest API — unreliable, use only when no direct site exists."""
    jobs     = []
    location = requests.utils.quote(f"{LOCATION}, New Zealand")
    keywords = requests.utils.quote(company_name)
    base_api = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={keywords}&location={location}&f_TPR=r2592000"
    )
    extra    = {
        "Accept":  "text/html,application/xhtml+xml",
        "Referer": "https://www.linkedin.com/jobs/search/",
    }
    start    = 0
    seen_ids = set()

    while True:
        r = safe_get(f"{base_api}&start={start}", extra_headers=extra)
        if r is None:
            break
        soup  = BeautifulSoup(r.text, "html.parser")
        cards = soup.find_all("li")
        if not cards:
            break

        found_any = False
        for card in cards:
            title_tag = card.find("h3", class_=re.compile("base-search-card__title"))
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            company_tag = card.find("h4", class_=re.compile("base-search-card__subtitle"))
            company_str = company_tag.get_text(strip=True).lower() if company_tag else ""
            if company_name.lower() not in company_str:
                continue

            loc_tag      = card.find("span", class_=re.compile("job-search-card__location"))
            location_str = loc_tag.get_text(strip=True) if loc_tag else LOCATION

            a_tag = card.find("a", href=True)
            link  = a_tag["href"].split("?")[0] if a_tag else ""
            if not link or link in seen_ids:
                continue
            seen_ids.add(link)
            found_any = True

            if fuzzy_match(title) and is_target_location(location_str):
                jobs.append({"site": site_name, "title": title,
                             "location": location_str, "link": link})

        if not found_any:
            break
        start += 25
        if start >= 100:
            break
        time.sleep(1.5)

    print(f"  {site_name} — {len(jobs)} matching job(s) found.")
    return jobs


# ==============================
# 1. HEARTLAND BANK  (Workday wd3)
# ==============================

def scrape_heartland() -> list:
    print("  Scraping Heartland Bank...")
    return scrape_workday(
        site_name="Heartland Bank",
        tenant="heartland",
        site_path="External",
        base_url="https://heartland.wd3.myworkdayjobs.com/External",
    )


# ==============================
# 2. MTF FINANCE  (static HTML)
# ==============================

def scrape_mtf() -> list:
    jobs = []
    url  = "https://www.mtf.co.nz/careers/"
    print("  Scraping MTF Finance...")
    r = safe_get(url)
    if r is None:
        print("  [SKIP] MTF Finance.")
        return jobs

    soup = BeautifulSoup(r.text, "html.parser")
    EXCLUDED = {
        "/careers/",
        "/careers/careers-at-mtf-finance-national-office/",
        "/careers/careers-at-an-mtf-finance-franchise/",
        "/careers/the-power-of-recognition-representation/",
    }
    job_pattern = re.compile(r"^/careers/[^/]+/$")

    for a in soup.find_all("a", href=job_pattern):
        href  = a["href"]
        if href in EXCLUDED:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        full_url = "https://www.mtf.co.nz" + href
        slug     = href.rstrip("/").split("/")[-1]
        location = "New Zealand"
        for city in ["dunedin", "auckland", "wellington", "christchurch",
                     "hamilton", "queenstown", "canterbury", "otago",
                     "hornby", "rolleston", "te-awamutu"]:
            if city in slug:
                location = city.replace("-", " ").title()
                break
        if fuzzy_match(title) and is_target_location(location):
            jobs.append({"site": "MTF Finance", "title": title,
                         "location": location, "link": full_url})

    print(f"  MTF Finance — {len(jobs)} matching job(s) found.")
    return jobs


# ==============================
# 3. AVANTI FINANCE  (RSS feed → HTML fallback)
# ==============================

def scrape_avanti() -> list:
    jobs     = []
    feed_url = "https://avantifinance.careercentre.net.nz/job/feed"
    print("  Scraping Avanti Finance...")
    r = safe_get(feed_url, extra_headers={"Accept": "application/rss+xml,text/xml,*/*"})

    if r and r.text.strip().startswith("<"):
        soup  = _parse_xml_safe(r.text)
        items = soup.find_all("item")
        for item in items:
            title_tag = item.find("title")
            link_tag  = item.find("link")
            loc_tag   = item.find("location") or item.find("region") or item.find("city")
            title     = title_tag.get_text(strip=True) if title_tag else ""
            link      = link_tag.get_text(strip=True)  if link_tag  else ""
            location  = loc_tag.get_text(strip=True)   if loc_tag   else "New Zealand"
            if title and fuzzy_match(title) and is_target_location(location):
                jobs.append({"site": "Avanti Finance", "title": title,
                             "location": location, "link": link})
        if items:
            print(f"  Avanti Finance — {len(jobs)} matching job(s) found.")
            return jobs

    # Fallback: HTML listing
    r2 = safe_get("https://avantifinance.careercentre.net.nz/job")
    if r2:
        soup2 = BeautifulSoup(r2.text, "html.parser")
        for a in soup2.find_all("a", href=re.compile(r"/job/\d+")):
            title = a.get_text(strip=True)
            if not title or not fuzzy_match(title):
                continue
            href = a["href"]
            if href.startswith("/"):
                href = "https://avantifinance.careercentre.net.nz" + href
            jobs.append({"site": "Avanti Finance", "title": title,
                         "location": "New Zealand", "link": href})
        jobs = list({j["link"]: j for j in jobs}.values())

    print(f"  Avanti Finance — {len(jobs)} matching job(s) found.")
    return jobs


# ==============================
# 4. KIWIBANK  (Cornerstone OnDemand)
# ==============================

def scrape_kiwibank() -> list:
    print("  Scraping Kiwibank...")
    return scrape_csod(
        site_name="Kiwibank",
        client_id="kiwibankpeople",
        base_url="https://kiwibankpeople.csod.com",
        site_id=1,
    )


# ==============================
# JOBTED NZ HELPER
#
# Jobted is a job aggregator that indexes NZ bank career sites in plain HTML.
# We use it specifically for Workday tenants whose CSRF is JavaScript-generated
# (Westpac wd105, BNZ on NAB wd3) — Python can never obtain the token without
# running a real browser, so direct Workday scraping is not feasible.
#
# URL pattern : https://www.jobted.co.nz/{slug}-jobs          (page 1)
#               https://www.jobted.co.nz/{slug}-jobs?pn={n}   (pages 2+)
# Job links   : https://www.jobted.co.nz?viewjob=HASH  (redirects to original)
# Location    : extracted from "Company Name – City" text in each card
# ==============================

def scrape_jobted(site_name: str, slug: str) -> list:
    """
    Scrape a company's listing page on jobted.co.nz.
    slug examples: 'westpac', 'bnz'
    """
    jobs     = []
    seen_ids = set()
    base     = "https://www.jobted.co.nz"

    for page in range(1, 15):
        url = f"{base}/{slug}-jobs" if page == 1 else f"{base}/{slug}-jobs?pn={page}"
        r = safe_get(url, extra_headers={"Accept": "text/html", "Referer": base + "/"})
        if r is None:
            break

        soup  = BeautifulSoup(r.text, "html.parser")

        # Each job is an <article> or a block containing an <h2><a> title link.
        # Jobted wraps each result in a <div class="job"> or similar; we find
        # all <h2> tags that contain a ?viewjob= link as a robust selector.
        job_headings = soup.find_all("h2")
        if not job_headings:
            break

        found_any = False
        for h2 in job_headings:
            a = h2.find("a", href=re.compile(r"\?viewjob="))
            if not a:
                continue

            title = a.get_text(strip=True)
            href  = a.get("href", "")
            if not title or not href:
                continue

            # Build absolute URL
            link = href if href.startswith("http") else base + href
            if link in seen_ids:
                continue
            seen_ids.add(link)
            found_any = True

            # Location: Jobted shows "Company Name – City" or "Company Name-City"
            # in a <span> or plain text near the heading. Walk up to the parent
            # container and scan all text nodes.
            location = "New Zealand"
            container = h2.find_parent() or h2
            full_text = container.get_text(separator=" ", strip=True)
            # Pattern: something like "Westpac New Zealand-Auckland" or
            # "Westpac New Zealand Limited – Wellington"
            loc_match = re.search(
                r'(?:Westpac|BNZ|Bank of New Zealand)[^–\-]*[–\-]\s*'
                r'(Auckland|Wellington|Christchurch|Hamilton|Dunedin'
                r'|Tauranga|Napier|Palmerston North|Nelson|Rotorua'
                r'|Whangarei|Queenstown|Lower Hutt|Porirua|New Plymouth'
                r'|Matamata|Britomart)',
                full_text, re.IGNORECASE)
            if loc_match:
                location = loc_match.group(1).title()

            if fuzzy_match(title) and is_target_location(location):
                jobs.append({"site": site_name, "title": title,
                             "location": location, "link": link})

        if not found_any:
            break
        time.sleep(1)

    jobs = list({j["link"]: j for j in jobs}.values())
    print(f"  {site_name} — {len(jobs)} matching job(s) found (Jobted).")
    return jobs


# ==============================
# 5. BNZ  (via Jobted — BNZ's Workday tenant uses JS-only CSRF)
# ==============================

def scrape_bnz() -> list:
    print("  Scraping BNZ...")
    return scrape_jobted(site_name="BNZ", slug="bnz")


# ==============================
# 6. ANZ  (SuccessFactors — static HTML, paginated)
# ==============================

def scrape_anz() -> list:
    jobs  = []
    base  = "https://careers.anz.com"
    pages = [
        f"{base}/go/ANZ-Jobs-List/4739210/",
        f"{base}/go/ANZ-Jobs-List/4739210/100/",
    ]
    SKIP_TITLES = {"title", "location", "date", "reset"}
    print("  Scraping ANZ...")

    for url in pages:
        r = safe_get(url)
        if r is None:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            a = cells[0].find("a", href=True)
            if not a:
                continue
            title    = a.get_text(strip=True)
            href     = a.get("href", "")
            location = cells[1].get_text(strip=True)
            if not title or title.lower() in SKIP_TITLES:
                continue
            link = (base + href) if href.startswith("/") else href
            if fuzzy_match(title) and is_target_location(location):
                jobs.append({"site": "ANZ", "title": title,
                             "location": location, "link": link})
        time.sleep(1)

    jobs = list({j["link"]: j for j in jobs}.values())
    print(f"  ANZ — {len(jobs)} matching job(s) found.")
    return jobs


# ==============================
# 7. WESTPAC  (via Jobted — wd105 CSRF is JS-generated, unfetchable by Python)
# ==============================

def scrape_westpac() -> list:
    print("  Scraping Westpac...")
    return scrape_jobted(site_name="Westpac", slug="westpac")


# ==============================
# 8. ASB  (careers.asbgroup.co.nz)
# ==============================

def scrape_asb() -> list:
    jobs     = []
    base     = "https://careers.asbgroup.co.nz"
    seen_ids = set()
    print("  Scraping ASB...")

    for page in range(1, 20):
        r = safe_get(f"{base}/search/page/{page}")
        if r is None:
            break

        soup      = BeautifulSoup(r.text, "html.parser")
        job_links = soup.find_all("a", href=re.compile(r"^/jobdetails/ajid/"))
        if not job_links:
            break

        found_any = False
        for a in job_links:
            href = a.get("href", "")
            if not href or href in seen_ids:
                continue
            seen_ids.add(href)
            found_any = True

            title = a.get_text(separator=" ", strip=True)
            if not title:
                title = (href.rstrip("/").split("/")[-1]
                         .replace("-", " ").replace(",", " "))

            location = "New Zealand"
            parent   = a.find_parent()
            if parent:
                loc_el = parent.find(string=re.compile(
                    r"Auckland|Wellington|Christchurch|Hamilton|Dunedin"
                    r"|Albany|Napier|Whangarei", re.IGNORECASE))
                if loc_el:
                    location = loc_el.strip()

            if fuzzy_match(title) and is_target_location(location):
                jobs.append({"site": "ASB", "title": title,
                             "location": location, "link": base + href})

        if not found_any:
            break
        time.sleep(1)

    jobs = list({j["link"]: j for j in jobs}.values())
    print(f"  ASB — {len(jobs)} matching job(s) found.")
    return jobs


# ==============================
# SITE REGISTRY
# ==============================

SCRAPERS = {
    "heartland": scrape_heartland,
    "mtf":       scrape_mtf,
    "avanti":    scrape_avanti,
    "kiwibank":  scrape_kiwibank,
    "bnz":       scrape_bnz,
    "anz":       scrape_anz,
    "westpac":   scrape_westpac,
    "asb":       scrape_asb,
}


# ==============================
# MAIN LOOP
# ==============================

def run_scraper():
    print(f"Running job scan... (location filter: {LOCATION})")
    active  = {k: fn for k, fn in SCRAPERS.items() if ENABLED_SITES.get(k, True)}
    skipped = set(SCRAPERS) - set(active)
    for k in skipped:
        print(f"  Skipping {k} (disabled in config).")

    all_jobs = []
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {pool.submit(fn): key for key, fn in active.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                all_jobs.extend(future.result())
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  [ERROR] {key}: {e}")

    unique_jobs = list({job["link"]: job for job in all_jobs}.values())

    seen     = load_seen()
    new_jobs = [job for job in unique_jobs if job["link"] not in seen]
    seen.update(job["link"] for job in new_jobs)

    save_seen(seen)

    with open("jobs.json", "w", encoding="utf-8") as f:
        json.dump(unique_jobs, f, indent=2, ensure_ascii=False)

    print(f"\nFound {len(new_jobs)} new job(s) in {LOCATION}.")
    for job in new_jobs:
        print(f"  -> [{job['site']}] {job['title']} — {job['location']}")
    send_to_discord_grouped(new_jobs)


def main():
    if "--reset" in sys.argv:
        if os.path.exists(SEEN_FILE):
            os.remove(SEEN_FILE)
            print(f"[RESET] Cleared {SEEN_FILE} — all jobs will be treated as new.")
        else:
            print("[RESET] No seen_jobs.json to clear.")

    if "--once" in sys.argv:
        try:
            run_scraper()
        except Exception as e:
            print(f"Error: {e}")
        return

    print(f"Auto-run job scraper started. Location filter: {LOCATION}")
    while True:
        try:
            run_scraper()
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
        print(f"\nSleeping for {INTERVAL // 60} minutes...\n")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()