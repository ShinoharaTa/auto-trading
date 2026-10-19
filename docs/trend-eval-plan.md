# トレンド型戦略の横並び比較 + 60日ランダム窓評価 — 方針案

作成: 2026-10-19 / 対象: `~/workspace/auto-trading`（main @ 554050f）
凡例: **[事実]** = コードで確認済み / **[推測]** = 未確認の見込み

---

## 0. 何のために

ペーパー稼働中の A_trend（1h SMA40/50 順張り）が直近180日で負け、買い持ち(+7.5%)にも劣る。
「トレンド系の作り方が悪いのか、相場の局面が合わなかったのか」を切り分けるため、

1. 複数のトレンド型戦略を **同じランダム60日窓** で横並び比較できるようにし、
2. 局面（トレンド/レンジ）で戦略を切り替えた場合の効果も同じ土俵で測る。

結論は「どの戦略を次のペーパー候補にするか」の材料。実発注・常駐は一切変えない。

---

## 1. 要望の解釈

「並列でいくつかのトレンドパターン試してみたい。60日をランダムに抽出して動かす」

| 解釈 | 内容 | 採否 |
|---|---|---|
| (a) 横並び比較 | 複数トレンド系戦略を同条件（同じ窓・同じ時間足・同じリスク設定）で比較 | **採用（主）** |
| (b) 並列実行 | multiprocessing で (a) を 4 コアで回す | **採用（手段）** |
| (c) 複数ペーパー口座を並列稼働 | 別サーバーの paper に口座を増やして実時間で比較 | 不採用。1ヶ月待つより過去180日で先に絞る。(a) の結果で候補が出たら config.toml に口座追加するだけ |
| (d) 相場パターン別の分類評価 | 各窓を「上昇/下落/レンジ」にラベル付けして戦略×局面の成績表を出す | **(a) の出力に窓ラベル列を足す形で一部採用**（§3.5）。「傾向に応じて」の直接の答えになる |

**推奨: (a)+(b) を本線、(d) をランナーの出力列として同梱。** (c) は結果が出てから。

---

## 2. 追加するトレンド系戦略の候補

### 2.0 既存規約 [事実]
- 戦略 = `f(df, p) -> Series(0/1)`。最後に `.shift(1).fillna(0.0)`。
- 状態シグナルは `_state_signal(enter, exit_, index)`（enter で 1、exit で 0、間は保持）。
- `STRATEGIES` / `WARMUP_KEYS` に登録。`warmup_bars()` は WARMUP_KEYS のパラメータ値と `atr_period` の最大。
- `simulator.run` は `len(df) < warmup_bars+2` なら無トレードで返す。NaN シグナルは各戦略が fillna(0) しているので、ウォームアップ中は必ず「無ポジ」。
- live engine は `ST.STRATEGIES[strategy](bars, p).iloc[-1]` を直接呼ぶ [事実: `src/live/engine.py:90`] → 登録すれば paper でもそのまま使える。feed の `_LOOKBACK_DAYS` は 1h=14日(336本)/4h=40日(240本) [事実: `feed.py:15`] なので、**paper に載せる戦略は最長窓をこれ以下に**。

### 2.1 候補一覧（5 個 + レジーム切替 1 個）

| # | 名前 | 一言 | 向く相場 | 弱い相場 | 追加コスト |
|---|---|---|---|---|---|
| 1 | `ema_cross` | EMA 短期 > 長期 | 立ち上がりの速いトレンド | レンジ（SMA より騙し多め） | 小（sma_cross のコピー） |
| 2 | `donchian` | N 本高値更新で入り、M 本安値割れで出る | 大きな一方向トレンド | 往復ビンタのレンジ | 小 |
| 3 | `macd` | MACD線 > シグナル線 かつ MACD > 0 | 中期トレンドの継続局面 | 横ばい（ゼロ付近でチラつく） | 小 |
| 4 | `sma_adx` | SMA クロス + ADX がしきい値超のときだけ入る | トレンドが「強い」局面 | ADX 立ち上がり遅れで初動を逃す | 中（ADX 実装） |
| 5 | `roc` | N 本前比の騰落率 > しきい値でロング（時系列モメンタム） | 持続的上昇 | 急落後の反発（遅い） | 小 |
| 6 | `regime_switch` | ADX で局面判定 → トレンドなら #4 系、レンジなら rsi | 局面が長く続く相場 | 局面転換が頻繁な相場 | 中（#4 の ADX 再利用） |

