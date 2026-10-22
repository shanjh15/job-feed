# job-feed

Daily Bengaluru software-job feed for a personal job tracker.

- **07:40 IST** — GitHub Actions runs `scrape.py`: [JobSpy](https://github.com/speedyapply/JobSpy) pulls fresh listings from LinkedIn, Indeed, Naukri and Google Jobs (no login). If the `APIFY_TOKEN` secret is set, Apify adds LinkedIn hiring posts ("mail your resume to…", Google Form links) and backs up Naukri when JobSpy gets blocked. Results land in `data/latest.json`.
- **08:40 IST** — Claude's scheduled run reads `data/latest.json`, filters and scores each role and adds the good ones to a private tracker.

## Settings
Edit `config.json` to change search terms, how many results each site returns, and the Apify monthly budget (default $4.50, inside Apify's free $5). Spend is tracked in `data/apify_usage.json`; Apify steps skip themselves once the month's budget is used.

## Manual refresh
Actions tab → **Daily job feed** → **Run workflow**.
