#!/usr/bin/env python3
"""Daily Bengaluru job feed for a personal job tracker.

1. JobSpy (free) scrapes LinkedIn, Indeed, Naukri and Google Jobs, no login.
2. Apify (optional, needs the APIFY_TOKEN secret) adds LinkedIn hiring posts
   ("mail your resume to..." / Google Form posts) and backs up Naukri when
   JobSpy gets blocked. A monthly budget guard keeps it inside the free $5.
3. Everything is normalised into data/latest.json (+ a dated copy). Claude's
   8:40 AM run reads that file, filters, scores and tailors resumes.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import traceback

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TODAY = dt.datetime.now(IST).date().isoformat()
MONTH = TODAY[:7]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FORM_RE = re.compile(r"https?://(?:forms\.gle/[\w-]+|docs\.google\.com/forms/[^\s)\]>\"']+|forms\.office\.com/[^\s)\]>\"']+)")
YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:yrs?|years?)|(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)", re.I)

meta = {"date": TODAY, "generated_at": dt.datetime.now(IST).isoformat(timespec="minutes"),
        "counts": {}, "errors": [], "apify_spend_usd": 0.0}


def clean(s, limit=None):
    if s is None:
        return None
    try:
        import math
        if isinstance(s, float) and math.isnan(s):
            return None
    except Exception:
        pass
    s = str(s).strip()
    return s[:limit] if limit else s


def min_years(text):
    """Smallest experience number mentioned, e.g. '1-3 Yrs' -> 1. None if no number."""
    if not text:
        return None
    vals = []
    for m in YEARS_RE.finditer(text):
        if m.group(1):
            vals.append(float(m.group(1)))
        elif m.group(3):
            vals.append(float(m.group(3)))
    return min(vals) if vals else None


NOT_APPLY = re.compile(r"accom+odation|disabilit|privacy|noreply|no-reply|fraud|unsubscribe", re.I)


def links(text):
    text = text or ""
    emails = [e.rstrip(".") for e in EMAIL_RE.findall(text) if not NOT_APPLY.search(e)]
    return sorted(set(emails)), sorted(set(FORM_RE.findall(text)))


DROP_TITLE = re.compile(CFG["prefilter"]["drop_title_regex"], re.I)
KEEP_LOC = re.compile(CFG["prefilter"]["keep_location_regex"], re.I)


def keep_location(loc):
    return not loc or bool(KEEP_LOC.search(loc))


def keep_title(title):
    return not DROP_TITLE.search(title or "")


# ---------------------------------------------------------------- JobSpy
def run_jobspy():
    from jobspy import scrape_jobs
    js = CFG["jobspy"]
    out = []
    for site in js["sites"]:
        n = 0
        for term in js["search_terms"]:
            try:
                kwargs = dict(site_name=[site], search_term=term, location=CFG["location"],
                              results_wanted=js["results_per_site"], hours_old=CFG["hours_old"],
                              country_indeed="india", description_format="markdown", verbose=0)
                if site == "linkedin":
                    kwargs["linkedin_fetch_description"] = js.get("linkedin_fetch_description", False)
                if site == "google":
                    kwargs["google_search_term"] = f"{term} jobs in Bengaluru since yesterday"
                if site == "naukri":
                    kwargs["location"] = js.get("location_naukri", CFG["location"])
                if js.get("fetch_description"):
                    kwargs["fetch_description"] = True
                df = scrape_jobs(**kwargs)
            except Exception as ex:
                meta["errors"].append(f"jobspy/{site}/{term}: {type(ex).__name__}: {str(ex)[:160]}")
                if site == "linkedin" and "429" in str(ex):
                    break  # rate-limited: stop hammering LinkedIn today
                continue
            for row in df.to_dict("records"):
                desc = clean(row.get("description"), 6000) or ""
                emails, forms = links(desc)
                exp_text = clean(row.get("experience_range"))
                out.append({
                    "kind": "listing", "source": f"jobspy:{site}", "site": site,
                    "title": clean(row.get("title")), "company": clean(row.get("company")),
                    "location": clean(row.get("location")),
                    "posted": str(row.get("date_posted"))[:10] if row.get("date_posted") is not None else None,
                    "url": clean(row.get("job_url")), "apply_url": clean(row.get("job_url_direct")),
                    "experience_text": exp_text,
                    "min_years": min_years(exp_text) if exp_text else min_years(desc[:3000]),
                    "job_level": clean(row.get("job_level")),
                    "emails": emails or [e.strip() for e in (clean(row.get("emails")) or "").split(",") if "@" in e],
                    "forms": forms, "description": desc, "search_term": term,
                })
                n += 1
            time.sleep(2)
        meta["counts"][f"jobspy:{site}"] = n
    return out


# ---------------------------------------------------------------- Apify
def usage_path():
    return os.path.join(DATA, "apify_usage.json")


def month_spend():
    try:
        u = json.load(open(usage_path()))
        return float(u.get(MONTH, 0.0))
    except Exception:
        return 0.0


def add_spend(amount):
    try:
        u = json.load(open(usage_path()))
    except Exception:
        u = {}
    u[MONTH] = round(float(u.get(MONTH, 0.0)) + amount, 4)
    json.dump(u, open(usage_path(), "w"), indent=1)
    meta["apify_spend_usd"] = round(meta["apify_spend_usd"] + amount, 4)


def apify_run(actor, payload, timeout=300):
    token = os.environ.get("APIFY_TOKEN")
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": token, "timeout": timeout}, json=payload, timeout=timeout + 30)
    r.raise_for_status()
    return r.json()


def run_apify(jobspy_rows):
    if not os.environ.get("APIFY_TOKEN"):
        meta["errors"].append("apify: APIFY_TOKEN secret not set, skipped")
        return []
    ap = CFG["apify"]
    budget = ap["monthly_budget_usd"]
    out = []

    lp = ap["linkedin_posts"]
    est = lp["posts_per_query"] * len(lp["queries"]) * lp["cost_per_post_usd"]
    if lp.get("enabled") and month_spend() + est <= budget:
        try:
            items = apify_run(lp["actor"], {"searchQueries": lp["queries"], "maxPosts": lp["posts_per_query"],
                                            "postedAfter": lp["posted_after"]})
            add_spend(len(items) * lp["cost_per_post_usd"])
            for p in items:
                text = clean(p.get("text"), 6000) or ""
                emails, forms = links(text)
                job = p.get("job") or {}
                first = next((l.strip() for l in text.splitlines() if l.strip()), "")[:140]
                out.append({
                    "kind": "post", "source": "apify:linkedin-post", "site": "linkedin",
                    "title": clean(job.get("title")) or first, "company": clean(job.get("company")) or clean(p.get("author")),
                    "location": clean(job.get("location")), "posted": clean(p.get("postedAt")),
                    "url": clean(p.get("postUrl") or p.get("shareUrl")), "apply_url": clean(job.get("applyUrl")),
                    "author": clean(p.get("author")), "author_url": clean(p.get("profileUrl")),
                    "experience_text": None, "min_years": min_years(text),
                    "emails": emails, "forms": forms, "description": text,
                })
            meta["counts"]["apify:linkedin-post"] = len(items)
        except Exception as ex:
            meta["errors"].append(f"apify/linkedin-post: {type(ex).__name__}: {str(ex)[:200]}")
    elif lp.get("enabled"):
        meta["errors"].append("apify/linkedin-post: skipped, monthly budget reached")

    nb = ap["naukri_backup"]
    naukri_n = meta["counts"].get("jobspy:naukri", 0)
    est = nb["max_results"] * nb["cost_per_job_usd"]
    if nb.get("enabled") and naukri_n < nb["run_if_jobspy_naukri_below"]:
        if month_spend() + est <= budget:
            try:
                items = apify_run(nb["actor"], {
                    "searches": [{"keyword": k, "location": nb["location"]} for k in nb["keywords"]],
                    "maxResults": nb["max_results"], "freshnessDays": 2,
                    "experienceMin": nb["experience_min"], "experienceMax": nb["experience_max"],
                    "sortBy": "date", "responseFormat": "detailed"})
                add_spend(len(items) * nb["cost_per_job_usd"])
                for j in items:
                    desc = clean(j.get("description"), 6000) or ""
                    emails, forms = links(desc)
                    out.append({
                        "kind": "listing", "source": "apify:naukri", "site": "naukri",
                        "title": clean(j.get("title")), "company": clean(j.get("companyName")),
                        "location": ", ".join(j.get("locations") or []) if isinstance(j.get("locations"), list) else clean(j.get("locations")),
                        "posted": clean(j.get("postedAt")), "url": clean(j.get("jobUrl")), "apply_url": None,
                        "experience_text": clean(j.get("experienceText")),
                        "min_years": j.get("experienceMin") if isinstance(j.get("experienceMin"), (int, float)) else min_years(clean(j.get("experienceText"))),
                        "emails": emails, "forms": forms, "description": desc,
                        "skills": j.get("skills"),
                    })
                meta["counts"]["apify:naukri"] = len(items)
            except Exception as ex:
                meta["errors"].append(f"apify/naukri: {type(ex).__name__}: {str(ex)[:200]}")
        else:
            meta["errors"].append("apify/naukri: skipped, monthly budget reached")
    return out


# ---------------------------------------------------------------- main
def main():
    os.makedirs(DATA, exist_ok=True)
    rows = []
    try:
        rows += run_jobspy()
    except Exception:
        meta["errors"].append("jobspy crashed: " + traceback.format_exc()[-400:])
    try:
        rows += run_apify(rows)
    except Exception:
        meta["errors"].append("apify crashed: " + traceback.format_exc()[-400:])

    seen, jobs = set(), []
    maxmin = CFG["prefilter"]["max_min_years"]
    for r in rows:
        if not r.get("url") or not r.get("title"):
            continue
        if r["kind"] == "listing" and not keep_title(r["title"]):
            continue
        if r["kind"] == "listing" and not keep_location(r.get("location")):
            continue
        if r.get("min_years") is not None and r["min_years"] > maxmin:
            continue
        key = (r.get("url"),)
        alt = ((r.get("company") or "").lower().strip(), (r.get("title") or "").lower().strip())
        if key in seen or (r["kind"] == "listing" and alt in seen):
            continue
        seen.add(key)
        seen.add(alt)
        jobs.append(r)
    meta["total_after_prefilter"] = len(jobs)
    meta["apify_month_spend_usd"] = month_spend()

    payload = {"meta": meta, "jobs": jobs}
    json.dump(payload, open(os.path.join(DATA, "latest.json"), "w"), indent=1, ensure_ascii=False, default=str)
    json.dump(payload, open(os.path.join(DATA, f"{TODAY}.json"), "w"), indent=1, ensure_ascii=False, default=str)
    # keep two weeks of dated files
    cutoff = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=14)).isoformat()
    for f in os.listdir(DATA):
        if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f) and f[:10] < cutoff:
            os.remove(os.path.join(DATA, f))
    print(json.dumps(meta, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