### 2.2 各候補の定義（implementer 向け）

共通: `close = df["close"]`。rolling 系は `min_periods` 既定（窓未満は NaN → 比較 False → 0）。ewm は `adjust=False, min_periods=<窓>` を明示して NaN を出す（ウォームアップ中に誤シグナルが出ないように）。

**1. `ema_cross`**
- params: `fast`(既定 20), `slow`(既定 60) ※ sma_cross と同名にして `optimize._param_combos` の `fast>=slow` スキップをそのまま使う
- `fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()`、slow も同様
- `return (fast_ema > slow_ema).astype(float).shift(1).fillna(0.0)`
- WARMUP_KEYS: `("fast", "slow")`
- グリッド: `fast [10,20,40]`, `slow [50,100,200]`, `atr_stop_mult [1.5,2.5]`, `risk_per_trade [0.01]`

**2. `donchian`**
- params: `dc_entry`(既定 20), `dc_exit`(既定 10)
- `upper = df["high"].rolling(dc_entry).max().shift(1)`（直前 N 本の高値。当バーを含めない）
- `lower = df["low"].rolling(dc_exit).min().shift(1)`
- `sig = _state_signal(close > upper, close < lower, df.index)` → `.shift(1).fillna(0.0)`
- WARMUP_KEYS: `("dc_entry", "dc_exit")`
- グリッド: `dc_entry [20,40,55]`, `dc_exit [10,20]`, `atr_stop_mult [1.5,2.5]`, `risk_per_trade [0.01]`
- 注意: `dc_exit >= dc_entry` は意味が薄いが有害ではない。スキップ条件には入れない

**3. `macd`**
- params: `macd_fast`(12), `macd_slow`(26), `macd_signal`(9)
- `macd = ema(fast) - ema(slow)`（上記 ewm 規約）、`sig_line = macd.ewm(span=macd_signal, adjust=False, min_periods=macd_signal).mean()`
- `long = (macd > sig_line) & (macd > 0)` → `.astype(float).shift(1).fillna(0.0)`（状態シグナル不要。条件そのものが状態）
- WARMUP_KEYS: `("macd_slow", "macd_signal")`。真の必要本数は slow+signal−1=34 だが `warmup_bars` は max を取るため 26 と過小 [事実: `strategies.py:78-81`]。影響は「窓が極端に短いときだけ」なので許容。ランナー側（§3.3 プレフィックス）で `warmup_bars*2` を取るので実害なし
- グリッド: `macd_fast [8,12]`, `macd_slow [21,26,40]`, `macd_signal [9]`, `atr_stop_mult [1.5,2.5]`, `risk_per_trade [0.01]`
- `optimize._param_combos` に `macd_fast >= macd_slow` スキップを追加（§5）

