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
from bs4 import BeautifulSoup

# ── 定数 ──────────────────────────────────────────────────────────────────────

DATA_DIR      = Path("data")
SEEN_IDS_PATH = DATA_DIR / "seen_ids.json"
HTML_PATH     = Path("docs/index.html")
RSS_PATH      = Path("docs/feed.xml")
JST               = timezone(timedelta(hours=9))
MAX_PER_FEED      = 5      # フレームワークあたり取得件数
MAX_HTML_DAYS     = 365    # HTMLに含める最大日数
WINDOW_START_HOUR = 8      # 取得ウィンドウ開始時刻 (JST) ── 前日08:00〜当日07:59

FEEDS = [
    {"id": "flutter",      "name": "Flutter",      "icon": "🐦", "color": "#54C5F8", "url": "https://medium.com/feed/flutter"},
    {"id": "react-native", "name": "React Native", "icon": "⚛️", "color": "#61DAFB", "url": "https://reactnative.dev/blog/rss.xml"},
    {"id": "expo",         "name": "Expo",         "icon": "🚀", "color": "#aaaaff", "url": None},  # スクレイピング
    {"id": "electron",     "name": "Electron",     "icon": "⚡", "color": "#9FEAF9", "url": "https://www.electronjs.org/blog/rss.xml"},
    {"id": "tauri",        "name": "Tauri",        "icon": "🦀", "color": "#FFC131", "url": None},  # スクレイピング
    {"id": "dioxus",       "name": "Dioxus",       "icon": "🧩", "color": "#EB4E3D", "url": "https://dioxuslabs.com/blog/rss.xml"},
    {"id": "flet",         "name": "Flet",         "icon": "🐟", "color": "#00D4AA", "url": "https://flet.dev/blog/rss.xml"},
]

EXPO_CHANGELOG_URL    = "https://expo.dev/changelog"
TAURI_RELEASE_URL     = "https://v2.tauri.app/release/"
TAURI_VERSIONS_PATH   = DATA_DIR / "tauri_versions.json"

# ── ユーティリティ ──────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:800]

def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")

def get_window() -> tuple[datetime, datetime]:
    """
    アクション実行日を「当日」として、
    前日 WINDOW_START_HOUR:00 JST 〜 当日 WINDOW_START_HOUR:00 JST 未満 を返す。
    例: 2026-05-01 に実行 → 2026-04-30 08:00 JST 〜 2026-05-01 07:59:59 JST
    """
    now   = datetime.now(JST)
    end   = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start, end

def parse_pub_date(raw: str) -> datetime | None:
    """feedparser / scraping が返す日付文字列を aware datetime に変換する。失敗時は None。"""
    if not raw:
        return None
    import email.utils
    try:
        # RFC 2822 形式 (例: "Tue, 30 Apr 2026 12:00:00 +0000")
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.astimezone(JST)
    except Exception:
        pass
    # ISO 8601: ミリ秒あり・なし・Z suffix を統一処理
    # 例: "2026-05-06T20:00:00.000Z" / "2026-05-06T20:00:00Z" / "2026-05-06T20:00:00+09:00"
    normalized = raw.strip()
    # ミリ秒部分（.XXX）を除去
    normalized = re.sub(r'\.\d+', '', normalized)
    # 末尾 Z を +00:00 に置換（Python 3.6以下との互換）
    normalized = re.sub(r'Z$', '+00:00', normalized)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(normalized, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(JST)
        except ValueError:
            continue
    return None

def in_window(pub_date_raw: str, start: datetime, end: datetime) -> bool:
    """pub_date が [start, end) の範囲内かどうかを返す。パース失敗時は False。"""
    dt = parse_pub_date(pub_date_raw)
    if dt is None:
        return False
    return start <= dt < end

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

# ── Expo Changelog スクレイピング ─────────────────────────────────────────────

EXPO_DATE_RE = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})")

