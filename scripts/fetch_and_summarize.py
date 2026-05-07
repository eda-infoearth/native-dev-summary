#!/usr/bin/env python3
"""
fetch_and_summarize.py
- 各フレームワークのRSSフィードを5件/フレームワーク取得
- data/seen_ids.json で重複管理
- 新着を Claude Haiku で日本語要約
- data/yyyy-mm-dd.json に当日分を保存
- docs/index.html を1年以内の全JSONから日付スタック形式で生成
- 新着があれば Slack 通知

ファイル構成:
  data/
    seen_ids.json          # 既出記事URLのセット
    2025-05-07.json        # その日の新着記事リスト
    2025-05-06.json
    ...
  docs/
    index.html             # 全JSONを束ねた静的ページ

各日付JSONの構造:
[
  {
    "fw_id":    "flutter",
    "fw_name":  "Flutter",
    "fw_icon":  "🐦",
    "fw_color": "#54C5F8",
    "id":          "https://...",
    "title":       "...",
    "link":        "https://...",
    "pub_date":    "...",
    "description": "...",
    "summary_ja":  "...",
    "fetched_at":  "..."
  }, ...
]
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import feedparser
import requests

# ── 定数 ──────────────────────────────────────────────────────────────────────

FEEDS = [
    {"id": "flutter",      "name": "Flutter",      "icon": "🐦", "color": "#54C5F8", "url": "https://medium.com/feed/flutter"},
    {"id": "react-native", "name": "React Native", "icon": "⚛️", "color": "#61DAFB", "url": "https://reactnative.dev/blog/rss.xml"},
    {"id": "expo",         "name": "Expo",         "icon": "🚀", "color": "#aaaaff", "url": "https://expo.dev/blog/rss"},
    {"id": "electron",     "name": "Electron",     "icon": "⚡", "color": "#9FEAF9", "url": "https://www.electronjs.org/blog/rss.xml"},
    {"id": "tauri",        "name": "Tauri",        "icon": "🦀", "color": "#FFC131", "url": "https://v2.tauri.app/blog/rss.xml"},
    {"id": "dioxus",       "name": "Dioxus",       "icon": "🧩", "color": "#EB4E3D", "url": "https://dioxuslabs.com/blog/rss.xml"},
]

DATA_DIR      = Path("data")
SEEN_IDS_PATH = DATA_DIR / "seen_ids.json"
HTML_PATH     = Path("docs/index.html")
JST           = timezone(timedelta(hours=9))
MAX_PER_FEED  = 5      # フレームワークあたり取得件数
MAX_HTML_DAYS = 365    # HTMLに含める最大日数

# ── ユーティリティ ──────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:800]

def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")

def load_seen_ids() -> set:
    if SEEN_IDS_PATH.exists():
        return set(json.loads(SEEN_IDS_PATH.read_text(encoding="utf-8")))
    return set()

def save_seen_ids(seen: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_IDS_PATH.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def load_date_file(date: str) -> list:
    p = DATA_DIR / f"{date}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []

def save_date_file(date: str, articles: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / f"{date}.json"
    p.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")

def load_all_date_files() -> dict:
    """1年以内のJSONファイルを全部読んで {date: [articles]} を返す"""
    cutoff = (datetime.now(JST) - timedelta(days=MAX_HTML_DAYS)).strftime("%Y-%m-%d")
    by_date = {}
    for p in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True):
        date = p.stem
        if date < cutoff:
            continue
        by_date[date] = json.loads(p.read_text(encoding="utf-8"))
    return by_date

# ── RSS 取得 ──────────────────────────────────────────────────────────────────

def fetch_feed(feed: dict) -> list[dict]:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FrameworkPulse/1.0)"}
        resp = requests.get(feed["url"], headers=headers, timeout=15)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
        articles = []
        for entry in parsed.entries[:MAX_PER_FEED]:
            link  = entry.get("link", "")
            title = entry.get("title", "").strip()
            if not link or not title:
                continue
            desc = strip_html(
                entry.get("summary", "")
                or (entry.get("content") or [{}])[0].get("value", "")
            )
            articles.append({
                "fw_id":       feed["id"],
                "fw_name":     feed["name"],
                "fw_icon":     feed["icon"],
                "fw_color":    feed["color"],
                "id":          link,
                "title":       title,
                "link":        link,
                "pub_date":    entry.get("published", entry.get("updated", "")),
                "description": desc,
                "summary_ja":  None,
                "fetched_at":  datetime.now(timezone.utc).isoformat(),
            })
        print(f"  {feed['icon']} {feed['name']}: {len(articles)} 件取得")
        return articles
    except Exception as e:
        print(f"  ⚠ {feed['name']} フィード取得失敗: {e}", file=sys.stderr)
        return []

# ── Claude API 要約 ───────────────────────────────────────────────────────────

def summarize(client: anthropic.Anthropic, article: dict) -> str:
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    f"Framework: {article['fw_name']}\n"
                    f"Title: {article['title']}\n"
                    f"Excerpt: {article['description'] or '(なし)'}\n\n"
                    "この記事を開発者向けに2〜3文の日本語で要約してください。"
                    "新機能・変更点・注意点を具体的に。前置きなしで要約のみ出力。"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    要約失敗: {e}", file=sys.stderr)
        return ""

# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def generate_html(by_date: dict, updated_at: str) -> str:
    sections_html = ""
    for date in sorted(by_date.keys(), reverse=True):
        articles = by_date[date]
        if not articles:
            continue

        try:
            dt    = datetime.strptime(date, "%Y-%m-%d")
            label = f"{dt.year}年{dt.month}月{dt.day}日"
        except Exception:
            label = date

        cards_html = ""
        for a in articles:
            color      = a.get("fw_color", "#888")
            safe_title = a["title"].replace("<", "&lt;").replace(">", "&gt;")
            safe_link  = a["link"].replace('"', "&quot;")
            summary_block = (
                f'<p class="summary">{a["summary_ja"]}</p>'
                if a.get("summary_ja")
                else '<p class="no-summary">要約なし</p>'
            )
            cards_html += f"""
            <div class="card" style="--fw-color:{color}">
              <div class="card-top">
                <span class="fw-badge" style="background:{color}22;color:{color}">{a['fw_icon']} {a['fw_name']}</span>
              </div>
              <h3><a href="{safe_link}" target="_blank" rel="noopener">{safe_title}</a></h3>
              {summary_block}
            </div>"""

        sections_html += f"""
        <section class="day-section">
          <div class="day-header">
            <span class="day-label">{label}</span>
            <span class="day-count">{len(articles)} 件</span>
          </div>
          <div class="grid">{cards_html}</div>
        </section>"""

    if not sections_html:
        sections_html = '<p class="empty">まだ記事がありません。GitHub Actionsを実行してください。</p>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Framework Releases Summary</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Fraunces:opsz,wght@9..144,400;9..144,700&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0a0f;--surface:#111118;--border:#1e1e2e;--text:#e8e8f0;--muted:#6b6b88;--accent:#c8f250;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);font-family:'Space Mono',monospace;min-height:100vh;}}
header{{border-bottom:1px solid var(--border);padding:24px 40px;position:sticky;top:0;background:rgba(10,10,15,.93);backdrop-filter:blur(12px);z-index:100;display:flex;align-items:baseline;gap:20px;}}
.logo{{font-family:'Fraunces',serif;font-size:1.5rem;font-weight:700;color:var(--accent);}}
.updated{{font-size:.6rem;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-left:auto;}}
main{{padding:32px 40px;max-width:1300px;margin:0 auto;}}
.day-section{{margin-bottom:52px;}}
.day-header{{display:flex;align-items:baseline;gap:14px;margin-bottom:16px;padding-bottom:12px;border-bottom:2px solid var(--border);}}
.day-label{{font-family:'Fraunces',serif;font-size:1.25rem;font-weight:700;color:var(--text);letter-spacing:-.01em;}}
.day-count{{font-size:.62rem;color:var(--muted);letter-spacing:.08em;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;}}
.card{{background:var(--surface);border:1px solid var(--border);padding:18px;position:relative;transition:border-color .2s,transform .2s;}}
.card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--fw-color,var(--muted));opacity:.7;}}
.card:hover{{border-color:color-mix(in srgb,var(--fw-color,#888) 40%,var(--border));transform:translateY(-1px);}}
.card-top{{margin-bottom:10px;}}
.fw-badge{{font-size:.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:3px 8px;}}
h3{{font-family:'Fraunces',serif;font-size:.9rem;font-weight:700;line-height:1.35;margin-bottom:10px;}}
h3 a{{color:var(--text);text-decoration:none;}}
h3 a:hover{{color:var(--fw-color,var(--accent));}}
.summary{{font-size:.76rem;line-height:1.7;color:#a0a0c0;}}
.no-summary{{font-size:.7rem;color:var(--muted);font-style:italic;}}
.empty{{color:var(--muted);font-size:.8rem;padding:40px;text-align:center;border:1px dashed var(--border);}}
footer{{border-top:1px solid var(--border);padding:20px 40px;font-size:.6rem;color:var(--muted);text-align:center;letter-spacing:.08em;text-transform:uppercase;}}
@media(max-width:600px){{header,main{{padding-left:16px;padding-right:16px;}}.grid{{grid-template-columns:1fr;}}header{{flex-wrap:wrap;}}.updated{{margin-left:0;}}}}
</style>
</head>
<body>
<header>
  <div class="logo">Framework Releases Summary</div>
  <div class="updated">最終更新: {updated_at} JST</div>
</header>
<main>{sections_html}</main>
<footer>Framework Releases Summary — Powered by GitHub Actions &amp; Anthropic Claude - まじサンキューソーマッチ</footer>
</body>
</html>"""