**4. `sma_adx`**
- ヘルパ `_adx(df, period) -> Series`（Wilder）: `+DM/-DM` → `ewm(alpha=1/period, adjust=False, min_periods=period)` で平滑 → `DX = 100*|+DI-−DI|/(+DI+−DI)` → `ADX = DX.ewm(alpha=1/period, adjust=False, min_periods=period).mean()`。分母 0 は NaN → `fillna(0.0)`
- params: `fast`(20), `slow`(60), `adx_period`(14), `adx_min`(20.0)
- `enter = (fast_sma > slow_sma) & (adx > adx_min)`、`exit_ = fast_sma < slow_sma`
- `sig = _state_signal(enter, exit_, df.index).shift(1).fillna(0.0)`
- 設計判断（確定）: **ADX 低下では出ない**（出口は SMA 反転のみ）。ADX はフィルタであって出口シグナルではない。これにより sma_cross との差分 = 「レンジ中の新規エントリー抑制」だけになり、比較が解釈しやすい
- WARMUP_KEYS: `("fast", "slow", "adx_period")`。ADX は二重平滑なので実効 2×adx_period。プレフィックス倍率で吸収
- グリッド: `fast [20,40]`, `slow [50,100]`, `adx_period [14]`, `adx_min [20,25,30]`, `atr_stop_mult [1.5,2.5]`, `risk_per_trade [0.01]`

**5. `roc`**
- params: `roc_period`(24), `roc_entry`(0.0), `roc_exit`(−0.01)
- `r = close / close.shift(roc_period) - 1`
- `sig = _state_signal(r > roc_entry, r < roc_exit, df.index).shift(1).fillna(0.0)`（entry/exit にヒステリシスを持たせてチラつき抑制）
- WARMUP_KEYS: `("roc_period",)`
- グリッド: `roc_period [12,24,48]`, `roc_entry [0.0, 0.01]`, `roc_exit [-0.01]`, `atr_stop_mult [1.5,2.5]`, `risk_per_trade [0.01]`

**6. `regime_switch`（レジーム判定で切替）**
- params: `regime_adx_period`(14), `regime_adx_min`(25.0), `trend_strategy`("sma_cross"), `range_strategy`("rsi")、ほかサブ戦略のパラメータ（`fast/slow`, `rsi_period/rsi_entry/rsi_exit`）はそのまま同じ p から読む（名前が衝突しない [事実]）
- `adx = _adx(df, regime_adx_period)`、`trending = adx > regime_adx_min`
- `sig_t = STRATEGIES[trend_strategy](df, p)`、`sig_r = STRATEGIES[range_strategy](df, p)`（各々 shift 済み）
- `trending` も `.shift(1)` してから `np.where(trending, sig_t, sig_r)` → Series、`fillna(0.0)`
  - **二重 shift しない**: サブ戦略はすでに shift 済みなので、合成後に shift を重ねない
- WARMUP_KEYS: `("fast", "slow", "rsi_period", "regime_adx_period")`
- グリッド: `regime_adx_min [20,25,30]`, `fast [20,40]`, `slow [50,100]`, `rsi_period [14]`, `rsi_entry [30]`, `rsi_exit [55]`, `atr_stop_mult [2.5]`, `risk_per_trade [0.01]`（72 組。規模は `optimize --period` 次第で調整）
- **トレードオフ**:
  - 利点: 「傾向に応じて」をそのまま 1 戦略で表現でき、既存 optimize/walkforward/validate/paper にそのまま乗る
  - 欠点1: 単一時間足内の切替。現行 A(1h) と B(5m) のように時間足が違う組合せは表現できない。→ 時間足違いの切替は「口座配分」の問題で simulator の外（§6 未決）
  - 欠点2: 局面が切り替わった瞬間にシグナル源が変わるので、保有中に強制クローズが起きうる（例: トレンドで保有中にレンジ判定→rsi が 0 を返す→手仕舞い）。これは仕様として受け入れる（レンジ入りで順張り玉を閉じるのは自然）
  - 欠点3: パラメータが増える分、過剰適合しやすい。walkforward の `overfit_gap` を必ず見る
  - 欠点4: ADX は遅行指標。1h×14 本=14 時間の遅れ。[推測] 180 日のデータでは局面が数回しか変わらず、効果の有無を統計的に言うには足りない可能性が高い → §3.6 のデータ延長を併せて推奨

---

## 3. 60 日ランダム窓ランナー `src/backtest/compare.py`

