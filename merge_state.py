#!/usr/bin/env python3
"""persist時のマージ: git rebase に頼らず state/freshness を決定的に統合する。

背景（2026-08-15）: 170分ランの走行中にリモートへ別コミットが入ると、
freshness.jsonl の追記同士が rebase で解決不能な競合になり、
push リトライが3回とも同じ理由で失敗していた。
git のテキストマージではなく、データの意味に沿ってマージする。

使い方（workflowのpersistステップから）:
    git fetch origin main && git reset --hard origin/main
    python3 merge_state.py /tmp/ours_state.json /tmp/ours_fresh.jsonl

マージ規則:
- freshness.jsonl: リモート版と自分の版の和集合（checked_at で重複排除・時刻順）。
  1MB超なら新しい半分だけ残す（hermes_monitor.record_freshness と同じ規則）
- state/products.json: 自分の版をそのまま採用。
  runは concurrency で直列化されており、直前まで動いていた自分が常に最新のため。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "state" / "products.json"
FRESH = BASE / "logs" / "freshness.jsonl"


def merge_freshness(ours_path: Path) -> None:
    rows: dict[str, dict] = {}
    for path in (FRESH, ours_path):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = r.get("checked_at")
            if key:
                rows[key] = r
    merged = [rows[k] for k in sorted(rows)]
    FRESH.parent.mkdir(parents=True, exist_ok=True)
    out = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged)
    if len(out.encode("utf-8")) > 1_000_000:
        lines = out.splitlines(keepends=True)
        out = "".join(lines[len(lines) // 2:])
    FRESH.write_text(out, encoding="utf-8")
    print(f"freshness merged: {len(merged)} rows")


def take_ours_state(ours_path: Path) -> None:
    if not ours_path.exists():
        print("no local state to merge (keeping remote)")
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(ours_path.read_text(encoding="utf-8"))  # 壊れたstateを書かない
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"state taken from this run: {len(data.get('products', {}))} products")


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: merge_state.py <ours_state.json> <ours_freshness.jsonl>")
    take_ours_state(Path(sys.argv[1]))
    merge_freshness(Path(sys.argv[2]))


if __name__ == "__main__":
    main()
