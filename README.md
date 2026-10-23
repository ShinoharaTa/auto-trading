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
- **同一窓比較（compare）**: 複数の (戦略, params) × 時間足 × ペアを、**同じランダム60日窓の集合**で評価して横並びにする（`src/backtest/compare.py`）。montecarlo は戦略ごとに別々の区間を引くため、戦略同士の比較にはこちらを使う。

### 同一窓比較（compare）

窓はカレンダー時刻（一様乱数で引き、開始 t0 を 4h 境界に丸める。`--seed` で固定）で引くので、全戦略・全時間足・全ペアで同じ期間になる。ジョブは `multiprocessing`(fork) で並列実行（`--workers` 既定 = コア数−1）。

```bash
# 既定: 全戦略(ST.STRATEGIES) × 15m/30m/1h/4h × 60日窓100本、既定 params 1 組
python -m src.backtest.compare --pairs btc_jpy --strategies all
# A_trend 相当の params を並べる / 時間足を絞る / 5m は明示したときだけ
python -m src.backtest.compare --strategies sma_cross rsi --spec sma_cross:fast=40,slow=50 --timeframes 1h 4h
# B&H と同条件の参考値（建玉上限を外す）。--param は全戦略の params に上書き
# 値は数値か文字列のみ。None/True/False は渡せない（例: daily_loss_limit=None は不可）
python -m src.backtest.compare --strategies all --param max_position_frac=1.0
# STRATEGY_GRIDS を展開（組数倍になる）
python -m src.backtest.compare --strategies sma_cross --grid --timeframes 1h
```

出力: 窓別 `state/compare_rows_<pair>_<seed>.csv`（1ジョブ1行）と要約 `state/compare_<pair>_<seed>.csv`（標準出力にも表示）。複数ペア指定時は `<pair>` が `all` になり、pair 列付きで 1 ファイルにまとまる。窓別 CSV には `bh_label`（その窓の買い持ちリターンが `+--label-th` 超 = up / `-th` 未満 = down / それ以外 flat、既定 0.05）が付く。

要約表の列（戦略×時間足×params ごと。`score` 降順、末尾にペアごとの `buy_and_hold` 行）:

| 列 | 意味 |
|---|---|
| n_win | 窓数 |
| win_rate | `total_return > 0` の窓の割合 |
| median_ret / p5_ret | 窓リターンの中央値 / 5% 分位 |
| worst_dd | 最大ドローダウンの最大値 |
| med_trades | トレード数の中央値 |
| bh_median | 同じ窓の買い持ちリターン中央値（全戦略で同じ値。基準線） |
| beat_bh | `total_return > bh_return` の窓の割合 |
| up_median / flat_median / down_median | `bh_label` 別のリターン中央値（局面別の成績） |
| score | `median_ret + p5_ret`（optimize と同じ定義） |

注意:
- **窓は互いに重なる。** 180日に60日窓は非重複で3本しか取れず、ランダム100本は強く相関する（独立な100回の試行ではない）。p5 や勝ち区間率は見かけより粗いので、絶対水準（robust 判定）は信用せず、**同じ窓での戦略同士の相対比較**に使う。データを延ばす（`fetch --start`）ほど改善する。
- **ウォームアップ・プレフィックス。** 各窓の前に `max(2 × warmup_bars, 200)` 本を足して切り出し、指標をウォームアップさせる。`simulator.run(no_entry_before=t0)` で窓開始前の新規エントリーを抑止するので、`total_return` / `max_drawdown` / `n_trades` は窓本体の成績と一致し、4h×長期SMAでも60日まるごと取引対象になる（`sharpe` はプレフィックスの平坦バーの分だけ薄まるが全戦略同条件）。プレフィックスの無い montecarlo とは**数値が一致しない**。データ先頭付近でプレフィックスが足りない窓は `warm_short=True`（除外はしない）。
- **損切りなし（トレンドフィルタ）。** `atr_stop_mult=0`（または None）で損切りを無効にできる。サイズは `min(max_position_frac × 資産, 現金)` のみで決まり `risk_per_trade` は無視される。`max_position_frac=1.0` と組み合わせると「シグナル中は全額保有、消えたら全額現金」になる。simulator と live engine で同じ規約。
- **B&H 比較。** simulator は `max_position_frac=0.25` で建玉を資産の25%に制限するので、フル買い持ち（`bh_return`、1分足で窓の始値〜終値）と直接比べると戦略側が不利。上限を外した参考値は `--param max_position_frac=1.0` で出す。