### 3.1 目的
複数 (戦略, params) × 複数時間足 を、**同一のランダム 60 日窓集合** で評価し、公平に横並びにする。既存 `montecarlo.run_montecarlo` は戦略ごとに別々の窓を引く上に逐次実行 [事実] なので、新モジュールにする（montecarlo は変更しない）。

### 3.2 窓のサンプリング（カレンダー時刻で引く）
- `sample_windows(start: Timestamp, end: Timestamp, days: int, n: int, seed: int) -> list[tuple[Timestamp, Timestamp]]`
- `t0 = start + U(0, (end - start) - days)`（分単位の一様乱数、`np.random.default_rng(seed)`）、`t1 = t0 + days`
- **時刻で引く**ので、全戦略・全時間足・全ペアで同じカレンダー窓になる（ペア間でも開始日が揃っている [事実: 4 ペアとも 2026-04-22 00:00 開始]）
- 窓数 `--runs` 既定 **100**

**重なりの注意（README/ヘルプに明記）**: 180 日に 60 日窓は非重複で 3 本しか取れない。ランダム 100 本は互いに強く相関し、「独立な試行 100 回」ではない。p5 や勝ち区間率は**見かけより粗い**（実効サンプルは数本）。使い方は「戦略同士の相対比較（同じ窓なので公平）」に限定し、絶対水準（robust 判定）は信用しない。改善は §3.6。

### 3.3 窓の切り出しとウォームアップ・プレフィックス
- 各時間足 tf について `rdf = R.resample_ohlcv(df1m, tf)` を 1 回だけ作る
- 窓 [t0, t1) に対し `pre = 2 * ST.warmup_bars(strategy, params)` 本を**前に足して** `blk = rdf.iloc[max(0, i0 - pre) : i1]`（`i0 = rdf.index.searchsorted(t0)`, `i1 = searchsorted(t1)`）
- 根拠 [事実]: ウォームアップ中は全戦略がシグナル 0（NaN→0 / rsi は 50 で中立）、ATR も NaN → simulator は無ポジのまま → equity 平坦。したがって `total_return`/`max_drawdown`/`n_trades` は窓本体の成績と一致する。`sharpe` は平坦バーが分母に入る分だけ薄まるが全戦略同条件
- これにより 4h×slow=200（=33 日）でも 60 日まるごと取引対象になる。既存 montecarlo（プレフィックスなし）と数値が一致しないことは README に書く
- データ先頭付近の窓は `i0 - pre < 0` でプレフィックス不足 → その行に `warm_short=True` を立てる（除外はしない）

### 3.4 ジョブと並列化
- `@dataclass(frozen=True) Job: pair, strategy, timeframe, params(json文字列), window_id, t0, t1`
- `multiprocessing.get_context("fork").Pool(workers, initializer=_init, initargs=(frames,))`。`frames = {(pair, tf): rdf}` をフォーク前にグローバルへ置き、子は CoW で共有（4 ペア×4 時間足のリサンプル済み DF は合計 100MB 未満 [推測: 1 分足 26 万行×6 列×4 ペア ≒ 50MB、上位足はその 1/5 以下]）
- `pool.imap_unordered(_eval_job, jobs, chunksize=8)`。`_eval_job` はモジュールトップレベル関数（pickle 可）
- `--workers` 既定 `max(1, os.cpu_count() - 1)` = 3。4 コア機で他サービスを巻き込まないため
- 1 ジョブの戻り: `{pair, strategy, timeframe, params, window_id, t0, t1, total_return, max_drawdown, sharpe, profit_factor, win_rate, n_trades, bh_return, warm_short}`
  - `bh_return = close[t1 直前] / close[t0] - 1`（窓本体のみ。プレフィックスを含めない）

