#!/usr/bin/env python3
"""Hermès JP bag stock monitor.

カテゴリページの hermes-state JSON（SSR埋め込み）から在庫あり商品を取得し、
前回状態と比較して 新商品 / 再入荷 / 新カラー / 新サイズ / 価格変更 をメール通知する。

前提（2026-08-12 調査済み・詳細は README.md）:
- カテゴリ一覧は「在庫あり商品のみ」掲載（fh_location に has_stock フィルタ）
  → SKU の出現＝購入可能、消滅＝売り切れ
- カテゴリページは curl 相当の素朴な GET で取得可（住宅用IPから）
- 検索ページ・商品詳細ページ・bck.hermes.com は DataDome/Cloudflare でブロック
  → 一覧データのみで完結する設計（素材はメールに載らない。商品ページリンクで代替）

使い方:
  python3 hermes_monitor.py --once --dry-run   # 1回実行・メール送らず内容表示
  python3 hermes_monitor.py --once             # 1回実行・メール送信
  python3 hermes_monitor.py --test-email       # テストメール送信のみ
  （定期実行は launchd から --once で呼ばれる）
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import random
import re
import smtplib
import sys
import time
import traceback
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state" / "products.json"
LOG_PATH = BASE_DIR / "logs" / "monitor.log"
FRESHNESS_PATH = BASE_DIR / "logs" / "freshness.jsonl"

JST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

EVENT_LABELS = {
    "NEW": "🆕 新商品",
    "RESTOCK": "🔥 再入荷",
    "NEW_COLOR": "🎨 新カラー",
    "NEW_SIZE": "📏 新サイズ",
    "PRICE_CHANGE": "💴 価格変更",
    "SOLD_OUT": "⚫ 売り切れ",
}
# メール件名に使う優先順位（高いものが件名になる）
EVENT_PRIORITY = ["RESTOCK", "NEW", "NEW_COLOR", "NEW_SIZE", "PRICE_CHANGE", "SOLD_OUT"]


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # 簡易ローテーション: 1MB 超えたら後半だけ残す
    if LOG_PATH.stat().st_size > 1_000_000:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-500:]
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"products": {}, "failure_streak": 0, "failure_alerted": False}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------- fetch/parse

def _in_paid_window(cfg: dict) -> bool:
    """有料経路を使う時間帯か。補充が集中する時間だけ課金して費用を抑える。"""
    sf = cfg.get("scrapfly", {})
    hours = sf.get("hours_jst")
    if not hours:
        return True  # 指定なしなら常時
    return datetime.now(JST).hour in hours


def fetch_via_scrapfly(cfg: dict, url: str) -> tuple[str, dict]:
    """ScrapFly経由で取得する（有料）。

    ここで重要なのは住宅IPそのものではなく **CDNキャッシュを外せること**。
    Hermèsのカテゴリページは Cloudflare で最大1時間キャッシュされ、
    普通に取得する限り誰が叩いても同じ古いコピーが返る。
    URLにクエリを付けると cf-cache-status: BYPASS になりオリジンまで届くが、
    素の取得では DataDome に阻まれる。ScrapFlyの asp がそこを突破する。
    """
    sf = cfg["scrapfly"]
    key = read_secret(sf.get("api_key_env", "SCRAPFLY_API_KEY"))
    if not key:
        raise RuntimeError(f"ScrapFly APIキー未設定: {sf.get('api_key_env')}")

    target = url
    if sf.get("cache_bust", True):
        sep = "&" if "?" in target else "?"
        target = f"{target}{sep}_={int(time.time())}"

    params = {
        "key": key,
        "url": target,
        "asp": "true" if sf.get("asp", True) else "false",
        "country": sf.get("country", "jp"),
        "render_js": "true" if sf.get("render_js", False) else "false",
    }
    api = "https://api.scrapfly.io/scrape?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(api, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=sf.get("timeout", 90)) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    result = payload.get("result", {})
    html = result.get("content", "")
    headers = {k.lower(): v for k, v in (result.get("response_headers") or {}).items()}
    cost = (payload.get("context") or {}).get("cost") or {}
    cache = {
        "age": headers.get("age"),
        "last_modified": headers.get("last-modified"),
        "cf_cache_status": headers.get("cf-cache-status"),
        "via": "scrapfly",
        "cost": cost.get("total") if isinstance(cost, dict) else cost,
    }
    return html, cache


def fetch_html(url: str) -> tuple[str, dict]:
    """HTMLとキャッシュ指標を返す。

    Hermèsのカテゴリページは Cloudflare CDN で max-age=3600 キャッシュされる。
    在庫検知の遅延はポーリング間隔ではなくこのキャッシュ鮮度で決まるため、
    age / last-modified を毎回記録して実際の更新間隔を実測する（Step 0）。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding", "")
        cache = {
            "age": resp.headers.get("Age"),
            "last_modified": resp.headers.get("Last-Modified"),
            "cf_cache_status": resp.headers.get("CF-Cache-Status"),
        }
    if enc == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace"), cache


