# Ichimoku crossover mailer (NSE, market cap > Rs 5,000 cr) - Vercel edition

Every weekday the scan emails you:
1. Daily chart (Conversion 18 / Base 52) crossovers - today
2. Daily chart crossovers - last 5 sessions
3. Weekly chart (Conversion 9 / Base 26) crossovers - today
4. Weekly chart crossovers - last 5 sessions
5. Stocks from the previous 10 mailers that dropped out, with first-included and last-seen dates

## Project layout
```
api/run.py          Vercel serverless endpoint (/api/run), called by Vercel Cron
ichimoku/core.py    Scanner, Ichimoku maths, email, storage
vercel.json         Cron schedule + 300 s function time limit
requirements.txt    Python packages Vercel installs
index.html          Simple landing page
local_run.py        Run on your own computer (optional)
```

## Environment variables (Vercel > Project > Settings > Environment Variables)
| Name | Value |
|---|---|
| SMTP_USER | your Gmail address |
| SMTP_PASSWORD | 16-character Gmail App Password (not your normal password) |
| MAIL_TO | recipient(s), comma-separated |
| CRON_SECRET | any random string of 16+ characters |
| KV_REST_API_URL / KV_REST_API_TOKEN | added automatically when you connect Upstash Redis |

## Schedule
`vercel.json` uses `0 11 * * 1-5` (11:00 UTC, Mon-Fri).
- Hobby (free) plan: Vercel fires somewhere within that hour -> mail arrives 4:30-5:30 PM IST.
- Pro plan: change it to `30 10 * * 1-5` for exactly 4:00 PM IST, then redeploy.
Do not schedule earlier than 10:00 UTC: NSE closes at 3:30 PM IST.

## Manual runs (in a browser)
- Preview, no email: `https://YOUR-APP.vercel.app/api/run?key=CRON_SECRET&preview=1&force=1`
- Send now: `https://YOUR-APP.vercel.app/api/run?key=CRON_SECRET&force=1`

## Safety built in
- Endpoint rejects any call without CRON_SECRET.
- Sends at most one mail per day, even if Vercel's scheduler fires twice.
- A lock stops two scans running at the same time.
- Skips market holidays (no fresh data = no mail).
- If data download is slow, it stops in time and marks the mail as incomplete.
- If anything fails, you get a "FAILED" email with the error.

## Run locally (optional)
```
pip install -r requirements.txt
python local_run.py          # writes report.html
```