### 3.5 出力
**(1) 窓別 CSV** `--out-rows`（既定 `state/compare_rows_<pair>_<seed>.csv`）: 上記 1 行/ジョブ。窓ラベル列を付ける:
- `bh_label`: `bh_return > +0.05` → `up` / `< -0.05` → `down` / それ以外 `flat`（しきい値 `--label-th` 既定 0.05）。これが解釈 (d) の最小実装。集計は pandas で後からできる

**(2) 要約表（標準出力 + `--out` CSV、既定 `state/compare_<pair>_<seed>.csv`）** 戦略×時間足で集計、`score = median_ret + p5_ret` 降順:

| 列 | 定義 |
|---|---|
| strategy, timeframe, params | 識別 |
| n_win | 窓数 |
| win_rate | `total_return > 0` の窓割合 |
| median_ret / p5_ret | 窓リターンの中央値 / 5% 分位 |
| worst_dd | `max_drawdown` の最大 |
| med_trades | `n_trades` の中央値 |
| bh_median | 同じ窓の買い持ちリターン中央値（全戦略で同じ値。基準線） |
| beat_bh | `total_return > bh_return` の窓割合 |
| up_median / flat_median / down_median | `bh_label` 別の中央値リターン（局面別成績。(d) の答え） |
| score | median_ret + p5_ret（optimize と同じ定義） |

さらに末尾に `buy_and_hold` 行を 1 行入れる（strategy="buy_and_hold", total_return=bh_return として集計）。これで「全戦略が B&H に負けている」が表の中で見える。

**B&H 比較の注意（README に書く）**: simulator は `max_position_frac=0.25` [事実] で建玉上限 25%。フル買い持ちと直接比べるのは不利なので、`--param max_position_frac=1.0` で上限外しの参考値も出せるようにする（`--param KEY=VALUE` 繰返し、全戦略の params に上書き）。

### 3.6 実行時間とデータ量
実測 [事実]（btc_jpy, 60 日窓 1 本, この機）: 5m 0.42s / 15m 0.14s / 1h 0.04s / 4h 0.01s → 4 時間足合計 ≈ 0.6s/ジョブ組
- 既定（7 戦略 × 4 時間足 × 100 窓 × 1 ペア、既定 params のみ）: 7×100×0.6 ≈ 7 分（逐次）→ 3 worker で **約 2.5 分**
- 4 ペア: 約 10 分。グリッド展開（`--grid`）すると組数倍。5m を外せば 1/3
- RAM: 子 3 プロセス × 100MB 程度 [推測]。空き 4GB で余裕

**データ延長の推奨**: 60 日窓の相対比較には 180 日で足りるが、局面別（up/flat/down）の評価は窓が足りない。`python -m src.data.fetch --pair btc_jpy --start 20240101` で 2 年強に延ばせる [事実: README に 2022-06〜2024-06 の取得実績、fetch は日単位の差分取得]。運用コスト: 公開 API のみ・1 日 1 リクエスト×0.3s pause → 1 ペア 650 日で約 5 分、SQLite +300MB 程度 [推測]。外部サービス追加なし。

### 3.7 CLI
```
python -m src.backtest.compare \
  --pairs btc_jpy eth_jpy \
  --strategies all            # or: sma_cross ema_cross donchian ...
  --timeframes 15m 1h 4h      # 既定: 15m 30m 1h 4h（5m は --timeframes で明示時のみ）
  --days 60 --runs 100 --seed 42 \
  --workers 3 \
  --param order_type=taker --param max_position_frac=0.25 \
  --grid                      # 付けると STRATEGY_GRIDS を展開（既定は各戦略の既定 params 1 組）
  --out state/compare.csv --out-rows state/compare_rows.csv
```
- `--strategies all` = `ST.STRATEGIES` の全キー（regime_switch 含む）
- 既定 params 1 組の出所: **戦略関数内の `p.get` 既定値**（params={}）。ただし A_trend 相当 `sma_cross fast=40 slow=50` も比較したいので `--spec sma_cross:fast=40,slow=50` を繰返し指定可能にする（`--strategies` と併用可）