# ── Slack 通知 ────────────────────────────────────────────────────────────────

def notify_slack(webhook_url: str, new_articles: list[dict]):
    total = len(new_articles)
    if total == 0:
        return

    by_fw: dict[str, list] = {}
    for a in new_articles:
        by_fw.setdefault(a["fw_id"], []).append(a)

    lines = [f"*📡 Framework Releases Summary — {today_jst()} の新着 {total} 件*\n"]
    for fw in FEEDS:
        arts = by_fw.get(fw["id"], [])
        if not arts:
            continue
        lines.append(f"{fw['icon']} *{fw['name']}* ({len(arts)}件)")
        for a in arts[:3]:
            lines.append(f">  <{a['link']}|{a['title']}>")
            if a.get("summary_ja"):
                lines.append(f">  _{a['summary_ja']}_")
        if len(arts) > 3:
            lines.append(f">  … 他 {len(arts)-3} 件")

    try:
        r = requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)
        r.raise_for_status()
        print(f"✅ Slack通知送信: {total}件")
    except Exception as e:
        print(f"⚠ Slack通知失敗: {e}", file=sys.stderr)

# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    api_key   = os.environ.get("ANTHROPIC_API_KEY")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    client   = anthropic.Anthropic(api_key=api_key)
    seen_ids = load_seen_ids()
    today    = today_jst()
    all_new: list[dict] = []

    for feed in FEEDS:
        print(f"\n{feed['icon']} {feed['name']} 処理中...")
        fetched  = fetch_feed(feed)
        new_arts = [a for a in fetched if a["id"] not in seen_ids]
        print(f"    新着: {len(new_arts)} 件")

        for art in new_arts:
            print(f"    要約中: {art['title'][:50]}...")
            art["summary_ja"] = summarize(client, art)
            seen_ids.add(art["id"])
            all_new.append(art)
            time.sleep(0.5)

    # 当日のJSONファイルに追記（既存分があれば先頭にマージ）
    if all_new:
        existing_today = load_date_file(today)
        save_date_file(today, all_new + existing_today)
        print(f"\n✅ data/{today}.json 保存完了 ({len(all_new)} 件追加)")
    else:
        print("\n新着なし — JSONファイル更新スキップ")

    save_seen_ids(seen_ids)
    print(f"✅ data/seen_ids.json 更新完了 (合計 {len(seen_ids)} 件既知)")

    # 1年以内の全JSONを読んでHTML生成
    by_date    = load_all_date_files()
    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(generate_html(by_date, updated_at), encoding="utf-8")
    print("✅ docs/index.html 生成完了")

    # Slack通知
    if slack_url and all_new:
        notify_slack(slack_url, all_new)
    elif not all_new:
        print("新着なし — Slack通知スキップ")


if __name__ == "__main__":
    main()