def parse_expo_date(text: str) -> str:
    """'May 6, 2026' → ISO 8601。失敗時は空文字。"""
    text = text.strip()
    # %-d はmacOSで使えないので %d で統一（ゼロパディングでも動く）
    try:
        dt = datetime.strptime(text, "%B %d, %Y")
        return dt.replace(hour=12, tzinfo=timezone.utc).isoformat()
    except ValueError:
        pass
    # 1桁の日付（"May 6, 2026"）は %B %-d, %Y で来るが、
    # strptime は " 6" のゼロパディングなし数値を "%d" では受け付けない環境もある。
    # 正規化してリトライ。
    try:
        m = re.match(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", text)
        if m:
            normalized = f"{m.group(1)} {int(m.group(2)):02d}, {m.group(3)}"
            dt = datetime.strptime(normalized, "%B %d, %Y")
            return dt.replace(hour=12, tzinfo=timezone.utc).isoformat()
    except ValueError:
        pass
    return ""

def fetch_expo_changelog(feed: dict) -> list[dict]:
    """
    expo.dev/changelog をスクレイピングして記事リストを返す。

    実際のHTML構造（BeautifulSoupで見える形）:
      <ブロック要素>  "May 6, 2026"  <a href="/changelog/sdk-56-beta">/ Read more →</a>  </ブロック要素>
      <h2><a href="/changelog/sdk-56-beta">Expo SDK 56 Beta is now available</a></h2>
      <p>本文（あれば）</p>
      ... 次エントリ ...

    h2 を起点に、直前の兄弟要素のテキストから日付を正規表現で抽出する。
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FrameworkPulse/1.0)"}
        resp = requests.get(EXPO_CHANGELOG_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        articles = []
        for h2 in soup.find_all("h2"):
            title = h2.get_text(strip=True)
            # h2 の親が <a href="..."> なのでそこからリンクを取得
            a_tag = h2.parent if h2.parent.name == "a" else h2.find("a", href=True)
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            link = href if href.startswith("http") else f"https://expo.dev{href}"

            # 日付: h2.parent.parent 内の <time datetime="..."> を取得
            pub_date = ""
            try:
                container = h2.parent.parent
                time_tag  = container.find("time", attrs={"datetime": True})
                if time_tag:
                    # datetime="2026-05-06T20:00:00.000Z" → そのままISO形式で使える
                    pub_date = time_tag["datetime"]
            except Exception:
                pass

            # 本文: h2 直後の兄弟要素で "Read more" を含まないテキスト
            desc = ""
            for sib in h2.find_next_siblings():
                if sib.name in ("h2", "h1", "h3"):
                    break
                text = sib.get_text(" ", strip=True)
                if text and "Read more" not in text:
                    desc = text[:800]
                    break

            if not title or not link:
                continue

            articles.append({
                "fw_id":       feed["id"],
                "fw_name":     feed["name"],
                "fw_icon":     feed["icon"],
                "fw_color":    feed["color"],
                "id":          link,
                "title":       title,
                "link":        link,
                "pub_date":    pub_date,
                "description": desc,
                "summary_ja":  None,
                "fetched_at":  datetime.now(timezone.utc).isoformat(),
            })

            if len(articles) >= MAX_PER_FEED:
                break

        print(f"  {feed['icon']} {feed['name']}: {len(articles)} 件取得 (scraping)")
        return articles
    except Exception as e:
        print(f"  ⚠ {feed['name']} スクレイピング失敗: {e}", file=sys.stderr)
        return []

# ── 記事ページから本文スクレイピング ─────────────────────────────────────────

def fetch_page_description(url: str, selector_id: str) -> str:
    """
    記事ページを取得して指定IDの要素テキストを返す。失敗時は空文字。
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FrameworkPulse/1.0)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        el = soup.find(id=selector_id)
        if el:
            return strip_html(el.get_text(" ", strip=True))
        return ""
    except Exception as e:
        print(f"    ⚠ ページ本文取得失敗 ({url}): {e}", file=sys.stderr)
        return ""

# ── Tauri リリースページ スクレイピング ──────────────────────────────────────

def load_tauri_versions() -> set:
    if TAURI_VERSIONS_PATH.exists():
        return set(json.loads(TAURI_VERSIONS_PATH.read_text(encoding="utf-8")))
    return set()