---

## 4. PR 分割（implementer にそのまま渡す粒度）

依存: PR-1 → PR-3（ADX ヘルパ共有）。PR-2 は PR-1 と独立（既存 3 戦略でも動く）。並行着手可。各 PR に README 追記を含める。各 PR は issue を先に立て、`feature/*` ブランチ、`Closes #n`。

### PR-1 `feat(strategies): トレンド系戦略を5種追加（ema_cross/donchian/macd/sma_adx/roc）`
- 対象: `src/backtest/strategies.py`, `src/backtest/optimize.py`, `README.md`
- strategies.py: §2.2 の 1〜5 を実装。`_ema(close, span) -> Series`、`_adx(df, period) -> Series` をヘルパとして追加。`STRATEGIES` と `WARMUP_KEYS` に登録（§2.2 のキー通り）
- optimize.py: `STRATEGY_GRIDS` に §2.2 のグリッドを追加。`_param_combos` を次に変更: `_ORDERED = (("fast","slow"), ("macd_fast","macd_slow"))` を定義し、いずれかで `params.get(a, 0) >= params.get(b, 1)` ならスキップ（既存 fast/slow 判定の一般化。既存挙動は不変）
- README: ロードマップの戦略行に 5 種追記、各戦略 1 行説明
- 受け入れ基準:
  1. `.venv/bin/python -c "from src.backtest import strategies as S; print(sorted(S.STRATEGIES)==sorted(S.WARMUP_KEYS))"` → `True`
  2. 単調増加の合成 close（`pd.Series(range(1,301), dtype=float)` を OHLC 全列に）で 5 戦略とも末尾 50 本のシグナルがすべて 1。単調減少なら 0（`python -c` で確認し、出力を PR 本文に貼る）
  3. 各戦略の戻り Series が `index == df.index`、値域 {0.0, 1.0}、先頭 `warmup_bars` 本は 0（shift+NaN 処理の確認）
  4. `.venv/bin/python -m src.backtest.optimize --strategy ema_cross --runs 30 --period 1w` が例外なく終わり表が出る（5 戦略すべてで実施）
  5. `.venv/bin/python -m src.backtest.walkforward --strategy donchian --runs 30 --train-days 90 --test-days 30` が終わる（新戦略が `_params_from_row` を通ること）
  6. 既存 3 戦略の `optimize --strategy sma_cross --runs 30` の上位 3 行が変更前と一致（`_param_combos` 変更の回帰確認。変更前の出力を先に取っておく）

### PR-2 `feat(backtest): 60日ランダム窓で複数戦略を同一窓・並列比較する compare.py`
- 対象: `src/backtest/compare.py`（新規）, `src/data/resample.py`, `src/backtest/montecarlo.py`, `README.md`
- resample.py: `PERIOD_MIN` に `"2m": 60*24*60` を追加。montecarlo.py: `BLOCK_PRESETS` に `"2m"` を追加（既存 `--period/--block` の choices に自動で載る）
- compare.py の公開関数:
  - `sample_windows(start: pd.Timestamp, end: pd.Timestamp, days: int, n: int, seed: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]`
  - `slice_window(rdf: pd.DataFrame, t0, t1, prefix_bars: int) -> tuple[pd.DataFrame, bool]`（戻りの bool = warm_short）
  - `build_jobs(pairs, specs: list[tuple[str, dict]], timeframes, windows) -> list[Job]`
  - `run_compare(db: str, pairs, specs, timeframes, windows, workers: int, extra_params: dict) -> pd.DataFrame`（窓別行）
  - `summarize(rows: pd.DataFrame, label_th: float = 0.05) -> pd.DataFrame`（§3.5 (2) の列）
  - `main()`: §3.7 の CLI
