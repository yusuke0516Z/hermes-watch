# hermes-bag-alert セッション

このディレクトリで起動するセッションは、Hermès バッグ在庫監視システム（奥様向け・個人用）の専用セッション。
**システム全体の正本は README.md**——構成・鮮度の物理・ScrapFly・運用履歴はまずそこを読む。

## 30秒サマリー

- Hermès日本公式の在庫を24時間監視し、新商品・再入荷・価格変更をLINE通知（奥様＋辰巳さん）
- 実行基盤は **GitHub Actions**（repo: `yusuke0516Z/hermes-watch`・public）。Macは不要。ローカルは開発・点検のみ
- 毎時cron → `hermes_monitor.py --loop` が170分走行（JST 0-8時=5分間隔／日中=15分間隔。正本は `config.json` の `loop`）
- 取得は ScrapFly 有料経路（キャッシュバスターでリアルタイム・30クレジット/回・月枠200,000）。失敗時は無料キャッシュ経路へ自動フォールバック
- 自己運用: 日次ヘルスLINE（`daily-health.yml`・21:10 JST）＋ クラウド自動点検ルーチン（`trig_01NaHsrQ9Z2fPhYpSKbmoRAv`・21:30 JST・異常時のみ自己修正コミット）

## 監視対象の追加・変更

1. **公式サイトの商品名そのまま**を `config.json` の `keywords` に入れる（例: `ガーデン・パーティ`）
2. `hermes_monitor.py` の `SELF_TEST_FIXTURES` にその公式商品名を1件足す
3. `python3 hermes_monitor.py --self-test` が通ることを確認してから push

**英語キーワードは一致しない**（日本サイトは slug/url がパーセントエンコード日本語）。
**公式名は中黒区切り**（「ガーデン・パーティ」）。2026-08-23に中黒なしで登録して監視ゼロ＋
`matched:0` が「在庫なし」と区別できず気づけない、という事故を起こしている。手順3を飛ばさないこと。

**追加前に必ず現在の在庫数を確認する**（在庫ありの品が多いモデルを足すと初回に通知が飛ぶ。LINEは1回のbroadcastに最大5件バンドル）。
※ただし `matched:0` は「在庫なし」の証明にならない。まず `--self-test` で一致を担保してから在庫を見ること。

## 厳守ルール

- **リポジトリはpublic。** トークン・宛先アドレスをコード/config/コミットに絶対に書かない（.env / GitHub Secrets のみ。正本は1Password）
- push前に `git diff --cached --name-only | grep -q "^\.env$"` で .env 混入がないこと
- **テストLINE送信を気軽にしない**（奥様の実機に届く）。動作確認は `--dry-run`
- ScrapFlyクレジット予算を超える変更（間隔短縮・URL追加）は試算してから
- state（`state/products.json`）を手で書き換えない。CIとの競合は `merge_state.py` が解決する
- 仕事系（Notion/Slack/freee/クライアント）のツール・情報はこのセッションで扱わない（プライベート専用）

## よく使うコマンド

```bash
python3 hermes_monitor.py --once --dry-run --no-jitter   # 通知なしで1回実行
python3 hermes_monitor.py --freshness-report              # 鮮度実測の集計
python3 healthcheck.py --dry                              # ヘルスチェックをローカルで
gh run list --repo yusuke0516Z/hermes-watch --limit 10    # CI状況
```

ローカルで作業する前に `git pull origin main`（CIが state を随時コミットしているため、ほぼ常にリモートが先行している）。ローカルの `logs/` `state/` の差分は古い実行の残骸なので `git checkout --` で捨ててよい。

## 登録場所

- registry: `~/.claude/system_registry.yaml` の `automations.hermes_bag_alert`
- 変更履歴: `~/.claude/change_log.md`
