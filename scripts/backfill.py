#!/usr/bin/env python3
"""
backfill.py — 指定日付の記事を後追い取得するスクリプト

使い方:
  python scripts/backfill.py 2026-04-01

動作:
  - 指定日付を「実行日」として fetch_and_summarize.py の通常ウィンドウロジックを再利用
    (指定日の前日 08:00 JST 〜 指定日 07:59 JST)
  - seen_ids のチェックは行わない（後追いなので既出でも強制取得）
  - 取得結果は data/YYYY-MM-DD.json に上書きマージ
  - docs/index.html を再生成
  - Slack 通知は送らない
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 引数チェック ──────────────────────────────────────────────────────────────

def parse_args() -> datetime:
    if len(sys.argv) != 2:
        print("使い方: python scripts/backfill.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    raw = sys.argv[1].strip()
    try:
        JST = timezone(timedelta(hours=9))
        dt  = datetime.strptime(raw, "%Y-%m-%d")
        return dt.replace(tzinfo=JST)
    except ValueError:
        print(f"ERROR: 日付の形式が不正です → '{raw}' (期待値: YYYY-MM-DD)", file=sys.stderr)
        sys.exit(1)

# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    target_dt = parse_args()
    target_str = target_dt.strftime("%Y-%m-%d")

    # fetch_and_summarize をモジュールとしてインポートする前に
    # sys.path にスクリプトのディレクトリを追加
    scripts_dir = Path(__file__).parent
    sys.path.insert(0, str(scripts_dir))
    import fetch_and_summarize as fs

    JST               = fs.JST
    WINDOW_START_HOUR = fs.WINDOW_START_HOUR

    # ── ウィンドウを指定日付で計算 ──────────────────────────────────────────
    win_end   = target_dt.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    win_start = win_end - timedelta(days=1)

    print(f"\n🔁 後追い取得モード: {target_str}")
    print(f"📅 取得ウィンドウ: {win_start.strftime('%Y-%m-%d %H:%M')} 〜 {win_end.strftime('%Y-%m-%d %H:%M')} JST")
    print("⚠️  seen_ids チェックをスキップ（強制取得）")
    print("⚠️  Slack 通知はスキップ\n")

    # ── Anthropic クライアント ──────────────────────────────────────────────
    import os
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    # ── 各フィード取得 ────────────────────────────────────────────────────
    all_new: list[dict] = []

    for feed in fs.FEEDS:
        print(f"{feed['icon']} {feed['name']} 処理中...")
        fetched = fs.fetch_feed(feed)

        in_window_arts = []
        for art in fetched:
            if not fs.in_window(art["pub_date"], win_start, win_end):
                print(f"    範囲外スキップ: {art['title'][:50]}")
                continue
            in_window_arts.append(art)

        print(f"    範囲内: {len(in_window_arts)} 件")

        for art in in_window_arts:
            print(f"    要約中: {art['title'][:50]}...")
            art["summary_ja"] = fs.summarize(client, art)
            all_new.append(art)
            time.sleep(0.5)

    # ── JSON 保存（既存分とマージ、重複はリンクURLで除外） ─────────────────
    if all_new:
        existing    = fs.load_date_file(target_str)
        existing_ids = {a["id"] for a in existing}
        merged      = [a for a in all_new if a["id"] not in existing_ids] + existing
        fs.save_date_file(target_str, merged)
        added = len(merged) - len(existing)
        print(f"\n✅ data/{target_str}.json 保存完了 ({added} 件追加, 合計 {len(merged)} 件)")

        # seen_ids にも追記（今後の通常取得で重複しないよう）
        seen_ids = fs.load_seen_ids()
        before   = len(seen_ids)
        for a in all_new:
            seen_ids.add(a["id"])
        fs.save_seen_ids(seen_ids)
        print(f"✅ data/seen_ids.json 更新完了 ({len(seen_ids) - before} 件追加)")
    else:
        print(f"\n該当記事なし — data/{target_str}.json は更新しません")

    # ── HTML・RSS 再生成 ───────────────────────────────────────────────────
    by_date    = fs.load_all_date_files()
    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    fs.HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    fs.HTML_PATH.write_text(fs.generate_html(by_date, updated_at), encoding="utf-8")
    print("✅ docs/index.html 再生成完了")
    site_url = os.environ.get("SITE_URL", "")
    fs.RSS_PATH.write_text(fs.generate_rss(by_date, site_url), encoding="utf-8")
    print("✅ docs/feed.xml 再生成完了")


if __name__ == "__main__":
    main()