所要時間の目安（この機・N100、`--workers 3`、データ約2年）: 3戦略 × 15m/1h/4h × 100窓 × 1ペア（900ジョブ）で約30秒。ジョブ数（戦略数 × 時間足 × 窓数 × ペア数、`--grid` なら組数倍）に比例する。5m は 15m の約3倍重い。

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
- [x] トレンド系戦略を5種追加（`strategies.py`。方針: `docs/trend-eval-plan.md`）
  - `ema_cross`: 短期EMA > 長期EMA でロング（sma_cross のEMA版。立ち上がりが速い分レンジで騙されやすい）
  - `donchian`: 直前N本(`dc_entry`)の高値更新で買い、直前M本(`dc_exit`)の安値割れで手仕舞い
  - `macd`: MACD線 > シグナル線 かつ MACD > 0 でロング（中期トレンドの継続局面向け）
  - `sma_adx`: SMAクロス + ADX > `adx_min` のときだけ新規エントリー。出口はSMA反転のみ（ADX低下では出ない）
  - `roc`: N本前比の騰落率 > `roc_entry` で買い、< `roc_exit` で手仕舞い（時系列モメンタム。entry/exit にヒステリシス）
  - `optimize --strategy all`（8戦略）の所要時間目安: `--runs 30` で約2.5分（実測146秒、btc_jpy 2年分・N100）。既定 `--runs 200` は runs に比例して約16分（推定）
- [x] レジーム切替戦略 `regime_switch`（`strategies.py`。方針: `docs/trend-eval-plan.md` §2.2-6）
  - ADX(`regime_adx_period`) > `regime_adx_min` ならトレンド局面として `trend_strategy`（既定 sma_cross）、それ以外はレンジ局面として `range_strategy`（既定 rsi）のシグナルを使う。サブ戦略のパラメータ（`fast`/`slow`/`rsi_*` 等）は同じ params から読む
  - トレードオフ: (1) 利点: 「傾向に応じて」を1戦略で表現でき、optimize/walkforward/validate/paper にそのまま乗る。(2) 単一時間足内の切替のみ（A=1h / B=5m のような時間足違いの切替は口座配分の問題で対象外）。(3) 局面が変わるとシグナル源が変わるので保有中に手仕舞いが起きうる（レンジ入りで順張り玉を閉じるのは仕様）。パラメータが増える分過剰適合しやすいので walkforward の `overfit_gap` を必ず見る。(4) ADX は遅行指標（1h×14本≒14時間遅れ）。局面の切替回数が少なく、効果を統計的に言うにはデータが足りない可能性が高い
  - `optimize --strategy regime_switch --runs 30` は約19秒（実測）。`optimize --strategy all`（9戦略）は `--runs 30` で約2.8分（推定）
- [x] `warmup_bars` を戦略の既定パラメータ（`strategies.DEFAULTS`）込みで評価し、macd は `macd_slow + macd_signal`、sma_adx は `2 × adx_period`、regime_switch はサブ戦略と `2 × regime_adx_period` の最大を反映（`STRATEGIES` / `WARMUP_KEYS` / `DEFAULTS` のキーは常に一致させる）
  - paper に載せる戦略は `warmup_bars × 2` が feed の `_LOOKBACK_DAYS`（1h=336本 / 4h=240本）以内であること（`src/live/feed.py`）。engine は `warmup_bars + 2` 本未満だと判定自体をスキップする。×2 は指標を安定させるための余裕
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