- 並列: §3.4。`workers=1` のときは Pool を使わず逐次（デバッグ用・結果同一であること）
- 出力ファイルは `state/` 配下（.gitignore 済み [事実]）
- README: 「評価方法論」に「同一窓比較（compare）」の段落、重なりの注意、プレフィックスの説明、B&H と max_position_frac の注意、実行例と所要時間目安
- 受け入れ基準:
  1. `.venv/bin/python -m src.backtest.compare --pairs btc_jpy --strategies sma_cross rsi --timeframes 1h --runs 20 --seed 1 --workers 1` と `--workers 3` の `--out-rows` CSV が `total_return` 列まで完全一致（sort 後 diff）
  2. 同コマンドで `--spec sma_cross:fast=40,slow=50` を足すと要約表に `sma_cross` が 2 行（params 違い）＋ `buy_and_hold` 1 行出る
  3. 窓別 CSV の `t0,t1` が戦略間で同一集合（`groupby(strategy).t0.apply(set)` が全て等しい）
  4. 全行で `t1 - t0 == 60 days`、`warm_short` は先頭付近の窓のみ True
  5. `bh_return` が戦略によらず同一窓で同じ値
  6. `--runs 100 --strategies all --timeframes 15m 1h 4h --workers 3` の実行時間を PR 本文に記載（目安 ≤ 5 分/ペア。超えるなら理由）
  7. `.venv/bin/python -m src.backtest.montecarlo --block 2m --runs 50` が動く

### PR-3 `feat(strategies): ADX レジーム判定でトレンド/レンジ戦略を切り替える regime_switch`
- 前提: PR-1 マージ済み（`_adx` を使う）
- 対象: `src/backtest/strategies.py`, `src/backtest/optimize.py`, `README.md`
- §2.2 の 6 を実装。`STRATEGIES["regime_switch"]`, `WARMUP_KEYS["regime_switch"] = ("fast","slow","rsi_period","regime_adx_period")`, `STRATEGY_GRIDS["regime_switch"]`（§2.2）
- `trend_strategy` / `range_strategy` は params 経由の文字列。`STRATEGIES` に無い名前なら `KeyError` をそのまま（黙って既定に戻さない）。自分自身を指定した場合は `ValueError`
- README: 戦略一覧に追記、§2.2 のトレードオフ 4 点を要約して記載
- 受け入れ基準:
  1. 合成データ: 前半 300 本が単調増加（ADX 高）、後半 300 本が ±1 の往復（ADX 低）。前半末尾は `sig == sma_cross の sig`、後半は `sig == rsi の sig` と一致する本数が 9 割以上（ADX の遅れ分を許容）
  2. `regime_adx_min=0` なら全区間で sma_cross と完全一致、`regime_adx_min=1000` なら rsi と完全一致（切替ロジックの境界確認）
  3. `.venv/bin/python -m src.backtest.optimize --strategy regime_switch --runs 30 --period 2w` が終わる
  4. `.venv/bin/python -m src.backtest.compare --strategies sma_cross rsi regime_switch --timeframes 1h --runs 50` の要約表に 3 戦略 + buy_and_hold が出る
  5. `python -m src.live.paper --once` に影響なし（config を変えていないので動作不変。実行して確認）

### PR-4（任意・後回し）`feat(compare): 局面ラベル別の集計を独立コマンド化`
PR-2 の `bh_label` 列で最低限は足りるので、**今回は起票しない**。compare の結果を見て、局面ラベルを ADX ベースに変えたい/複数しきい値で見たい、となったときに切る。

---

## 5. 既存コードを壊さないための注意

