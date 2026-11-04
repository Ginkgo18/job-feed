# job-feed

Checks public Greenhouse, Lever, Ashby and Workday job boards on a schedule and saves new jobs that match the filters: Greenhouse, Lever and Ashby hourly, Workday twice a day.
No AI and no personal data. Results are in `feed/new_jobs.json` and `feed/new_jobs_workday.json`, with run summaries in `feed/status.json` and `feed/status_workday.json`.

Edit `config/filters.json` to change titles and locations, or `config/companies.json` to add companies.

The starting company list combines board names from [rishilahoti/ashby-job-scraper](https://github.com/rishilahoti/ashby-job-scraper) , [blakegrudzien/find-jobs](https://github.com/blakegrudzien/find-jobs) and [Feashliaa/job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) (MIT).
