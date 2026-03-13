# Deployment Guide — JobScout v2.0

Follow these steps in order. Takes about 15 minutes total.

---

## Step 1 — Free Cloud PostgreSQL (Neon.tech)

1. Go to https://neon.tech and sign up (free)
2. Click **New Project** → name it `jobscout`
3. After creation, click **Connection Details**
4. Copy the connection string — it looks like:
   ```
   postgresql://pranav:abc123@ep-xxx.eu-west-1.aws.neon.tech/jobsdb?sslmode=require
   ```
5. Save this — you'll use it as `DATABASE_URL` in Steps 3 and 4

---

## Step 2 — Free Cloud Redis (Upstash)

1. Go to https://upstash.com and sign up (free)
2. Click **Create Database** → pick region `eu-west-1` (closest to Ireland)
3. After creation, go to **Details** tab
4. Copy the **Redis URL** — looks like:
   ```
   redis://default:abc123@eu1-xxx.upstash.io:6379
   ```
5. Save this — you'll use it as `REDIS_URL` in Steps 3 and 4

---

## Step 3 — GitHub Secrets

1. Go to: https://github.com/ghorpadeire/job-discovery-system
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** for each:

| Secret name          | Value                                      |
|----------------------|--------------------------------------------|
| `DATABASE_URL`       | Your Neon connection string (Step 1)       |
| `REDIS_URL`          | Your Upstash Redis URL (Step 2)            |
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram                |
| `TELEGRAM_CHAT_ID`   | From @userinfobot on Telegram              |
| `OPENAI_API_KEY`     | Your OpenAI key (or put `placeholder`)     |

---

## Step 4 — Render Deployment

1. Go to https://render.com and sign up (free)
2. Click **New** → **Blueprint**
3. Connect your GitHub repo: `ghorpadeire/job-discovery-system`
4. Render reads `render.yaml` automatically → click **Apply**
5. After deploy, go to your service → **Environment** tab
6. Add these environment variables (same values as GitHub secrets):

| Variable             | Value                          |
|----------------------|--------------------------------|
| `DATABASE_URL`       | Neon connection string         |
| `REDIS_URL`          | Upstash Redis URL              |
| `TELEGRAM_BOT_TOKEN` | Your bot token                 |
| `TELEGRAM_CHAT_ID`   | Your chat ID                   |
| `OPENAI_API_KEY`     | Your OpenAI key                |

7. After deploy completes, note your Render URL, e.g.:
   `https://job-web-xxxx.onrender.com`

---

## Step 5 — Register Telegram Webhook

Run this once after Render is live. Open a Python shell:

```python
from dashboard import register_webhook
register_webhook("https://job-web-xxxx.onrender.com")  # your actual URL
```

Or from the command line:
```bat
py -c "from dashboard import register_webhook; register_webhook('https://job-web-xxxx.onrender.com')"
```

---

## Step 6 — Trigger First Scrape

1. Go to your GitHub repo → **Actions** tab
2. Click **Scrape & Score Jobs** in the left panel
3. Click **Run workflow** → **Run workflow** (green button)
4. Wait ~5 minutes for it to complete
5. Then click **Telegram Notifications** → **Run workflow** → mode: `digest`
6. Check your Telegram — you should receive a job digest message

---

## Step 7 — Verify Everything Works

Send these commands to your Telegram bot:

| Command   | Expected response                          |
|-----------|--------------------------------------------|
| `/status` | Stats: active jobs, avg score, ghosts etc  |
| `/top10`  | Top 10 highest-scoring job listings        |
| `/help`   | List of all commands                       |

Also open your Render URL in a browser — the dashboard should show jobs.

---

## Full Production Schedule

| Time (Irish) | What happens                                      |
|--------------|---------------------------------------------------|
| 08:00        | GitHub scrapes IrishJobs + Indeed → saves to Neon |
| 09:05        | GitHub sends daily digest to Telegram             |
| Every 30 min | GitHub checks for new unalerted jobs → sends      |
| 20:00        | GitHub scrapes again (evening run)                |
| Any time     | You send /status → Render webhook → replies       |

---

## Troubleshooting

**Telegram bot not responding:**
- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in Render env vars
- Re-run the webhook registration (Step 5)
- Check Render logs: dashboard → Logs tab

**GitHub Actions failing:**
- Check all 5 secrets are set correctly (Step 3)
- Check the Actions tab for error details
- Make sure your Neon DB is active (free tier pauses after inactivity)

**No jobs in DB:**
- Manually trigger the Scrape workflow (Step 6)
- Check if Neon DB is paused: go to neon.tech → resume project

**Render URL showing error:**
- Check DATABASE_URL is set in Render env vars
- Check Render logs for Python errors
- Make sure the Neon DB allows connections from Render's IP
  (Neon → Settings → IP Allow → set to `0.0.0.0/0` for all IPs)
