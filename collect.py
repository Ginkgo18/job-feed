#!/usr/bin/env python3
"""Collects new matching jobs from public Greenhouse, Lever and Ashby job boards. No AI, no personal data."""
import concurrent.futures as cf, html, json, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FILTERS = json.loads((ROOT / "config/filters.json").read_text())
COMPANIES = json.loads((ROOT / "config/companies.json").read_text())
MODE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] == "workday" else "fast"
SUFFIX = "_workday" if MODE == "workday" else ""
SEEN_FILE, FEED_FILE, STATUS_FILE = (ROOT / "feed" / f"{n}{SUFFIX}.json" for n in ("seen", "new_jobs", "status"))
NOW = datetime.now(timezone.utc)
KEEP_DAYS, FIRST_SCAN_DAYS, MAX_AGE_DAYS = 7, 3, 14
DEADLINE = time.time() + 75 * 60
DEAD_AFTER = 3

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

def workday_date(label):
    t = (label or "").lower()
    if "today" in t: return NOW
    if "yesterday" in t: return NOW - timedelta(days=1)
    if "30+" in t: return NOW - timedelta(days=45)
    m = re.search(r"(\d+)\s+day", t)
    return NOW - timedelta(days=int(m.group(1))) if m else None

def post_json(url, body, origin):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "User-Agent": "job-feed/1.0", "Accept": "application/json", "Content-Type": "application/json", "Origin": origin})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def workday(slug):
    tenant, dc, site = slug.split("|")
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    found = {}
    for query in FILTERS["workday_searches"]:
        data = post_json(api + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query}, base)
        for p in data.get("jobPostings", []):
            if p.get("externalPath"): found[p["externalPath"]] = p
        if not data.get("total"): break
    out = []
    for path, p in found.items():
        title, loc, posted = p.get("title") or "", p.get("locationsText") or "", workday_date(p.get("postedOn"))
        key = f"workday:{slug}:{path}"
        kind, company = "workday_label", tenant
        hidden = bool(re.fullmatch(r"\d+ locations?", loc.lower()))
        if title_match(title) and key not in SEEN_KEYS and posted and NOW - posted <= timedelta(days=7) and (hidden or area({"location": loc, "remote_flag": False})):
            try:
                info = get(api + path).get("jobPostingInfo", {})
                extra = info.get("additionalLocations") or []
                loc = "; ".join(x for x in [info.get("location")] + extra if x) or loc
                if info.get("remoteType"): loc += f"; {info['remoteType']}"
                if info.get("startDate"): posted, kind = parse(info["startDate"]), "start_date"
            except Exception:
                pass
        out.append(job("workday", slug, path, title, company, loc, f"{base}/{site}{path}", iso(posted) if posted else None, kind))
    return out

def clean(text):
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()[:7000]

def description(j):
    """Reads the public job text so screening does not depend on opening the page."""
    source, slug, jid = j["key"].split(":", 2)
    try:
        if source == "greenhouse":
            return clean(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}").get("content"))
        if source == "lever":
            p = get(f"https://api.lever.co/v0/postings/{slug}/{jid}?mode=json")
            parts = [p.get("descriptionPlain"), p.get("additionalPlain")]
            for l in p.get("lists") or []:
                parts += [l.get("text"), l.get("content")]
            return clean("\n".join(x for x in parts if x))
        if source == "ashby":
            for p in get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
                if p.get("id") == jid:
                    return clean(p.get("descriptionPlain") or p.get("descriptionHtml"))
            return None
        if source == "workday":
            tenant, dc, site = slug.split("|")
            return clean(get(f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{jid}").get("jobPostingInfo", {}).get("jobDescription"))
    except Exception:
        return None

FETCH = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}
SEEN_KEYS = set()

def has(text, terms):
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t.strip()) + r"(?![a-z0-9])", text) for t in terms)

def title_match(title):
    t = f" {title.lower()} "
    return has(t, FILTERS["title_include"]) and not has(t, FILTERS["title_exclude"])

def area(j):
    loc = j["location"].lower()
    if has(loc, FILTERS["location_local"]) and (has(loc, FILTERS["location_nyc_sure"]) or not has(loc, FILTERS["location_exclude"])):
        return "New York area"
    us_or_blank = not loc or has(loc, ["remote", "us", "usa", "united states"])
    if ((j["remote_flag"] and us_or_blank) or has(loc, FILTERS["location_remote"])) and not has(loc, FILTERS["remote_exclude"]): return "Remote"
    return None

def scan(item):
    source, slug = item
    if source == "workday" and time.time() > DEADLINE:
        return source, slug, [], "skipped"
    try:
        return source, slug, FETCH[source](slug), None
    except urllib.error.HTTPError as e:
        if e.code == 429 and source == "workday":
            time.sleep(5)  # Workday asked us to slow down
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
    dead = seen.get("dead", {})
    SEEN_KEYS.update(seen["jobs"])
    live = lambda s, b: dead.get(f"{s}:{b}", 0) < DEAD_AFTER
    wanted = ["workday"] if MODE == "workday" else ["greenhouse", "lever", "ashby"]
    items = [(s, b) for s in wanted for b in COMPANIES.get(s, []) if live(s, b)]
    cursor = seen.get("cursor", 0) % max(len(items), 1)
    items = items[cursor:] + items[:cursor]
    scanned, failed, total, matched, added = {k: 0 for k in wanted}, {}, 0, 0, []
    with cf.ThreadPoolExecutor(max_workers=6 if MODE == "workday" else 48) as pool:
        results = list(pool.map(scan, items))
    done = sum(1 for r in results if r[3] != "skipped")
    seen["cursor"] = (cursor + done) % max(len(items), 1)
    for source, slug, jobs, err in results:
        board = f"{source}:{slug}"
        if err == "skipped":
            continue
        if err:
            failed[err] = failed.get(err, 0) + 1
            if err in ("HTTP 404", "HTTP 401", "HTTP 403"): dead[board] = dead.get(board, 0) + 1
            continue
        dead.pop(board, None)
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
    jobs = [j for j in feed["jobs"] if parse(j["first_seen"]) >= cutoff and area(j)] + added
    missing = [j for j in jobs if not j.get("description")]
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for j, text in zip(missing, pool.map(description, missing)):
            if text: j["description"] = text
    jobs.sort(key=lambda j: (j["first_seen"], j["posted_at"] or ""), reverse=True)
    seen["boards"] = sorted(known_boards)
    seen["dead"] = dead
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))
    FEED_FILE.write_text(json.dumps({"jobs": jobs}, indent=1))
    STATUS_FILE.write_text(json.dumps({"last_run": iso(NOW), "seconds": round(time.time() - start, 1), "mode": MODE, "boards_listed": sum(len(COMPANIES.get(k, [])) for k in wanted), "boards_dead_skipped": sum(1 for v in dead.values() if v >= DEAD_AFTER), "boards_tried": done,
        "boards_scanned": scanned, "boards_failed": failed, "jobs_read": total, "jobs_matching_filters": matched,
        "new_this_run": len(added), "with_description": sum(1 for j in jobs if j.get("description")), "in_feed": len(jobs)}, indent=1))
    print(STATUS_FILE.read_text())

if __name__ == "__main__":
    main()