def _age_sec(cache: dict) -> int:
    """Ageヘッダを秒で返す。取れない場合は「非常に古い」扱いにして比較から外す。"""
    try:
        return int(cache.get("age") or 0)
    except (TypeError, ValueError):
        return 10 ** 9


def record_freshness(cache: dict) -> None:
    """キャッシュ指標を freshness.jsonl に追記（Step 0の実測データ）。"""
    FRESHNESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"checked_at": datetime.now(JST).isoformat(), **cache}
    with open(FRESHNESS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    # monitor.log と同様に上限を設ける。毎イテレーション追記され、gitにもコミットされるため
    # 無制限だとリポジトリ履歴が際限なく肥大する。超過時は新しい半分だけ残す。
    try:
        if FRESHNESS_PATH.stat().st_size > 1_000_000:
            lines = FRESHNESS_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
            FRESHNESS_PATH.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
    except OSError:
        pass


class BlockedError(Exception):
    pass


def parse_products(html: str) -> list[dict]:
    """hermes-state JSON から商品一覧を取り出す。取得できなければ BlockedError。

    ブロック判定は「captcha-delivery という文字列があるか」ではなく
    「商品データが取れたか」で行う。ScrapFly経由で正常に取得したページには
    解決済みDataDomeのiframe要素が残っており、文字列で判定すると
    データが取れているのにブロック扱いになる（2026-08-13に誤検知を確認）。
    """
    m = re.search(
        r'<script id="hermes-state" type="application/json">(.*?)</script>', html, re.S
    )
    if m:
        try:
            st = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise BlockedError(f"hermes-state のJSONが壊れている: {e}")
        for v in st.values():
            if isinstance(v, dict) and isinstance(v.get("b"), dict):
                products = (v["b"].get("products") or {}).get("items")
                if products is not None:
                    return products

    # ここに来た＝商品データが無い。理由を切り分けてから投げる。
    if "captcha-delivery" in html or "Attention Required" in html:
        raise BlockedError("bot-protection page returned (DataDome/Cloudflare)")
    raise BlockedError("hermes-state / products が見つからない（サイト構造変更の可能性）")


# ------------------------------------------------------------------- matching

# 【表記ゆれを吸収する理由（2026-08-23）】
# 日本公式の商品名は「バッグ 《ガーデン・パーティ》 36」のように中黒で区切られ、
# slug も url もパーセントエンコードされた日本語（英語スラッグではない）。つまり
#   - 英語キーワード（"garden party" 等）はどこにも一致しない＝実質デッドコード
#   - 中黒の有無ひとつでキーワードが素通りする
# 実際に「ガーデンパーティ」（中黒なし）を登録して監視ゼロになり、しかもログ上は
# matched:0 = 在庫なしと見分けがつかず気づけなかった。照合前に表記ゆれだけを潰す。
_NOISE_RE = re.compile(r"[・･\s\u3000\-‐‑–—ー]")


def _norm(s: str) -> str:
    """中黒・空白・ハイフン・長音符を除去して小文字化する（表記ゆれの吸収）。"""
    return _NOISE_RE.sub("", s.lower())


def _norm_fields(*vals: str) -> str:
    """複数フィールドを正規化して連結する。
    空白を消すので、区切り "|" を残さないと title の末尾と slug の先頭が
    つながって偽の一致が生まれる。"""
    return "|".join(_norm(v or "") for v in vals)


def match_model(cfg: dict, item: dict) -> str | None:
    text = _norm_fields(item.get("title", ""), item.get("slug", ""), item.get("url", ""))
    for w in cfg["watch_models"]:
        if any(_norm(k) in text for k in w["keywords"]):
            return w["model"]
    return None


def is_priority_color(cfg: dict, rec: dict) -> bool:
    text = _norm_fields(rec.get("title", ""), rec.get("color", ""), rec.get("url", ""))
    return any(_norm(c) in text for c in cfg["priority_colors"])


# --self-test のフィクスチャ。すべて hermes.com/jp/ja に実在した公式商品名。
# 【監視モデルを追加したら、公式サイトの商品名をそのまま1件ここにも足すこと】
# これが無いと「キーワードが一致しないだけ」と「本当に在庫が無い」を区別できない。
SELF_TEST_FIXTURES = {
    "Picotin": "バッグ 《ピコタン・ロック》 18 ポケット",
    "Constance": "ウォレットバッグ 《コンスタンス》 トゥー・ゴー",
    "Lindy": "バッグ 《リンディII》 ミニ",
    "Garden Party": "バッグ 《ガーデン・パーティ》 36",
}

# 監視対象でない商品を誤って拾っていないか（過剰一致の検出）
SELF_TEST_NEGATIVES = [
    "バッグ 《ソー・メドール》",
    "バッグ 《ボリード1923》 45 カザック",
    "バッグ 《エトリヴィエール・ショッピング》",
]


def self_test(cfg: dict) -> None:
    """config のキーワードが実在の公式商品名に本当に一致するかを通信なしで検証する。

    2026-08-23 の事故の再発防止。「ガーデンパーティ」を中黒なしで登録したため
    公式名「バッグ 《ガーデン・パーティ》 36」に一致せず、監視されていないのに
    ログ上は matched:0（＝在庫なしと同じ見た目）で気づけなかった。
    """
    def as_item(title: str) -> dict:
        return {"title": title, "slug": title.replace(" ", "-"), "url": ""}

    ng, unverified = [], []
    print("watch_models の照合テスト:")
    for w in cfg["watch_models"]:
        model = w["model"]
        fixture = SELF_TEST_FIXTURES.get(model)
        if not fixture:
            unverified.append(model)
            continue
        hit = match_model(cfg, as_item(fixture))
        ok = hit == model
        print(f"  [{'OK' if ok else 'NG'}] {model:<13} <- {fixture}"
              + ("" if ok else f"   ← 実際の判定: {hit}"))
        if not ok:
            ng.append(model)

    print("過剰一致テスト（拾ってはいけない商品）:")
    for title in SELF_TEST_NEGATIVES:
        hit = match_model(cfg, as_item(title))
        print(f"  [{'OK' if hit is None else 'NG'}] {title}"
              + ("" if hit is None else f"   ← {hit} として誤検知"))
        if hit is not None:
            ng.append(f"過剰一致:{title}")

    for m in unverified:
        print(f"  [WARN] {m:<13} フィクスチャ未登録＝検証できない。"
              "SELF_TEST_FIXTURES に公式商品名を1件追加すること")

    if ng:
        print(f"\n❌ 失敗 {len(ng)}件: {', '.join(ng)}")
        print("   一致しないモデルは在庫が出ても通知されない。"
              "config.json のキーワードを公式サイトの表記どおりに直すこと")
        sys.exit(1)
    print(f"\n✅ watch_models {len(cfg['watch_models'])}件すべて公式商品名に一致"
          + (f"（未検証 {len(unverified)}件）" if unverified else ""))


def to_record(cfg: dict, item: dict, model: str, now: str) -> dict:
    img = ""
    if item.get("assets"):
        img = item["assets"][0].get("url", "")
        if img.startswith("//"):
            img = "https:" + img
        img = re.sub(r"size=\d+,\d+", "size=600,600", img)
    size = item.get("size", "")
    if size == "SANS_TAILLE":
        size = "-"
    # JSON内の url は "/product/..."（ロケール接頭辞なし）。実リンクは "/jp/ja/product/..."。
    # 将来 JSON 側が接頭辞付きに変わっても二重付与しないようにする。
    path = item.get("url", "")
    base = cfg["base_url"].rstrip("/")
    prefix = "/jp/ja"
    if path.startswith(prefix) and base.endswith(prefix):
        url = "https://www.hermes.com" + path
    else:
        url = base + path
    # Hermèsの商品URLは日本語（例 /product/バッグ-《ピコタン》-H0000/）を含む。
    # 非ASCIIのままだとLINEが "Invalid action URI" で400を返し通知が届かないため、
    # パス部分をパーセントエンコードする（ASCIIのみのURLは変化しない）。
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/-_.~"),
        urllib.parse.quote(parts.query, safe="=&"),
        parts.fragment,
    ))
    return {
        "sku": item["sku"],
        "model": model,
        "title": item.get("title", ""),
        "color": item.get("avgColor", ""),
        "size": size,
        "price": item.get("price"),
        "url": url,
        "image": img,
        "in_stock": bool(item.get("stock", {}).get("ecom")),
        "first_seen": now,
        "last_seen": now,
    }


