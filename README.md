# native-dev-summary
Test implementation an app like a `samari.news`.

https://eda-infoearth.github.io/native-dev-summary/

## dir structure
```sh
/
├── .github/
│   └── workflows/
│       └── update.yml      # GitHub Actions cron
├── scripts/
│   ├── backfill.py             # get RSS, samarize, diff (for past date)
│   └── fetch_and_summarize.py  # get RSS, samarize, diff
├── data/
│   ├── seen_ids.json       # store article ids
│   └── YYYY-MM-DD.json     # store articles
├── docs/ 
│   ├── index.html          # root of GitHub Pages
│   └── index.html
└── README.md
```

---

## app structure 
**for free:**
- GitHub Actions（cron, 2,000 min/month）
- GitHub Pages（static app hosting）
- Anthropic API（summarize, Claude Haiku, $0.0003/article）
- Slack Incoming Webhook（notification）

---

## setup

### 1. GitHub Secrets

This repositry
**Settings → Secrets and variables → Actions** 

| SECRET NAME | VALUE |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Console API key |
| `SLACK_WEBHOOK_URL` | Slack Incoming WebhookのURL |

#### Slack Webhook URL
1. https://api.slack.com/apps → Create New App
2. enable Incoming Webhooks
3. select channel, and copy Webhook URL

### 2. enable GitHub Pages

This repositry
**Settings → Pages**:
- Source: `Deploy from a branch`
- Branch: `main` / `docs`

### 4. enable Actions

#### at first
**Actions → Fetch & Summarize Framework News → Run workflow**

Launch app manually, after that it will fetch every 8:00AM(JST).

---

## test on local

```bash
# in root dir
pip install anthropic feedparser requests beautifulsoup4

export ANTHROPIC_API_KEY=sk-ant-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...

python3 scripts/fetch_and_summarize.py

python scripts/backfill.py YYYY-MM-DD
```

see `docs/index.html` on browser.

## other memo 

```sh
source .venv/bin/activate

pip freeze > requirements.txt

pip install -r requirements.txt

deactivate
```
