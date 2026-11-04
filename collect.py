#!/usr/bin/env python3
"""Collects new matching jobs from public Greenhouse, Lever and Ashby job boards. No AI, no personal data."""
import concurrent.futures as cf, json, re, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FILTERS = json.loads((ROOT / "config/filters.json").read_text())
COMPANIES = json.loads((ROOT / "config/companies.json").read_text())
SEEN_FILE, FEED_FILE, STATUS_FILE = (ROOT / "feed" / n for n in ("seen.json", "new_jobs.json", "status.json"))
NOW = datetime.now(timezone.utc)
KEEP_DAYS, FIRST_SCAN_DAYS, MAX_AGE_DAYS = 7, 3, 14

def iso(d): return d.strftime("%Y-%m-%dT%H:%M:%SZ")

def parse(value):
    if value in (None, ""): return None
    try:
        if isinstance(value, (int, float)): return datetime.fromtimestamp(value / 1000, timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OSError):
        return None

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-feed/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def job(source, slug, jid, title, company, location, url, posted, date_kind, remote=False):
    d = parse(posted)
    return {"key": f"{source}:{slug}:{jid}", "source": source, "board": slug, "title": (title or "").strip(),
            "company": company or slug, "location": (location or "").strip(), "url": url, "remote_flag": bool(remote),
            "posted_at": iso(d) if d else None, "date_kind": date_kind}

def greenhouse(slug):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [job("greenhouse", slug, j["id"], j.get("title"), j.get("company_name"), (j.get("location") or {}).get("name"),
                j.get("absolute_url"), j.get("first_published") or j.get("updated_at"),
                "first_published" if j.get("first_published") else "updated_at") for j in data.get("jobs", [])]

def lever(slug):
    out = []
    for j in get(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        c = j.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location")]
        out.append(job("lever", slug, j["id"], j.get("text"), None, "; ".join(x for x in locs if x), j.get("hostedUrl"),
                       j.get("createdAt"), "created_at", j.get("workplaceType") == "remote"))
    return out

def ashby(slug):
    out = []
    for j in get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
        if j.get("isListed") is False: continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        out.append(job("ashby", slug, j.get("id"), j.get("title"), None, "; ".join(x for x in locs if x), j.get("jobUrl"),
                       j.get("publishedAt"), "published_at", j.get("isRemote")))
    return out

FETCH = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}

def has(text, terms):
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t.strip()) + r"(?![a-z0-9])", text) for t in terms)

def title_match(title):
    t = f" {title.lower()} "
    return has(t, FILTERS["title_include"]) and not has(t, FILTERS["title_exclude"])

def area(j):
    loc = j["location"].lower()
    if has(loc, FILTERS["location_local"]): return "New York area"
    if (j["remote_flag"] or has(loc, FILTERS["location_remote"])) and not has(loc, FILTERS["remote_exclude"]): return "Remote"
    return None

def scan(item):
    source, slug = item
    try:
        return source, slug, FETCH[source](slug), None
    except urllib.error.HTTPError as e:
        return source, slug, [], f"HTTP {e.code}"
    except Exception as e:
        return source, slug, [], type(e).__name__

def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default

def main():
    start = time.time()
    seen = load(SEEN_FILE, {"boards": [], "jobs": {}})
    feed = load(FEED_FILE, {"jobs": []})
    known_boards = set(seen["boards"])
    items = [(s, b) for s, boards in COMPANIES.items() for b in boards]
    scanned, failed, total, matched, added = {k: 0 for k in FETCH}, {}, 0, 0, []
    with cf.ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(scan, items))
    for source, slug, jobs, err in results:
        board = f"{source}:{slug}"
        if err:
            failed[err] = failed.get(err, 0) + 1
            continue
        scanned[source] += 1
        total += len(jobs)
        first_scan = board not in known_boards
        known_boards.add(board)
        for j in jobs:
            if not title_match(j["title"]): continue
            a = area(j)
            if not a: continue
            matched += 1
            if j["key"] in seen["jobs"]: continue
            seen["jobs"][j["key"]] = iso(NOW)
            posted = parse(j["posted_at"])
            if posted and NOW - posted > timedelta(days=MAX_AGE_DAYS): continue
            if first_scan and not (posted and NOW - posted <= timedelta(days=FIRST_SCAN_DAYS)): continue
            added.append({**j, "area": a, "first_seen": iso(NOW), "first_scan": first_scan})
    cutoff = NOW - timedelta(days=KEEP_DAYS)
    jobs = [j for j in feed["jobs"] if parse(j["first_seen"]) >= cutoff] + added
    jobs.sort(key=lambda j: (j["first_seen"], j["posted_at"] or ""), reverse=True)
    seen["boards"] = sorted(known_boards)
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))
    FEED_FILE.write_text(json.dumps({"jobs": jobs}, indent=1))
    STATUS_FILE.write_text(json.dumps({"last_run": iso(NOW), "seconds": round(time.time() - start, 1), "boards_listed": len(items),
        "boards_scanned": scanned, "boards_failed": failed, "jobs_read": total, "jobs_matching_filters": matched,
        "new_this_run": len(added), "in_feed": len(jobs)}, indent=1))
    print(STATUS_FILE.read_text())

if __name__ == "__main__":
    main()
