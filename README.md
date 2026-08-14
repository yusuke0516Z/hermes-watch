# Hermès Bag Alert 👜

Hermès日本公式オンラインショップのバッグ在庫を監視し、**新商品・再入荷・新カラー・新サイズ・価格変更**を検知してLINEとメールで通知するツール。

GitHub Actions で **24時間**動く（深夜は5分・日中は15分間隔。正本は `config.json` の `loop`）。個人の在庫確認を自動化する目的で、リクエストは1回につきカテゴリページ1枚のみ。

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

つまり **検知の遅延はポーリング間隔ではなくHermès側のキャッシュで決まる**。5分間隔にしても2分間隔にしても、エッジが同じキャッシュを返す限り差は出ない。

**23.3時間・99回の実測結果（2026-08-13）**：

| 指標 | 実測値 |
|---|---|
| データの古さ（`age`） | 最小0分 / **中央31分** / 最大60分 |
| オリジンの実更新間隔 | 最短29分 / **中央60分** / 最長146分 |

→ **平均31分・最悪1時間遅れて気づく**ことになる。人気色が数分で消える場合は取り逃す。

最新の実測は随時確認できる：

```bash
python3 hermes_monitor.py --freshness-report
```

#### 鮮度を上げる手段（実測で分かったこと）

**❌ 効かないと確認済み**

| 試したこと | 結果 |
|---|---|
| ポーリング間隔を詰める（2分・5分） | 同じキャッシュを見るだけで無意味 |
| URLにクエリを付けてキャッシュ回避 | `BYPASS` になるがDataDomeが即ブロック |
| パス変種（`//`・`/./`・`/index.html`） | 前2つは同じキャッシュに正規化。最後はブロック |
| 本物のブラウザで直接アクセス | JSチャレンジが解けず取得不可 |
| 住宅用プロキシで同じURLを叩く | **キャッシュはCDN側で共有されるため無意味**（誰が叩いても同じ古いコピー） |

**✅ 効く手段（実装済み）**

1. **複数カテゴリページの併用（無料）**
   各ページのCDNキャッシュは**独立に期限切れする**（実測: 46分/42分/37分/33分とバラバラ）。
   同じ商品を含むページを複数見て和集合を取れば、どれかが先に更新される分だけ実効遅延が縮む。
   `category_urls` に2件設定済み。**追加できるのは全件掲載（`total <= items`）のページのみ** —
   広いカテゴリは48件で打ち切られるため監視に使えない。

2. **ScrapFly経由（有料・`scrapfly.enabled`）**
   本質は住宅IPではなく **キャッシュを外せること**。クエリ付きURLで `BYPASS` にしてオリジンまで
   届かせ、DataDomeを `asp` で突破する。これが唯一リアルタイムに近づく方法。

   ```bash
   # 課金前に、本当に鮮度が上がるか実証する
   python3 hermes_monitor.py --test-scrapfly
   ```

   **実証済み（2026-08-13）**:

   | | 無料経路 | 有料経路 |
   |---|---|---|
   | cf-cache-status | HIT | **MISS** |
   | データの古さ | 54分前 | **0分（リアルタイム）** |
   | 商品取得 | 21件 | 21件 |

   実コストは **30クレジット/回**（住宅IP 25 + JS描画 5。`asp` が描画を伴うため
   `render_js: false` でも描画分の5クレジットは発生する）。

   運用設定（2026-08-13 $30プラン契約後）: **全時間帯を有料経路・24時間リアルタイム監視**。
   間隔・時間帯・予算・クレジット試算の**正本は `config.json` の `loop` セクション**
   （GitHubが高頻度cronを間引くため、cronは約30分ごとの起動トリガーに格下げし、
   実際の監視間隔は `--loop` がプロセス内で管理する）。間隔を詰める時は必ず
   `loop._cost_note` の試算をやり直すこと。

   有料経路が落ちても（クレジット切れ含む）**自動的に無料経路へフォールバック**するので
   監視は止まらない。フォールバックが起きた時は**LINEに1回だけ警告**が届く
   （「検知が最大1時間遅くなります」）。回復すると次のフォールバックでまた1回通知される。

