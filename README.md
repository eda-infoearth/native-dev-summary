# native-dev-summary
Test implementation an app like a `samari.news`.

- GitHub Pages: https://eda-infoearth.github.io/native-dev-summary/ 
- RSS feed: https://eda-infoearth.github.io/native-dev-summary/feed.xml 

---

## targets

### Done

- Typescript
  - Expo
  - React Native
  - Electron
- Dart
  - Flutter
- Python
  - Flet
- Rust
  - Tauri
  - Dioxus
  - Crux

### Planed

- OS update
- Application development guideline
- Application store guideline

---

## dir structure
```sh
$ tree --dirsfirst
.
├── .github
│   └── workflows/
│       └── update.yml      # GitHub Actions cron
├── data
│   ├── YYYY-MM-DD.json     # store articles
│   ├── seen_ids.json       # store article ids
│   └── tauri_versions.json # store Tauri versions from 
├── docs
│   ├── feed.xml            # RSS feed file
│   └── index.html          # root of GitHub Pages
├── scripts
│   ├── backfill.py            # get RSS, samarize, diff (for past date)
│   └── fetch_and_summarize.py # get RSS, samarize, diff (main)
├── README.md
└── requirements.txt
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
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |
| `SITE_URL` | GitHub Pages URL |

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

# launch virtual env and install libs
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...

# run main script
python3 scripts/fetch_and_summarize.py
# run backfill script to read past data
python scripts/backfill.py YYYY-MM-DD

# kill virtual env
deactivate
```

see `docs/index.html` on browser.

## other memo 

```sh
# write installed libs
pip freeze > requirements.txt
```