# ----------------------------------------------------------------------- diff

def diff(cfg: dict, state: dict, current: dict[str, dict], now: str) -> list[dict]:
    """state['products'] を更新しつつイベント一覧を返す。"""
    events = []
    known = state["products"]

    # 既知モデルごとのカラー/サイズ集合（新カラー・新サイズ判定用）
    seen_colors: dict[str, set] = {}
    seen_sizes: dict[str, set] = {}
    seen_models: set = set()
    for rec in known.values():
        seen_models.add(rec["model"])
        seen_colors.setdefault(rec["model"], set()).add(rec.get("color", ""))
        seen_sizes.setdefault(rec["model"], set()).add(rec.get("size", ""))

    for sku, rec in current.items():
        old = known.get(sku)
        if old is None:
            if rec["model"] not in seen_models:
                etype = "NEW"
            elif rec.get("color") and rec["color"] not in seen_colors.get(rec["model"], set()):
                etype = "NEW_COLOR"
            elif rec.get("size") and rec["size"] not in seen_sizes.get(rec["model"], set()):
                etype = "NEW_SIZE"
            else:
                etype = "NEW"
            events.append({"type": etype, "rec": rec})
            known[sku] = rec
        else:
            if not old.get("in_stock", False):
                events.append({"type": "RESTOCK", "rec": rec})
            elif old.get("price") != rec["price"] and rec["price"] is not None:
                events.append({"type": "PRICE_CHANGE", "rec": rec, "old_price": old.get("price")})
            old.update(rec)
            old["first_seen"] = old.get("first_seen") or now
            known[sku] = old

    # 一覧から消えた = 売り切れ
    for sku, rec in known.items():
        if sku not in current and rec.get("in_stock"):
            rec["in_stock"] = False
            rec["sold_out_at"] = now
            if cfg.get("notify_sold_out"):
                events.append({"type": "SOLD_OUT", "rec": rec})
            else:
                log(f"sold out (no notify): {sku} {rec['title']}")
    return events


