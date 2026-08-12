# Hermès Bag Alert 👜

Hermès日本公式オンラインショップのバッグ在庫を監視し、**新商品・再入荷・新カラー・新サイズ・価格変更**を検知してLINEとメールで通知するツール。

GitHub Actions で **24時間・5分間隔**で動く。個人の在庫確認を自動化する目的で、リクエストは1回につきカテゴリページ1枚のみ。

> 📌 **設定に個人情報を書かないこと。** 宛先メール・トークン類はすべて環境変数（ローカルは `.env`、CIは GitHub Secrets）から読む。

## 何を監視しているか

`config.json` の `watch_models` で指定したモデル。既定は **Picotin / Constance / Lindy**。

追加はキーワードを1行足すだけ：

```json
{ "model": "Kelly", "keywords": ["ケリー", "kelly"] }
```

`priority_colors` に載っている色（Etoupe・Gold・Noir 等）が出ると通知の件名に ⭐⭐⭐ が付く。

## 仕組み

```
Hermèsカテゴリページ（レディスバッグ）
  └─ HTML内の <script id="hermes-state"> にSSRの商品JSONが埋まっている
       → sku / title / avgColor / size / price / 画像URL / stock.ecom が取れる
  └─ 一覧は「在庫あり商品のみ」掲載（クエリに has_stock フィルタ）
       → SKUの出現 ＝ 買える状態になった（新商品 or 再入荷）
       → SKUの消滅 ＝ 売り切れ
  └─ state/products.json と差分を取り、変化があった時だけ通知（重複通知を防ぐ）
```

### 調査で確定した制約（2026-08-12 実測）

| 経路 | 結果 |
|---|---|
| **カテゴリページ** | ✅ 素朴なGETで取得可。UA不問（curl/Googlebot/UA無しでも通る）。GitHub ActionsのIPからも通過を確認 |
| 検索ページ `/search/?s=...` | ❌ DataDomeブロック |
| 商品詳細ページ | ❌ Cloudflareブロック（**素材はメールに載せられない** → 商品ページリンクで代替） |
| バックエンドAPI `bck.hermes.com` | ❌ ブロック |

### ⚠️ 鮮度の限界（重要）

カテゴリページは Cloudflare CDN で **`max-age=3600`（最大1時間）キャッシュ**される。キャッシュを外す方法（URLにクエリ付与）は DataDome が即ブロックするため、**本物のブラウザでも新鮮なデータを取得できない**。

つまり **検知の遅延はポーリング間隔ではなくHermès側のキャッシュで決まる**。実測では `age` が5〜28分程度だった。

実際の更新間隔は測定して確認できる：

```bash
python3 hermes_monitor.py --freshness-report
```

これが遅すぎる場合は、住宅用プロキシ系のスクレイピングAPI（ScrapFly等・月$30〜）を挟む選択肢がある。

## セットアップ

### 1. LINE通知（推奨・移動中でも気づける）

1. [LINE Developers](https://developers.line.biz/console/) にログイン
2. 新規プロバイダー作成 → **Messaging API** チャネルを作成
3. 「Messaging API設定」タブ → **チャネルアクセストークン（長期）** を発行してコピー
4. 同じタブのQRコードから、通知を受け取る人が**Botを友だち追加**
5. 「応答メッセージ」はオフにしておくと静か
6. `.env` に設定し、`config.json` の `line.enabled` を `true` に：
   ```
   LINE_CHANNEL_TOKEN=発行したトークン
   ```
7. テスト送信： `python3 hermes_monitor.py --test-line`

### 2. メール通知

1. 送信元にするGmailで [アプリパスワード](https://myaccount.google.com/apppasswords) を発行
   （2段階認証が未設定なら先に[セキュリティ設定](https://myaccount.google.com/security)から有効化）
2. `.env` に記入：
   ```
   HERMES_SMTP_USER=送信元@gmail.com
   HERMES_SMTP_PASSWORD=アプリパスワード16文字（スペースなし）
   HERMES_RECIPIENT=通知先@gmail.com
   HERMES_FAILURE_ALERT_TO=障害警告の宛先@gmail.com
   ```
3. テスト送信： `python3 hermes_monitor.py --test-email`
4. 受信側のGmailで、`Hermès Watch` からのメールに**フィルタ＋スター＋重要マーク**を設定しておくと埋もれにくい

### 3. GitHub Actions（24時間監視）

リポジトリの Settings → Secrets and variables → Actions に登録：

| Secret | 内容 |
|---|---|
| `LINE_CHANNEL_TOKEN` | LINEチャネルアクセストークン |
| `HERMES_SMTP_USER` | 送信元Gmailアドレス |
| `HERMES_SMTP_PASSWORD` | Gmailアプリパスワード |
| `HERMES_RECIPIENT` | 通知先メールアドレス |
| `HERMES_FAILURE_ALERT_TO` | 障害警告の宛先 |

登録すれば5分間隔で自動監視が始まる。手動実行は Actions タブ → `hermes-monitor` → Run workflow。

## 運用コマンド

```bash
# 1回だけ実行（通知を送らず内容だけ確認）
python3 hermes_monitor.py --once --dry-run --no-jitter

# 通知テスト
python3 hermes_monitor.py --test-line
python3 hermes_monitor.py --test-email

# キャッシュ鮮度の実測レポート
python3 hermes_monitor.py --freshness-report

# ログ
tail -20 logs/monitor.log
```

### ローカルでも動かす場合（任意）

`com.tatsumi.hermes-bag-alert.plist` を `~/Library/LaunchAgents/` に置いて `launchctl load` すると5分間隔で動く。
ただし **Macがスリープすると止まる**ため、24時間監視はGitHub Actions側が担う。

両方動かすと同じ入荷で**通知が最大2通**届く（それぞれ独立にstateを持つため）。
不要なら `launchctl unload ~/Library/LaunchAgents/com.tatsumi.hermes-bag-alert.plist` で止める。

## 障害時の動き

- 取得失敗（ブロック・ネットワーク断）はログに記録して静かに終了（通知を乱発しない）
- **12回連続失敗で `HERMES_FAILURE_ALERT_TO` に警告メール1通**
- 通知が全経路失敗した場合は state を保存せず終了 → 次回に同じイベントを再検知して再送
- LINEとメールは**独立して送信**するので、片方が落ちても他方は届く

## ファイル構成

| ファイル | 役割 |
|---|---|
| `hermes_monitor.py` | 本体（取得・差分判定・通知） |
| `config.json` | 監視モデル・優先カラー（**個人情報は書かない**） |
| `state/products.json` | 既知SKUと在庫状態。重複通知防止の要 |
| `logs/freshness.jsonl` | キャッシュ鮮度の実測データ |
| `.github/workflows/monitor.yml` | 24時間監視のスケジュール |

## 免責

個人の在庫確認を自動化する目的のツール。Hermès に過剰な負荷をかけないよう、1回の実行につきカテゴリページを1枚取得するだけに留めている。転売・買い占め等の用途は想定していない。