- **`STRATEGIES` と `WARMUP_KEYS` のキー集合を常に一致させる**（PR-1 受け入れ 1）。`warmup_bars` は未登録でも 30 を返して黙って動く [事実] ので、ズレると live/paper で足が足りないのに走る
- `optimize --strategy all` は `STRATEGY_GRIDS` の全キーを回す [事実: `optimize.py:133`]。グリッドを増やすと `all` の時間が伸びる。PR-1/3 の README に `all` の概算時間を書く
- `walkforward._params_from_row` は optimize の結果列からメタ列以外を全部 params にする [事実]。**optimize の出力に新しい列を足さない**（足すなら `_META_COLS` にも追加）。compare は optimize と別物なので影響なし
- `tune.py` / `retune.py` / `settings_store` は `strategy` 名を文字列で保持するだけ [事実] → 新戦略名を渡せばそのまま動く。既定は `sma_cross` のまま変えない
- `_param_combos` の変更は既存 fast/slow 判定を包含する形にし、既存 3 戦略の optimize 結果が変わらないことを確認する（PR-1 受け入れ 6）
- live `feed._LOOKBACK_DAYS` [事実] は 1h=336 本 / 4h=240 本。paper に載せる戦略は `warmup_bars*2` がこれ以下であること（PR-3 の README に注記）。今回 feed は触らない
- pandas 3.0 [事実: 3.0.6]: Copy-on-Write 既定。`_state_signal` の `raw[enter] = 1.0` は新規 Series への代入なので問題なし。新戦略でも **スライスした DataFrame に列を追加しない**（`blk["x"] = ...` は CoW で警告/無効になりうる）。中間値はローカル Series で持つ。`np.where` の結果は `pd.Series(..., index=df.index)` で包む
- `ewm(min_periods=...)` を使うと pandas 3 でも先頭が NaN になる。比較演算は NaN→False なので 0 になる（PR-1 受け入れ 3 で確認）
- `multiprocessing` は `fork` を明示（Linux 既定だが将来の既定変更に備える）。子プロセスで SQLite を開かない（親で `load_candles` → リサンプル → フォーク）
- compare の出力先 `state/` は .gitignore 済み [事実]。リポジトリに CSV をコミットしない

---

## 6. 決定事項 / 未決事項

**決定（この方針案で確定）**
- 解釈は (a) 横並び比較 + (b) multiprocessing。(d) は `bh_label` 列で最小対応
- 戦略 5 種（ema_cross / donchian / macd / sma_adx / roc）+ regime_switch
- 窓はカレンダー時刻でサンプリング、ウォームアップ・プレフィックス `2×warmup_bars` を前置
- 要約表の列と `score` 定義は optimize と揃える（median+p5）
- `sma_adx` の出口は SMA 反転のみ（ADX 低下では出ない）
- PR は 3 本、PR-4 は保留

**未決（ユーザー判断）**
1. **データ延長**（`fetch --start 20240101`、約 2 年）を PR-2 の受け入れ前に行うか。局面別の評価を本気でやるなら必要。コスト: 取得 ~5 分/ペア、SQLite +300MB [推測]
2. **5m を既定時間足に含めるか**。含めると実行時間 3 倍。B_intraday 相当（rsi 5m maker）を同じ表に載せたいなら `--timeframes 5m 15m 1h 4h --param order_type=maker` を別途回す運用で足りるか
3. **時間足をまたぐレジーム切替**（A=1h トレンド / B=5m 逆張り を局面で配分）をやるか。simulator ではなく paper の口座配分の話になり、別設計（今回の範囲外）
4. compare で「良い」と出た戦略を paper の第 3 口座（C_xxx）として config.toml に足すか。これは compare の結果を見てから

---

## 7. 実行順の提案

1. PR-1 と PR-2 を並行で implementer に出す（独立）
2. PR-2 マージ後、`compare --strategies all --pairs btc_jpy eth_jpy --runs 100` を 1 度回して結果を見る（この時点で「A_trend が他のトレンド型より悪いのか、トレンド型全般が負けているのか」が分かる）
3. PR-3（regime_switch）は PR-1 マージ後に着手
4. 未決 1（データ延長）は PR-2 の結果で「窓が足りない」と感じたら