# ---------------------------------------------------------------------- email

def yen(v) -> str:
    return f"¥{v:,.0f}" if isinstance(v, (int, float)) else "-"


def build_email(cfg: dict, events: list[dict], now_disp: str) -> tuple[str, str]:
    """(subject, html_body) を返す。"""
    events = sorted(events, key=lambda e: EVENT_PRIORITY.index(e["type"]))
    top = events[0]
    rec = top["rec"]
    stars = "⭐⭐⭐ " if is_priority_color(cfg, rec) else ""
    label = {"RESTOCK": "再入荷！", "NEW": "新商品", "NEW_COLOR": "新カラー",
             "NEW_SIZE": "新サイズ", "PRICE_CHANGE": "価格変更", "SOLD_OUT": "売り切れ"}[top["type"]]
    subject = f"{stars}👜 {rec['model']} {label} {rec['title']}"
    if len(events) > 1:
        subject += f" ほか{len(events) - 1}件"

    cards = []
    for ev in events:
        r = ev["rec"]
        star = " ⭐" if is_priority_color(cfg, r) else ""
        price_html = yen(r["price"])
        if ev["type"] == "PRICE_CHANGE":
            price_html = f"<s>{yen(ev.get('old_price'))}</s> → <b>{yen(r['price'])}</b>"
        stock_html = "🟢 在庫あり（今すぐ購入可能）" if r["in_stock"] else "🔴 在庫なし"
        img_html = (
            f'<img src="{r["image"]}" width="280" style="display:block;border:1px solid #eee;border-radius:8px;" alt="">'
            if r["image"] else ""
        )
        cards.append(f"""
        <div style="border:1px solid #ddd;border-radius:12px;padding:20px;margin:16px 0;font-family:-apple-system,'Hiragino Sans',sans-serif;">
          <div style="font-size:13px;color:#b45309;font-weight:600;">{EVENT_LABELS[ev['type']]}{star}</div>
          <h2 style="margin:6px 0 12px;font-size:19px;">{r['title']}</h2>
          {img_html}
          <table style="font-size:14px;margin-top:12px;border-collapse:collapse;">
            <tr><td style="color:#666;padding:2px 14px 2px 0;">モデル</td><td>{r['model']}</td></tr>
            <tr><td style="color:#666;padding:2px 14px 2px 0;">カラー</td><td>{r['color'] or '-'}</td></tr>
            <tr><td style="color:#666;padding:2px 14px 2px 0;">サイズ</td><td>{r['size'] or '-'}</td></tr>
            <tr><td style="color:#666;padding:2px 14px 2px 0;">価格</td><td>{price_html}</td></tr>
            <tr><td style="color:#666;padding:2px 14px 2px 0;">在庫</td><td>{stock_html}</td></tr>
          </table>
          <p style="margin:14px 0 0;">
            <a href="{r['url']}" style="background:#f37021;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:14px;display:inline-block;">🔗 商品ページを開く</a>
          </p>
        </div>""")

    body = f"""
    <div style="max-width:520px;margin:0 auto;font-family:-apple-system,'Hiragino Sans',sans-serif;">
      <h1 style="font-size:16px;color:#333;">👜 Hermès Japan Alert</h1>
      {''.join(cards)}
      <p style="font-size:12px;color:#999;">検知日時：{now_disp} JST ／ 素材などの詳細は商品ページでご確認ください。</p>
    </div>"""
    return subject, body


def read_secret(name: str) -> str:
    """環境変数 → .env の順で秘密情報を読む。"""
    val = os.environ.get(name, "")
    if not val:
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith(name + "="):
                    val = line.split("=", 1)[1].strip().strip('"')
    return val


def send_email(cfg: dict, subject: str, html_body: str, to_addr: str | None = None) -> None:
    """宛先・送信元は環境変数から読む（このリポジトリは公開のため設定に書かない）。"""
    smtp_cfg = cfg["smtp"]
    user = read_secret(smtp_cfg["user_env"])
    password = read_secret(smtp_cfg["password_env"])
    recipient = to_addr or read_secret(smtp_cfg["recipient_env"])
    missing = [n for n, v in (
        (smtp_cfg["user_env"], user),
        (smtp_cfg["password_env"], password),
        (smtp_cfg["recipient_env"], recipient),
    ) if not v]
    if missing:
        raise RuntimeError(f"メール設定が未設定です: {', '.join(missing)}（.env か環境変数に設定）")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Hermès Watch", user))
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


# ------------------------------------------------------------------------ LINE

LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


