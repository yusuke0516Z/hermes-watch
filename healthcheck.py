#!/usr/bin/env python3
"""日次ヘルスチェック: 監視システム自体の健康状態をLINEで報告する。

GitHub Actions（daily-health.yml）から毎晩実行される想定。Macに依存しない。
チェック項目:
  - 直近24hのActions実行の成功/失敗数
  - 直近24hの監視イテレーション数と最大間隔（freshness.jsonlから）
  - ScrapFlyクレジット消費ペース（月末までの着地予測）
  - LINE無料枠の消費
異常があれば ⚠️ を付けて目立たせる。

使い方:
  python3 healthcheck.py         # LINEに送信
  python3 healthcheck.py --dry   # 送信せず標準出力のみ
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
REPO = os.environ.get("GITHUB_REPOSITORY", "yusuke0516Z/hermes-watch")


def read_secret(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip()
                    break
    return val


def http_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def actions_stats() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = http_json(
            f"https://api.github.com/repos/{REPO}/actions/runs?created=>{since}&per_page=100",
            headers,
        )
    except Exception as e:
        return f"⚠️ Actions実行履歴の取得に失敗: {e}"
    runs = data.get("workflow_runs", [])
    ok = sum(1 for r in runs if r.get("conclusion") == "success")
    bad = sum(1 for r in runs if r.get("conclusion") in ("failure", "timed_out"))
    mark = "⚠️ " if bad else ""
    return f"{mark}Actions実行: 成功{ok} / 失敗{bad}（24h）"


def freshness_stats() -> str:
    path = BASE_DIR / "logs" / "freshness.jsonl"
    if not path.exists():
        return "⚠️ freshness.jsonl なし"
    cutoff = datetime.now(JST) - timedelta(hours=24)
    times = []
    paid = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            t = datetime.fromisoformat(r["checked_at"])
        except Exception:
            continue
        if t >= cutoff:
            times.append(t)
            if r.get("via") == "scrapfly":
                paid += 1
    if len(times) < 2:
        return f"⚠️ 監視回数: {len(times)}回/24h（少なすぎる。停止の疑い）"
    gaps = [(times[i + 1] - times[i]).total_seconds() / 60 for i in range(len(times) - 1)]
    max_gap = max(gaps)
    mark = "⚠️ " if max_gap > 45 else ""
    return (f"監視回数: {len(times)}回/24h（有料経路 {paid}回）\n"
            f"{mark}最大間隔: {max_gap:.0f}分")


def scrapfly_stats() -> str:
    key = read_secret("SCRAPFLY_API_KEY")
    if not key:
        return "⚠️ ScrapFlyキー未設定"
    try:
        d = http_json(f"https://api.scrapfly.io/account?key={key}")
    except Exception as e:
        return f"⚠️ ScrapFly残高の取得に失敗: {e}"
    sub = d.get("subscription", {})
    u = sub.get("usage", {}).get("scrape", {})
    used, limit = u.get("current", 0), u.get("limit", 1)
    period = sub.get("period", {})
    try:
        # ScrapFlyの期間は "2026-08-13 08:20:02" 形式（タイムゾーン表記なし・UTC）
        start = datetime.fromisoformat(period["start"]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(period["end"]).replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        total = (end - start).total_seconds()
        projected = int(used / max(elapsed, 1) * total)
    except Exception:
        projected = 0
    mark = "⚠️ " if projected > limit * 0.95 else ""
    return (f"クレジット: {used:,} / {limit:,}\n"
            f"{mark}月末着地予測: {projected:,}")


def line_quota(token: str) -> str:
    try:
        q = http_json("https://api.line.me/v2/bot/message/quota",
                      {"Authorization": f"Bearer {token}"})
        c = http_json("https://api.line.me/v2/bot/message/quota/consumption",
                      {"Authorization": f"Bearer {token}"})
    except Exception as e:
        return f"⚠️ LINE残量の取得に失敗: {e}"
    used, limit = c.get("totalUsage", 0), q.get("value", 200)
    mark = "⚠️ " if used > limit * 0.8 else ""
    return f"{mark}LINE通数: {used} / {limit}（今月）"


def main() -> None:
    dry = "--dry" in sys.argv
    token = read_secret("LINE_CHANNEL_TOKEN")
    now = datetime.now(JST).strftime("%m/%d %H:%M")
    parts = [
        f"🩺 Hermès Watch 日次ヘルスチェック（{now}）",
        actions_stats(),
        freshness_stats(),
        scrapfly_stats(),
        line_quota(token) if token else "⚠️ LINEトークン未設定",
    ]
    text = "\n\n".join(parts)
    warn = text.count("⚠️")
    if warn:
        text = f"⚠️ 要確認 {warn}件\n\n" + text

    print(text)
    if dry:
        return
    if not token:
        sys.exit("LINE_CHANNEL_TOKEN 未設定のため送信不可")
    payload = json.dumps({"messages": [{"type": "text", "text": text}]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    urllib.request.urlopen(req, timeout=20)
    print("sent to LINE")


if __name__ == "__main__":
    main()
