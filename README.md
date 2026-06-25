# auto-trading

bitbank 向けの自動売買システム。**「確実に儲ける」ではなく「損失をコントロールしながら期待値プラスを狙う」** ことを目的に、過去の分足を使ったバックテストでリスクパラメータを決め、週次・月次で再評価する。

## 大前提（安全のための不可侵ルール）

- **APIキーに「出金(withdraw)」権限を付けない。** 「資産参照」と「取引」のみ有効化する。
- キーはコードに直書きしない。環境変数 or シークレットで管理し、`.gitignore` 徹底（`.env`, `config.toml` は追跡しない）。
- 投入順序を必ず守る: **バックテスト → ペーパートレード → 少額ライブ**。いきなり本番資金を入れない。
- 自動発注はユーザー管理の常駐プロセスとして動かす。

## 評価方法論

- **ブロック・ブートストラップ**: 分足を1本ずつシャッフルしない（トレンド/自己相関が壊れる）。「ランダムな開始日から連続◯日」を多数サンプリングして局面の頑健性を測る。
- **モンテカルロ**: 多数の資産曲線を重ね、平均だけでなく分布（最悪5%・最大DD）を見る。
- **ウォークフォワード**: 学習窓で最適化 → 次の窓（未知データ）で検証 → 窓をずらして反復。インサンプルとアウトオブサンプルを必ず分ける。
- **過剰最適化検知**: 再評価ごとに最適パラメータの変動を記録。暴れたら警告。OOS成績が大きく劣化したら新パラメータを採用せず前回設定を維持。

## 評価指標

期待値(Expectancy) / シャープ・ソルティノ比 / 最大ドローダウン / プロフィットファクター。
単純利益ではなく **リスク調整後リターン** で比較する。

## アーキテクチャ

```
[1] data    : bitbank ローソク足API(1min) → ローカルキャッシュ(SQLite)。差分追記。
[2] backtest: 区間データ + パラメータ → 資産曲線・各種指標
[3] montecarlo: ランダム区間を N 本サンプリング → 指標の分布
[4] optimize: グリッド/ベイズ最適化。評価軸 = リスク調整後リターン
[5] walkforward: 週次/月次で再最適化 → 新パラメータを設定ファイルに書き出すだけ（実発注と疎結合）
[6] live    : Risk Manager(サイズ/損切り/サーキットブレーカ) + Executor(発注/約定確認)
[7] monitor : 損益・異常を通知。日次損失上限で新規発注停止。
```

## ロードマップ

- [x] [1] データ層: 1分足取得 → SQLite（差分追記・再取得スキップ）
- [x] [2] 最小バックテスタ（SMAクロス+ATR損切り+固定割合サイジング）
- [x] [3] モンテカルロ評価（ブロック・ブートストラップ。`src/backtest/montecarlo.py`）
- [x] [4] パラメータ最適化（時間足リサンプリング `src/data/resample.py` + `src/backtest/optimize.py`）
- [x] 戦略を差し替え可能に分離（`src/backtest/strategies.py`: sma_cross / rsi / bollinger）
- [x] データ拡張: 2年分(2022-06〜2024-06、2022下落相場を含む、110万本)
- [x] [5] ウォークフォワード再評価ループ（`src/backtest/walkforward.py`。test_days=30で月次/7で週次）
- [ ] Risk Manager + サーキットブレーカ
- [x] Risk Manager: 最大ポジション比率上限 + 日次損失サーキットブレーカ（`simulator.py`）
- [x] クロスペア検証（`src/backtest/validate.py`。設定を固定し他ペア×ランダム期間で信頼性を判定）
- [x] ペア別設定（`src/settings_store.py` + `src/backtest/tune.py`）。各ペア独立の設定＋OOS信頼性ゲートで enabled/disabled を自動判定。`state/pair_settings.json`
- [x] 定期再チューニング(機械的・LLM不要) `src/retune.py` + `scripts/retune.sh`（cron）。増分取得→全ペア再チューニング→差分ログ→review_neededフラグ
- [x] maker/taker 手数料モデル（`simulator.py` の order_type）。intraday採算の分岐点
- [x] 検証済み候補2系統: A=1h トレンド・月次・taker（OOS+16%/2年）/ B=5m RSI逆張り・maker（OOS 勝率81%・+10%/21ヶ月）
- [x] ペーパートレード基盤（`src/live/`）: 2口座(A_trend成行 / B_intraday指値)各¥100k、複利、指値約定率を実測、実発注なし
- [x] 日次Discord通知（`src/live/notify_discord.py`）: 評価額 + 1日/1週/2週 + 約定率
- [x] デプロイ: systemd常駐 + cron通知（`scripts/`）
- [ ] 1ヶ月運用 → 結果レビュー（実約定率でBの真偽を判定）
- [ ] （Bが生き残れば）少額ライブ
- [ ] 監視・通知
- [ ] 少額ライブ

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml   # config.toml は git 管理外
python -m src.data.fetch              # 1分足を取得して SQLite に貯める
```

## ペーパートレードのデプロイ（1ヶ月運用）

実発注なし・仮想¥100k×2口座。毎日18:00 JSTにDiscordへ通知。

> Proxmox(Ubuntu LXC)への詳細手順は **[docs/deploy-proxmox.md](docs/deploy-proxmox.md)** を参照。
> 以下は環境共通の最小手順。

### 1. 準備
```bash
cp .env.example .env                 # .env に DISCORD_WEBHOOK_URL を設定（git管理外）
cp config.example.toml config.toml
# ウォームアップ用に対象ペアの直近を必ずバックフィル（穴があると指標が出ない）
python -m src.data.fetch --pair btc_jpy --start <30日前YYYYMMDD>
python -m src.data.fetch --pair eth_jpy --start <30日前YYYYMMDD>
python -m src.live.paper --once      # 動作確認（1回実行）
python -m src.live.notify_discord --dry-run   # 通知本文を確認（投稿しない）
```

### 2. 常駐（systemd, Linuxサーバー）
```bash
# scripts/auto-trading-paper.service の /path/to/auto-trading を実パスに置換してから
sudo cp scripts/auto-trading-paper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now auto-trading-paper
journalctl -u auto-trading-paper -f
```

### 3. 日次通知（systemd timer, 18:00 JST）
```bash
# scripts/auto-trading-notify.{service,timer} のパスを置換して配置
systemctl enable --now auto-trading-notify.timer
```
- 日次レポート（評価額・1日/1週/2週・約定率）を 18:00 JST に送信
- 常駐プロセスは **起動🟢/停止🔴/エラー⚠️** も自動でDiscord通知
- 詳細は [docs/deploy-proxmox.md](docs/deploy-proxmox.md)

1ヶ月後、Discordログと `state/paper.sqlite`（snapshot/trade/fillstat）で結果をレビュー。
特に **B_intraday の指値約定率** がバックテスト前提（ほぼ100%）からどれだけ下がるかがBの真偽。