def build_line_messages(cfg: dict, events: list[dict], now_disp: str) -> list[dict]:
    """Flex Message を組み立てる（LINEは1回あたり最大5メッセージ）。"""
    events = sorted(events, key=lambda e: EVENT_PRIORITY.index(e["type"]))
    messages = []
    for ev in events[:5]:
        r = ev["rec"]
        star = "⭐⭐⭐ " if is_priority_color(cfg, r) else ""
        label = EVENT_LABELS[ev["type"]]
        price = yen(r["price"])
        if ev["type"] == "PRICE_CHANGE":
            price = f"{yen(ev.get('old_price'))} → {yen(r['price'])}"
        rows = [
            ("カラー", r["color"] or "-"),
            ("サイズ", r["size"] or "-"),
            ("価格", price),
            ("在庫", "🟢 購入可能" if r["in_stock"] else "🔴 在庫なし"),
        ]
        body_contents = [
            {"type": "text", "text": f"{star}{label}", "size": "sm",
             "color": "#B45309", "weight": "bold", "wrap": True},
            {"type": "text", "text": r["title"], "size": "lg",
             "weight": "bold", "wrap": True, "margin": "sm"},
            {"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs",
             "contents": [
                 {"type": "box", "layout": "baseline", "contents": [
                     {"type": "text", "text": k, "size": "sm", "color": "#888888", "flex": 2},
                     {"type": "text", "text": v, "size": "sm", "flex": 5, "wrap": True},
                 ]} for k, v in rows
             ]},
        ]
        bubble = {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "contents": body_contents},
            "footer": {"type": "box", "layout": "vertical", "contents": [
                {"type": "button", "style": "primary", "color": "#F37021",
                 "action": {"type": "uri", "label": "商品ページを開く", "uri": r["url"]}},
                {"type": "text", "text": f"検知 {now_disp}", "size": "xxs",
                 "color": "#AAAAAA", "align": "center", "margin": "sm"},
            ]},
        }
        if r["image"]:
            bubble["hero"] = {"type": "image", "url": r["image"],
                              "size": "full", "aspectRatio": "1:1", "aspectMode": "cover"}
        messages.append({
            "type": "flex",
            "altText": f"{star}👜 {r['model']} {label} {r['title']}",
            "contents": bubble,
        })
    return messages