def save_tauri_versions(versions: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TAURI_VERSIONS_PATH.write_text(
        json.dumps(sorted(versions), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def fetch_tauri_release_dates() -> dict[str, str]:
    """
    GitHub Releases atom フィードからバージョン→リリース日付のマップを返す。
    例: {"2.11.1": "2026-05-06T10:17:49Z", ...}
    """
    url = "https://github.com/tauri-apps/tauri/releases.atom"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FrameworkPulse/1.0)"}
    version_dates: dict[str, str] = {}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
        for entry in parsed.entries:
            tag = entry.get("title", "")  # 例: "tauri v2.11.1"
            m = re.search(r"tauri v([\d.]+)$", tag)
            if m:
                version_dates[m.group(1)] = entry.get("published", entry.get("updated", ""))
    except Exception as e:
        print(f"  ⚠ Tauri GitHub atom 取得失敗（日付取得用）: {e}", file=sys.stderr)
    return version_dates

def fetch_tauri_releases(feed: dict) -> list[dict]:
    """
    1. v2.tauri.app/release/ のサイドバーから tauri 本体バージョン一覧を取得
    2. data/tauri_versions.json と比較して未取得バージョンを検出
    3. GitHub atom フィードでリリース日付を取得（pub_date に使用）
    4. v2.tauri.app の詳細ページから本文を取得して要約
    5. 実際に取得できた分だけ tauri_versions.json に追記
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FrameworkPulse/1.0)"}
    try:
        # バージョン一覧を取得
        resp = requests.get(TAURI_RELEASE_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        all_links = soup.find_all("a", href=True)
        tauri_links = [
            a for a in all_links
            if re.match(r"^/release/tauri/v[\d.]+/$", a["href"])
        ]

        known_versions  = load_tauri_versions()
        release_dates   = fetch_tauri_release_dates()
        articles        = []
        fetched_versions: set[str] = set()

        for a in tauri_links:
            version  = a.get_text(strip=True)   # "2.11.1"
            page_url = f"https://v2.tauri.app{a['href']}"

            if version in known_versions:
                continue  # 既取得済み → スキップ

            print(f"    新バージョン検出: tauri v{version}")

            # リリース日付（GitHub atom から）
            pub_date = release_dates.get(version, datetime.now(timezone.utc).isoformat())

            # 詳細ページから本文取得
            try:
                r2 = requests.get(page_url, headers=headers, timeout=15)
                r2.raise_for_status()
                soup2 = BeautifulSoup(r2.text, "html.parser")
                main  = soup2.find("main")
                desc  = strip_html(main.get_text(" ", strip=True)) if main else ""
            except Exception as e:
                print(f"    ⚠ 詳細ページ取得失敗 ({page_url}): {e}", file=sys.stderr)
                desc = ""

            articles.append({
                "fw_id":       feed["id"],
                "fw_name":     feed["name"],
                "fw_icon":     feed["icon"],
                "fw_color":    feed["color"],
                "id":          page_url,
                "title":       f"tauri v{version}",
                "link":        page_url,
                "pub_date":    pub_date,
                "description": desc,
                "summary_ja":  None,
                "fetched_at":  datetime.now(timezone.utc).isoformat(),
            })
            fetched_versions.add(version)

            if len(articles) >= MAX_PER_FEED:
                break

        # 実際に取得できた分だけ known_versions に追記して保存
        if fetched_versions:
            save_tauri_versions(known_versions | fetched_versions)

        print(f"  {feed['icon']} {feed['name']}: {len(articles)} 件取得 (scraping)")
        return articles

    except Exception as e:
        print(f"  ⚠ {feed['name']} スクレイピング失敗: {e}", file=sys.stderr)
        return []

# ── RSS 取得 ──────────────────────────────────────────────────────────────────

def fetch_feed(feed: dict) -> list[dict]:
    if feed["id"] == "expo":
        return fetch_expo_changelog(feed)
    if feed["id"] == "tauri":
        return fetch_tauri_releases(feed)

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

            # Flet は RSS の summary が短すぎるので記事ページから本文を取得
            if feed["id"] == "flet":
                desc = fetch_page_description(link, "__blog-post-container")
            else:
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
                    "記事の内容を読み込めなかった場合は、「要約失敗」のみ出力。"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    要約失敗: {e}", file=sys.stderr)
        return ""

# ── HTML 生成（Shiny Metal Industrial デザイン） ──────────────────────────────

def generate_html(by_date: dict, updated_at: str) -> str:
    sections_html = ""
    for date in sorted(by_date.keys(), reverse=True):
        articles = by_date[date]
        if not articles:
            continue

        try:
            dt    = datetime.strptime(date, "%Y-%m-%d")
            label = f"{dt.year}.{dt.month:02d}.{dt.day:02d}"
        except Exception:
            label = date

        # リベット（divider用）
        rivets = '<div class="rivet"></div>' * 10

        cards_html = ""
        for a in articles:
            color      = a.get("fw_color", "#909aa8")
            safe_title = a["title"].replace("<", "&lt;").replace(">", "&gt;")
            safe_link  = a["link"].replace('"', "&quot;")
            summary_block = (
                f'<p class="card-summary">{a["summary_ja"]}</p>'
                if a.get("summary_ja")
                else '<p class="card-no-summary">— NO SUMMARY —</p>'
            )
            cards_html += f"""
            <div class="steel-card" style="--fw-color:{color}">
              <div class="corner-mark tl"></div>
              <div class="corner-mark tr"></div>
              <div class="corner-mark bl"></div>
              <div class="corner-mark br"></div>
              <div class="fw-badge-wrap">
                <span class="fw-badge" style="border-left-color:{color};color:{color}">{a['fw_icon']} {a['fw_name']}</span>
              </div>
              <h3><a href="{safe_link}" target="_blank" rel="noopener">{safe_title}</a></h3>
              {summary_block}
            </div>"""

        sections_html += f"""
        <section class="day-section">
          <div class="steel-divider-wrap">
            <div class="steel-divider">{rivets}</div>
            <div class="steel-divider-bottom"></div>
          </div>
          <div class="day-header">
            <h2 class="day-label">{label}</h2>
            <span class="day-count">{len(articles)} ARTICLES</span>
          </div>
          <div class="card-grid">{cards_html}</div>
        </section>"""

    if not sections_html:
        sections_html = """
        <div class="empty-state">
          <div class="hazard-stripe-bar"></div>
          <p>NO DATA — Run GitHub Actions to fetch articles.</p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Framework Releases Summary</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}

body{{
  background:#111214;
  background-image:
    radial-gradient(ellipse at 20% 50%,rgba(30,40,60,.8) 0%,transparent 60%),
    radial-gradient(ellipse at 80% 20%,rgba(50,30,20,.4) 0%,transparent 50%);
  min-height:100vh;
  font-family:'Rajdhani',sans-serif;
  color:#ccc;
  overflow-x:hidden;
}}

/* ── HEADER ── */
header{{
  position:sticky;top:0;z-index:100;
  background:
    linear-gradient(135deg,
      rgba(255,255,255,.9) 0%,rgba(220,225,235,.6) 8%,
      rgba(160,168,178,.4) 18%,rgba(120,128,138,.3) 30%,
      rgba(140,148,158,.4) 52%,rgba(200,208,218,.6) 62%,
      rgba(255,255,255,.85) 70%,rgba(100,108,118,.3) 90%,
      rgba(60,68,78,.4) 100%),
    linear-gradient(90deg,rgba(255,255,255,.1) 0%,transparent 50%,rgba(255,255,255,.05) 100%);
  background-color:#b8bec8;
  border-bottom:3px solid #505860;
  box-shadow:0 4px 20px rgba(0,0,0,.8),0 1px 0 rgba(255,255,255,.4) inset;
  overflow:hidden;
}}
header::before{{
  content:'';position:absolute;inset:0;
  background:linear-gradient(168deg,rgba(255,255,255,.95) 0%,rgba(255,255,255,0) 35%,transparent 60%);
  pointer-events:none;z-index:1;
}}
header::after{{
  content:'';position:absolute;inset:0;
  background-image:repeating-linear-gradient(90deg,transparent,transparent 2px,rgba(255,255,255,.03) 2px,rgba(255,255,255,.03) 3px);
  pointer-events:none;z-index:2;
}}
.header-inner{{
  position:relative;z-index:3;
  max-width:1300px;margin:0 auto;
  padding:20px 40px;
  display:flex;align-items:center;gap:20px;
}}
.logo{{
  font-family:'Bebas Neue',sans-serif;
  font-size:clamp(32px,6vw,52px);
  letter-spacing:.12em;
  background:linear-gradient(160deg,
    #1a2030 0%,#283848 20%,#101820 40%,
    #2a3848 60%,#182030 80%,#0e1820 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  filter:drop-shadow(0 1px 0 rgba(255,255,255,.5));
  line-height:1;
}}
.header-meta{{margin-left:auto;text-align:right;}}
.header-tagline{{
  font-size:10px;letter-spacing:.35em;text-transform:uppercase;
  color:#404858;font-weight:700;
}}
.header-updated{{
  font-size:10px;letter-spacing:.2em;text-transform:uppercase;
  color:#606878;margin-top:3px;
}}

/* ── MAIN ── */
main{{max-width:1300px;margin:0 auto;padding:0 40px 60px;}}

/* ── DIVIDER ── */
.steel-divider-wrap{{margin:40px 0 0;}}
.steel-divider{{
  height:18px;
  background:linear-gradient(180deg,#2a2e38 0%,#3a3e48 40%,#252830 100%);
  border-top:1px solid rgba(255,255,255,.1);
  border-bottom:1px solid rgba(0,0,0,.6);
  display:flex;align-items:center;gap:12px;padding:0 16px;
  box-shadow:0 2px 8px rgba(0,0,0,.5) inset;
}}
.rivet{{
  width:8px;height:8px;border-radius:50%;flex-shrink:0;
  background:radial-gradient(circle at 35% 35%,rgba(255,255,255,.8) 0%,rgba(180,190,200,.5) 40%,rgba(60,70,80,.6) 100%);
  background-color:#909aa8;
  box-shadow:0 1px 0 rgba(255,255,255,.6) inset,0 -1px 0 rgba(0,0,0,.4) inset,0 2px 4px rgba(0,0,0,.6);
}}
.steel-divider-bottom{{
  height:2px;
  background:linear-gradient(180deg,rgba(0,0,0,.5) 0%,rgba(255,255,255,.06) 100%);
}}

/* ── DAY HEADER ── */
.day-header{{
  display:flex;align-items:baseline;gap:16px;
  padding:20px 0 14px;
  border-bottom:1px solid rgba(255,255,255,.06);
  margin-bottom:20px;
}}
.day-label{{
  font-family:'Bebas Neue',sans-serif;
  font-size:clamp(28px,5vw,44px);
  letter-spacing:.15em;
  background:linear-gradient(160deg,#fff 0%,#e0e8f0 15%,#a8b8c8 30%,#687888 45%,#c8d8e8 60%,#fff 72%,#90a0b0 85%,#d0d8e0 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  filter:drop-shadow(0 2px 6px rgba(0,0,0,.6));
  line-height:1;
}}
.day-count{{
  font-size:11px;letter-spacing:.3em;text-transform:uppercase;
  color:#506070;font-weight:700;
}}

/* ── CARD GRID ── */
.card-grid{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:0;
}}

/* ── STEEL CARD ── */
.steel-card{{
  padding:22px 20px;
  position:relative;overflow:hidden;
  border:1px solid rgba(0,0,0,.5);
  border-right-width:0;
  outline:1px solid rgba(255,255,255,.07);
  outline-offset:-2px;
  background:linear-gradient(160deg,#1e2028 0%,#161820 40%,#12141c 70%,#1a1c24 100%);
  background-color:#161820;
  transition:filter .18s;
}}
.steel-card:last-child{{border-right-width:1px;}}
.steel-card:hover{{filter:brightness(1.12);}}

/* brushed texture */
.steel-card::after{{
  content:'';position:absolute;inset:0;
  background-image:
    repeating-linear-gradient(0deg,transparent,transparent 6px,rgba(255,255,255,.008) 6px,rgba(255,255,255,.008) 7px),
    repeating-linear-gradient(90deg,transparent,transparent 3px,rgba(255,255,255,.005) 3px,rgba(255,255,255,.005) 4px);
  pointer-events:none;z-index:0;
}}

/* fw-color left accent */
.steel-card::before{{
  content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--fw-color,#909aa8);
  opacity:.8;z-index:1;
}}

/* corner marks */
.corner-mark{{position:absolute;width:10px;height:10px;border-color:rgba(255,255,255,.15);border-style:solid;z-index:2;}}
.corner-mark.tl{{top:6px;left:6px;border-width:1px 0 0 1px;}}
.corner-mark.tr{{top:6px;right:6px;border-width:1px 1px 0 0;}}
.corner-mark.bl{{bottom:6px;left:6px;border-width:0 0 1px 1px;}}
.corner-mark.br{{bottom:6px;right:6px;border-width:0 1px 1px 0;}}

.fw-badge-wrap{{position:relative;z-index:3;margin-bottom:10px;}}
.fw-badge{{
  display:inline-block;
  font-size:10px;font-weight:700;letter-spacing:.25em;text-transform:uppercase;
  padding:2px 8px;border-radius:0;
  border-left:3px solid var(--fw-color,#909aa8);
  background:rgba(255,255,255,.04);
}}

.steel-card h3{{
  font-family:'Bebas Neue',sans-serif;
  font-size:17px;letter-spacing:.1em;line-height:1.3;
  margin-bottom:10px;
  position:relative;z-index:3;
}}
.steel-card h3 a{{
  color:#c8d4e0;text-decoration:none;
  transition:color .15s;
}}
.steel-card h3 a:hover{{color:var(--fw-color,#c8d4e0);}}

.card-summary{{
  font-size:16px;line-height:1.7;letter-spacing:.04em;
  color:#C1C8CF;position:relative;z-index:3;
}}
.card-no-summary{{
  font-size:11px;letter-spacing:.25em;
  color:#303840;font-style:normal;
  position:relative;z-index:3;
}}

/* ── EMPTY STATE ── */
.empty-state{{
  margin:60px 0;border:1px solid rgba(255,255,255,.06);
  outline:1px solid rgba(0,0,0,.6);outline-offset:3px;
  overflow:hidden;
}}
.hazard-stripe-bar{{
  height:8px;
  background:repeating-linear-gradient(-45deg,#c09010 0px,#c09010 8px,#181410 8px,#181410 16px);
}}
.empty-state p{{
  padding:32px;font-size:12px;letter-spacing:.3em;
  text-transform:uppercase;color:#404850;text-align:center;
}}

/* ── FOOTER ── */
footer{{
  border-top:1px solid rgba(255,255,255,.06);
  padding:20px 40px;
  font-size:10px;letter-spacing:.25em;text-transform:uppercase;
  color:#C1C8CF;text-align:center;
}}

@media(max-width:600px){{
  .header-inner,main,footer{{padding-left:16px;padding-right:16px;}}
  .card-grid{{grid-template-columns:1fr;}}
  .steel-card{{border-right-width:1px !important;}}
  .header-meta{{display:none;}}
}}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div class="logo">Framework Releases Summary</div>
    <div class="header-meta">
      <div class="header-tagline">React Native · Expo · Flutter · Flet · Electron · Tauri · Dioxus</div>
      <div class="header-updated">Last updated: {updated_at} JST</div>
    </div>
  </div>
</header>
<main>{sections_html}</main>
<footer>Framework Releases Summary — Powered by GitHub Actions &amp; Anthropic Claude <br /> まじサンキューソーマッチ</footer>
</body>
</html>"""

# ── RSS フィード生成 ──────────────────────────────────────────────────────────

def generate_rss(by_date: dict, site_url: str = "") -> str:
    """
    1年以内の全記事から RSS 2.0 フィードを生成する。
    site_url: GitHub Pages の URL（例: https://yourname.github.io/framework-pulse）
              未設定でも動作するが、<link> が相対パスになる。
    """
    import xml.etree.ElementTree as ET
    from email.utils import format_datetime

    def to_rfc2822(pub_date_raw: str) -> str:
        dt = parse_pub_date(pub_date_raw)
        if dt:
            return format_datetime(dt)
        return format_datetime(datetime.now(JST))

    def escape_xml(text: str) -> str:
        return (text or "")                 \
            .replace("&", "&amp;")          \
            .replace("<", "&lt;")           \
            .replace(">", "&gt;")           \
            .replace('"', "&quot;")         \
            .replace("'", "&apos;")

    # 全記事を日付の新しい順にフラット化
    all_articles: list[dict] = []
    for date in sorted(by_date.keys(), reverse=True):
        all_articles.extend(by_date[date])

    items_xml = ""
    for a in all_articles[:50]:  # RSSは最新50件まで
        title   = escape_xml(f"{a['fw_icon']} {a['fw_name']} — {a['title']}")
        link    = escape_xml(a["link"])
        desc    = escape_xml(a.get("summary_ja") or a.get("description") or "")
        pub     = to_rfc2822(a.get("pub_date", ""))
        guid    = escape_xml(a["id"])
        items_xml += f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{guid}</guid>
      <pubDate>{pub}</pubDate>
      <description>{desc}</description>
    </item>"""

    now_rfc = format_datetime(datetime.now(JST))
    feed_link = site_url or "https://example.github.io/framework-pulse"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Framework Pulse</title>
    <link>{feed_link}</link>
    <description>Flutter / React Native / Expo / Electron / Tauri / Dioxus の最新ニュース（AI日本語要約付き）</description>
    <language>ja</language>
    <lastBuildDate>{now_rfc}</lastBuildDate>
    <atom:link href="{feed_link}/feed.xml" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>"""

# ── Slack 通知 ────────────────────────────────────────────────────────────────

def notify_slack(webhook_url: str, new_articles: list[dict]):
    total = len(new_articles)
    if total == 0:
        return

    by_fw: dict[str, list] = {}
    for a in new_articles:
        by_fw.setdefault(a["fw_id"], []).append(a)

    lines = [f"*📡 Framework Pulse — {today_jst()} の新着 {total} 件*\n"]
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
    win_start, win_end = get_window()
    print(f"\n📅 取得ウィンドウ: {win_start.strftime('%Y-%m-%d %H:%M')} 〜 {win_end.strftime('%Y-%m-%d %H:%M')} JST")
    all_new: list[dict] = []

    for feed in FEEDS:
        print(f"\n{feed['icon']} {feed['name']} 処理中...")
        fetched  = fetch_feed(feed)

        for art in fetched:
            if art["id"] in seen_ids:
                continue  # 既出 → スキップ（seen_ids登録済み）

            seen_ids.add(art["id"])  # 範囲外でも seen_ids には登録

            if not in_window(art["pub_date"], win_start, win_end):
                print(f"    範囲外スキップ: {art['title'][:50]}")
                continue  # 範囲外 → 要約・保存しない

            print(f"    要約中: {art['title'][:50]}...")
            art["summary_ja"] = summarize(client, art)
            all_new.append(art)
            time.sleep(0.5)

        in_window_count = sum(1 for a in all_new if a["fw_id"] == feed["id"])
        print(f"    範囲内新着: {in_window_count} 件")

    # 当日のJSONファイルに追記（既存分があれば先頭にマージ）
    if all_new:
        existing_today = load_date_file(today)
        save_date_file(today, all_new + existing_today)
        print(f"\n✅ data/{today}.json 保存完了 ({len(all_new)} 件追加)")
    else:
        print("\n新着なし — JSONファイル更新スキップ")

    save_seen_ids(seen_ids)
    print(f"✅ data/seen_ids.json 更新完了 (合計 {len(seen_ids)} 件既知)")

    # 1年以内の全JSONを読んでHTML・RSS生成
    by_date    = load_all_date_files()
    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(generate_html(by_date, updated_at), encoding="utf-8")
    print("✅ docs/index.html 生成完了")
    site_url = os.environ.get("SITE_URL", "")
    RSS_PATH.write_text(generate_rss(by_date, site_url), encoding="utf-8")
    print(f"✅ docs/feed.xml 生成完了{f' ({site_url})' if site_url else ' (SITE_URL未設定)'}")

    # Slack通知
    if slack_url and all_new:
        notify_slack(slack_url, all_new)
    elif not all_new:
        print("新着なし — Slack通知スキップ")


if __name__ == "__main__":
    main()