## セットアップ

### 1. LINE通知（推奨・移動中でも気づける）

> ⚠️ **2024年9月に作成フローが変わった。** 古い記事の「LINE Developersで直接チャネル作成」は現在使えない。
> 先に**LINE公式アカウント**を作り、そこからMessaging APIを有効化する。

1. [LINE Official Account Manager](https://manager.line.biz/) にLINEアカウントでログイン → アカウントを作成
2. 「設定」→「Messaging API」→ **「Messaging APIを利用する」** をクリック
   （プロバイダー名は**後から変更できない**ので慎重に）
3. [LINE Developers](https://developers.line.biz/console/) を開く → 該当チャネル → 「Messaging API設定」タブ
   → 最下部の **チャネルアクセストークン（長期）** を発行してコピー（正本は1Passwordへ）
4. manager.line.biz の「応答設定」で **応答メッセージ・あいさつメッセージをオフ**（静かにする）
5. 通知を受け取る人が **Botを友だち追加**（`https://line.me/R/ti/p/@ベーシックID`）
   **追加していない人には届かない**ので必須
6. `.env` に設定し、`config.json` の `line.enabled` を `true` に：

   ```bash
   LINE_CHANNEL_TOKEN=発行したトークン
   ```

7. テスト送信： `python3 hermes_monitor.py --test-line`

#### リッチメニュー（トーク画面下部のメニュー）

```bash
python3 setup_richmenu.py          # 作成・画像アップロード・全員に適用
python3 setup_richmenu.py --list   # 登録状況の確認
python3 setup_richmenu.py --clean  # 全削除
```

画像は `assets/richmenu.html` を1250×843で撮影して `assets/richmenu.png`（2500×1686）にしたもの。
デザインを変えたい時はHTMLを編集 → 同じサイズで撮り直し → `setup_richmenu.py` を再実行。

> リンク先には **クエリ付きURL（`?facet_line=...` 等）を使わないこと。** DataDomeにブロックされ、
> タップした人にキャプチャ画面が出る。プレーンなカテゴリURLのみ採用している。

#### アカウントのプロフィール画像

`assets/icon.png`（640×640）を用意してあるが、**プロフィール画像はAPIで設定できない**。
manager.line.biz →「設定」→「アカウント設定」から手動でアップロードする。

### 2. メール通知（任意）

**LINEが主経路。メールは無くても動く。** 使わない場合は `config.json` の `smtp.enabled` を
`false` のままにするか、`HERMES_SMTP_PASSWORD` を設定しなければ自動的にスキップされる。

> 障害アラート（12回連続で取得失敗）は**LINEにも流れる**ので、メールを使わなくても
> システムが止まったことに気づける。

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

登録すれば自動監視が始まる（起動は約30分ごと・各起動が `--loop` で予算時間ぶん監視）。手動実行は Actions タブ → `hermes-monitor` → Run workflow（dry_run にチェックで1回だけ安全に確認）。

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
| `setup_richmenu.py` | LINEリッチメニューの作成・適用 |
| `assets/icon.html` / `icon.png` | アカウントアイコン（640×640・手動アップロード用） |
| `assets/richmenu.html` / `richmenu.png` | リッチメニュー画像（2500×1686） |
| `state/products.json` | 既知SKUと在庫状態。重複通知防止の要 |
| `logs/freshness.jsonl` | キャッシュ鮮度の実測データ |
| `.github/workflows/monitor.yml` | 24時間監視のスケジュール |

## 免責

個人の在庫確認を自動化する目的のツール。Hermès に過剰な負荷をかけないよう、1回の実行につきカテゴリページを1枚取得するだけに留めている。転売・買い占め等の用途は想定していない。
