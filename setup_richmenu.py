#!/usr/bin/env python3
"""リッチメニューを作成・画像アップロード・全ユーザーへ既定適用する。

使い方:
    python3 setup_richmenu.py          # 作成して既定に設定
    python3 setup_richmenu.py --list   # 現在の登録状況を表示
    python3 setup_richmenu.py --clean  # 既存のリッチメニューを全削除

トークンは .env の LINE_CHANNEL_TOKEN を読む（正本は1Password）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
IMAGE = BASE / "assets" / "richmenu.png"
API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"

# 画像は 2500x1686。3列×2行に分割する。
COL = [0, 833, 1666]
COL_W = [833, 833, 834]
ROW = [0, 843]
ROW_H = 843

CELLS = [
    # (列, 行, ラベル, URL)
    (0, 0, "レディスバッグ", "https://www.hermes.com/jp/ja/category/leather-goods/bags-and-clutches/womens-bags-and-clutches/"),
    (1, 0, "バッグ＆クラッチ", "https://www.hermes.com/jp/ja/category/leather-goods/bags-and-clutches/"),
    (2, 0, "レザーグッズ", "https://www.hermes.com/jp/ja/category/leather-goods/"),
    (0, 1, "ウィメンズ", "https://www.hermes.com/jp/ja/category/women/"),
    (1, 1, "カート", "https://www.hermes.com/jp/ja/cart/"),
    (2, 1, "Hermès トップ", "https://www.hermes.com/jp/ja/"),
]


def token() -> str:
    tok = os.environ.get("LINE_CHANNEL_TOKEN", "")
    if not tok:
        env = BASE / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("LINE_CHANNEL_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
                    break
    if not tok:
        sys.exit("LINE_CHANNEL_TOKEN が見つかりません（.env か環境変数に設定してください）")
    return tok


def call(url: str, method: str = "GET", body: bytes | None = None,
         content_type: str = "application/json") -> dict:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8") or "{}"
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"APIエラー {e.code} {method} {url}\n{detail}")


def build_menu() -> dict:
    areas = []
    for col, row, label, url in CELLS:
        areas.append({
            "bounds": {"x": COL[col], "y": ROW[row], "width": COL_W[col], "height": ROW_H},
            "action": {"type": "uri", "label": label[:20], "uri": url},
        })
    return {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "Hermes Watch Menu",
        "chatBarText": "メニュー",
        "areas": areas,
    }


def list_menus() -> None:
    res = call(f"{API}/richmenu/list")
    menus = res.get("richmenus", [])
    print(f"登録済みリッチメニュー: {len(menus)}件")
    for m in menus:
        print(f"  {m['richMenuId']}  {m.get('name')}  areas={len(m.get('areas', []))}")
    default = call(f"{API}/user/all/richmenu")
    print("既定メニュー:", default.get("richMenuId", "(未設定)"))


def clean() -> None:
    for m in call(f"{API}/richmenu/list").get("richmenus", []):
        call(f"{API}/richmenu/{m['richMenuId']}", method="DELETE")
        print("削除:", m["richMenuId"])


def main() -> None:
    if "--list" in sys.argv:
        list_menus()
        return
    if "--clean" in sys.argv:
        clean()
        return

    if not IMAGE.exists():
        sys.exit(f"画像がありません: {IMAGE}")

    clean()  # 作り直しのたびに古いものを消す（無料枠は1000件だが混乱を防ぐ）

    created = call(f"{API}/richmenu", "POST", json.dumps(build_menu()).encode("utf-8"))
    rid = created["richMenuId"]
    print("作成:", rid)

    call(f"{DATA_API}/richmenu/{rid}/content", "POST", IMAGE.read_bytes(), "image/png")
    print("画像アップロード完了:", IMAGE.name)

    call(f"{API}/user/all/richmenu/{rid}", "POST")
    print("全ユーザーの既定メニューに設定しました")
    print("\n→ 奥様のLINEトーク画面下部にメニューが表示されます（反映に数分かかる場合あり）")


if __name__ == "__main__":
    main()