def send_line(cfg: dict, messages: list[dict]) -> None:
    line_cfg = cfg.get("line", {})
    if not line_cfg.get("enabled"):
        return
    token = read_secret(line_cfg.get("token_env", "LINE_CHANNEL_TOKEN"))
    if not token:
        raise RuntimeError(
            f"LINEトークン未設定: 環境変数 {line_cfg.get('token_env')} か .env に設定してください"
        )
    payload = json.dumps({"messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        LINE_BROADCAST_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"LINE broadcast failed: HTTP {resp.status}")


def send_line_text(cfg: dict, text: str) -> None:
    """障害アラート等のプレーンテキストをLINEに流す。"""
    send_line(cfg, [{"type": "text", "text": text}])


def email_enabled(cfg: dict) -> bool:
    """メールは任意。パスワードが無ければ黙って使わない（LINEが主経路のため）。"""
    if not cfg.get("smtp", {}).get("enabled", True):
        return False
    return bool(read_secret(cfg["smtp"]["password_env"]))


# ----------------------------------------------------------------------- main

def run_once(cfg: dict, dry_run: bool) -> None:
    state = load_state()
    now = datetime.now(JST).isoformat()
    now_disp = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

    try:
        current: dict[str, dict] = {}
        total_listed = 0
        last_cache: dict = {}
        # 複数のカテゴリページを見る。各ページのCDNキャッシュは独立に期限切れするため、
        # 同じ商品を含むページを複数見ると「どれかが先に更新される」＝実効遅延が縮む。
        # 在庫は和集合で判定する（どこかに載っていれば購入可能）。
        use_paid = cfg.get("scrapfly", {}).get("enabled") and _in_paid_window(cfg)
        urls = cfg["category_urls"]
        if use_paid:
            # 有料時は1回30クレジット。複数ページを叩くと費用が倍々になるので先頭だけに絞る。
            # 先頭はレディスバッグ全件ページで、監視対象（Picotin/Constance/Lindy）は必ずここに載る。
            # 無料経路の「複数ページでキャッシュのズレを稼ぐ」工夫は、
            # キャッシュを外せる有料経路では不要（そもそもリアルタイムが取れる）。
            urls = urls[:cfg["scrapfly"].get("url_limit", 1)]
        for url in urls:
            if use_paid:
                try:
                    html, cache = fetch_via_scrapfly(cfg, url)
                    if state.get("scrapfly_fail_streak"):
                        log(f"scrapfly recovered after {state['scrapfly_fail_streak']} failures")
                    state["scrapfly_fail_streak"] = 0
                    state.pop("scrapfly_alerted", None)
                except Exception as se:
                    # 有料経路が落ちても監視は止めない。無料のキャッシュ経路に退避する。
                    streak = state.get("scrapfly_fail_streak", 0) + 1
                    state["scrapfly_fail_streak"] = streak
                    log(f"scrapfly failed ({streak}回連続), falling back to direct: {se}")
                    # 一時的なタイムアウト1回でLINEを鳴らさない（深夜3時に家族へ⚠️が
                    # 飛んだ実績あり・2026-08-15）。3回連続＝クレジット切れ等の持続的
                    # 障害と判断した時だけ、1度警告する。
                    if streak >= 3 and not state.get("scrapfly_alerted") and not dry_run:
                        try:
                            send_line_text(
                                cfg,
                                "⚠️ Hermès Watch: 有料経路(ScrapFly)が3回連続で失敗し、"
                                "無料経路に切り替えています。\n"
                                "クレジット切れの可能性があります。監視は続いていますが、"
                                "検知が最大1時間ほど遅くなります。\n\n"
                                f"エラー: {se}",
                            )
                            state["scrapfly_alerted"] = True
                        except Exception as le:
                            log(f"scrapfly fallback alert failed: {le}")
                    html, cache = fetch_html(url)
            else:
                html, cache = fetch_html(url)
            record_freshness({**cache, "url": url})
            # 最も新しい（age最小の）ページを鮮度の代表値とする
            if not last_cache or _age_sec(cache) < _age_sec(last_cache):
                last_cache = cache
            items = parse_products(html)
            total_listed += len(items)
            for item in items:
                model = match_model(cfg, item)
                if model:
                    current[item["sku"]] = to_record(cfg, item, model, now)
            time.sleep(2)  # 複数カテゴリ監視時の間隔
    except (BlockedError, OSError) as e:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        log(f"FETCH FAILED ({state['failure_streak']}回連続): {e}")
        limit = cfg.get("failure_alert_after", 12)
        if state["failure_streak"] >= limit and not state.get("failure_alerted"):
            if not dry_run:
                # LINEを主経路にする。メールだけに頼るとメール未設定時に
                # 「壊れたまま誰も気づかない」状態になるため。
                alerted = False
                try:
                    send_line_text(
                        cfg,
                        f"⚠️ Hermès Watch が停止している可能性があります\n\n"
                        f"{state['failure_streak']}回連続で在庫の取得に失敗しました。"
                        f"ブロックまたはサイト構造の変更が疑われます。\n\n"
                        f"最終エラー: {e}",
                    )
                    alerted = cfg.get("line", {}).get("enabled", False)
                except Exception as le:
                    log(f"failure alert LINE failed: {le}")
                if email_enabled(cfg):
                    try:
                        send_email(
                            cfg,
                            "⚠️ Hermès Watch 停止中の可能性",
                            f"<p>{state['failure_streak']}回連続で取得に失敗しています。"
                            f"ブロックまたはサイト構造変更の可能性。<br>最終エラー: {e}</p>",
                            to_addr=read_secret(cfg["smtp"]["failure_alert_env"]) or None,
                        )
                        alerted = True
                    except Exception as me:
                        log(f"failure alert mail failed: {me}")
                # 通知できなかった場合はフラグを立てない＝次回も再通知を試みる
                state["failure_alerted"] = alerted
        if not dry_run:
            save_state(state)
        return

    if state.get("failure_streak"):
        log(f"recovered after {state['failure_streak']} failures")
    state["failure_streak"] = 0
    state["failure_alerted"] = False

    # 「初回か」は監視対象の在庫有無ではなく、一度でも取得に成功したかで判定する。
    # state["products"] を基準にすると、監視対象が品切れの間ずっと初回扱いになり、
    # 待ち望んでいた最初の再入荷を握り潰してしまう（2026-08-13 に実測で発覚）。
    first_run = not state.get("initialized") and not state["products"]
    state["initialized"] = True
    events = diff(cfg, state, current, now)

    log(f"listed:{total_listed} matched:{len(current)} events:{len(events)} "
        f"age:{last_cache.get('age')} cache:{last_cache.get('cf_cache_status')} "
        f"lm:{last_cache.get('last_modified')}")

    if first_run and events:
        # 初回はベースライン記録のみ（既掲載品を「新商品」と誤通知しない）
        log(f"first run: seeded {len(events)} items as baseline, no notification")
        events = []

    if events:
        subject, body = build_email(cfg, events, now_disp)
        line_msgs = build_line_messages(cfg, events, now_disp)

        if dry_run:
            log(f"[DRY-RUN] subject: {subject}")
            (BASE_DIR / "logs" / "last_email.html").write_text(body, encoding="utf-8")
            (BASE_DIR / "logs" / "last_line.json").write_text(
                json.dumps({"messages": line_msgs}, ensure_ascii=False, indent=1), encoding="utf-8")
            log("[DRY-RUN] -> logs/last_email.html, logs/last_line.json")
        else:
            # 有効な経路だけを独立に送る。片方が失敗しても他方は届かせる。
            channels = []
            if cfg.get("line", {}).get("enabled"):
                channels.append(("LINE", lambda: send_line(cfg, line_msgs)))
            if email_enabled(cfg):
                channels.append(("mail", lambda: send_email(cfg, subject, body)))
            if not channels:
                log("通知経路が1つも有効になっていません（LINEかメールを設定してください）")

            delivered = False
            for name, fn in channels:
                try:
                    fn()
                    delivered = True
                    log(f"{name} sent: {subject}")
                except Exception as e:
                    log(f"{name} FAILED: {e}")
            if not delivered:
                # 全経路失敗 → state を保存せず次回に同じイベントを再検知させる
                log("all channels FAILED (state未保存・次回リトライ)")
                return
        for ev in events:
            log(f"  event: {ev['type']} {ev['rec']['sku']} {ev['rec']['title']}")

    # 【dry-run では state を保存しない（2026-08-23）】
    # 以前は if/else の外にあったため --dry-run でも state が前進していた。
    # ローカル検証のつもりで走らせた結果を誤ってcommitすると、CIが「通知済み」と
    # 誤認して本物の再入荷を奥様に通知しなくなる。dry-run は副作用ゼロを保証する。
    if dry_run:
        log("[DRY-RUN] state は保存しない")
        return

    save_state(state)


def freshness_report() -> None:
    """freshness.jsonl から「Hermèsが実際に何分ごとにデータを更新しているか」を集計。

    last-modified が変わった間隔＝オリジンの実更新間隔。
    age の最大値＝最悪どれだけ古いデータを見せられるか。
    この2つが有料API（ScrapFly等）に課金すべきかの判断材料になる。
    """
    if not FRESHNESS_PATH.exists():
        print("まだ実測データがありません。監視を数時間動かしてから再実行してください。")
        return
    rows = [json.loads(l) for l in FRESHNESS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        print("実測データが空です。")
        return

    ages = [int(r["age"]) for r in rows if r.get("age") and str(r["age"]).isdigit()]
    # last-modified の変化点を検出
    changes, prev_lm, prev_t = [], None, None
    for r in rows:
        lm = r.get("last_modified")
        if lm and lm != prev_lm:
            t = datetime.fromisoformat(r["checked_at"])
            if prev_t is not None:
                changes.append((t - prev_t).total_seconds() / 60)
            prev_lm, prev_t = lm, t

    span = ""
    if len(rows) >= 2:
        t0 = datetime.fromisoformat(rows[0]["checked_at"])
        t1 = datetime.fromisoformat(rows[-1]["checked_at"])
        span = f"（{(t1 - t0).total_seconds() / 3600:.1f}時間分）"

    print(f"=== Hermès キャッシュ鮮度 実測レポート ===")
    print(f"観測回数: {len(rows)}回 {span}")
    if ages:
        print(f"\n■ データの古さ（age）")
        print(f"  最小 {min(ages) // 60}分 / 中央 {sorted(ages)[len(ages) // 2] // 60}分 / "
              f"最大 {max(ages) // 60}分")
        print(f"  → 最悪の場合 {max(ages) // 60}分 古いデータを見ている")
    print(f"\n■ オリジンの実更新間隔（last-modified の変化）")
    if changes:
        print(f"  更新検出 {len(changes)}回")
        print(f"  最短 {min(changes):.0f}分 / 中央 {sorted(changes)[len(changes) // 2]:.0f}分 / "
              f"最長 {max(changes):.0f}分")
        median = sorted(changes)[len(changes) // 2]
        print(f"\n■ 判定")
        if median <= 10:
            print(f"  ✅ 約{median:.0f}分ごとに更新されている → 無料構成で十分。課金不要")
        elif median <= 20:
            print(f"  🟡 約{median:.0f}分ごと → 無料構成でも実用範囲。課金は好みの問題")
        else:
            print(f"  🔴 約{median:.0f}分ごと → 人気色は取り逃す可能性。ScrapFly課金の検討価値あり")
    else:
        print("  まだ更新を検出していません（データ不足 or 更新が非常に遅い）")
        print("  → もう少し観測を続けてください")


def run_loop(cfg: dict) -> None:
    """--loop: 1プロセスで予算時間だけ run_once を繰り返す（GitHub Actions用）。

    背景: GitHubは高頻度cron（*/5等）を混雑時に間引くため、cron起動だけに頼ると
    実測で45〜65分間隔まで劣化した（2026-08-14）。cronは約30分ごとの起動トリガーに
    格下げし、監視間隔の管理はここで行う。

    間隔・時間帯・予算の正本は config.json の "loop" セクション（workflowやREADMEに
    数値を直書きしない）。クレジット試算もこの値から導出する。
    """
    lp = cfg.get("loop", {})
    budget = lp.get("budget_seconds", 1680)
    night_hours = set(lp.get("night_hours", [0, 1, 2, 3, 4, 5, 6, 7, 8]))
    night_iv = lp.get("night_interval_sec", 300)
    day_iv = lp.get("day_interval_sec", 900)
    # 次のイテレーションを開始してよいかの安全マージン。
    # 最悪ケース ＝ ScrapFlyタイムアウト ＋ 無料経路フォールバック・通知送信のぶん。
    margin = cfg.get("scrapfly", {}).get("timeout", 90) + 90

    start = time.monotonic()
    total = failures = 0
    while True:
        iter_start = time.monotonic()
        total += 1
        try:
            run_once(cfg, dry_run=False)
        except Exception:
            # BlockedError/OSError は run_once 内で処理済み。ここに来るのは想定外の
            # バグ（構造変更・設定破損等）。ループは続けるが件数を数えておき、
            # 全滅ならジョブを失敗させて外部（GitHubの失敗通知）に見えるようにする。
            failures += 1
            log(f"iteration crashed:\n{traceback.format_exc()}")

        interval = night_iv if datetime.now(JST).hour in night_hours else day_iv
        sleep_s = max(0, interval - (time.monotonic() - iter_start))
        if time.monotonic() - start + sleep_s + margin >= budget:
            break
        time.sleep(sleep_s)

    log(f"loop done: {total} iterations, {failures} crashed")
    if total and failures == total:
        # 1回も正常に動けなかった＝構造的な故障。exit 1 で run を赤くする
        sys.exit(1)


def scrapfly_proof(cfg: dict) -> None:
    """課金前の実証: 有料経路が本当に「キャッシュを外した新鮮なデータ」を返すか確かめる。

    無料経路と有料経路を同じURLに対して撃ち、age と last-modified を比べる。
    有料経路が BYPASS/MISS かつ age が小さければ、鮮度改善は本物。
    """
    url = cfg["category_urls"][0]
    print("=== 有料経路の実証（課金前の確認） ===\n")

    html_a, cache_a = fetch_html(url)
    n_a = len(parse_products(html_a))
    print("【無料経路（CDNキャッシュ）】")
    print(f"  cache={cache_a.get('cf_cache_status')} age={cache_a.get('age')}秒 "
          f"({int(cache_a.get('age') or 0)//60}分古い)")
    print(f"  last-modified={cache_a.get('last_modified')}  商品数={n_a}\n")

    sf = dict(cfg.get("scrapfly", {}))
    sf["enabled"] = True
    probe_cfg = {**cfg, "scrapfly": sf}
    try:
        html_b, cache_b = fetch_via_scrapfly(probe_cfg, url)
    except Exception as e:
        print(f"【有料経路】失敗: {e}")
        print("\n→ APIキーを .env の SCRAPFLY_API_KEY に設定してから再実行してください")
        return

    try:
        n_b = len(parse_products(html_b))
    except BlockedError as e:
        print(f"【有料経路】ブロックされました: {e}")
        print("\n→ 鮮度改善は見込めない。課金しないこと")
        return

    age_b = int(cache_b.get("age") or 0)
    print("【有料経路（ScrapFly・キャッシュバスター付き）】")
    print(f"  cache={cache_b.get('cf_cache_status')} age={age_b}秒 ({age_b//60}分古い)")
    print(f"  last-modified={cache_b.get('last_modified')}  商品数={n_b}")
    print(f"  消費クレジット={cache_b.get('cost')}\n")

    print("■ 判定")
    improve = int(cache_a.get("age") or 0) - age_b
    if n_b == 0:
        print("  ❌ 商品が取れていない → 課金しない")
    elif age_b <= 300:
        print(f"  ✅ ほぼリアルタイム（{age_b//60}分）。無料経路より {improve//60}分 新しい → 課金する価値あり")
    elif improve > 600:
        print(f"  🟡 {improve//60}分 改善。効果はあるが完全なリアルタイムではない")
    else:
        print(f"  ❌ 改善 {improve//60}分 のみ＝同じキャッシュを見ている → 課金しても無駄")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1回だけ実行（launchd用）")
    ap.add_argument("--loop", action="store_true",
                    help="config.loop の設定に従い予算時間ぶんループ実行（GitHub Actions用）")
    ap.add_argument("--dry-run", action="store_true", help="メールを送らず内容をログに出す")
    ap.add_argument("--no-jitter", action="store_true", help="開始時のランダム待機をしない")
    ap.add_argument("--test-email", action="store_true", help="テストメールを送って終了")
    ap.add_argument("--test-line", action="store_true", help="テストLINEを送って終了")
    ap.add_argument("--test-scrapfly", action="store_true",
                    help="有料経路が本当に鮮度を上げるか検証（課金前の実証用）")
    ap.add_argument("--freshness-report", action="store_true",
                    help="キャッシュ鮮度の実測結果を集計して表示")
    ap.add_argument("--self-test", action="store_true",
                    help="監視キーワードが公式商品名に一致するか検証（通信なし・CIで実行）")
    args = ap.parse_args()

    cfg = load_config()

    if args.self_test:
        self_test(cfg)
        return

    if args.freshness_report:
        freshness_report()
        return

    if args.test_scrapfly:
        scrapfly_proof(cfg)
        return

    if args.test_email:
        send_email(cfg, "✅ Hermès Watch テストメール",
                   f"<p>通知システムのテストです。{datetime.now(JST).strftime('%Y/%m/%d %H:%M')} JST</p>")
        print("test email sent")
        return

    if args.test_line:
        send_line(cfg, [{"type": "text",
                         "text": f"✅ Hermès Watch テスト通知\n{datetime.now(JST).strftime('%Y/%m/%d %H:%M')} JST"}])
        print("test LINE broadcast sent")
        return

    if args.loop:
        run_loop(cfg)
        return

    if not args.no_jitter and not args.dry_run:
        time.sleep(random.uniform(0, cfg.get("jitter_seconds", 45)))

    run_once(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
