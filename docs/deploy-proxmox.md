# Proxmox (Ubuntu LXC) へのデプロイ手順

ペーパートレード（実発注なし）を Proxmox 上の Ubuntu LXC コンテナで常駐運用し、
毎日 18:00 JST に Discord 通知する手順。

## 0. 前提と方針

- 必要なのは **外向きインターネット接続だけ**（bitbank公開API・Discord Webhook・GitHub）。
  インバウンドのポート開放は不要。
- 常駐プロセス（`src/live/paper.py`）は軽い。ただし **再チューニング（retune）はCPUを使う**ので、
  retune も回すならコアを多めに。
- 時刻は cron 側で `CRON_TZ=Asia/Tokyo` を使うため、コンテナのTZがUTCでも 18:00 JST に通知される。

## 1. Proxmox でコンテナ作成（LXC）

Proxmox WebUI →「Create CT」:

| 項目 | 推奨値 |
|---|---|
| Template | `ubuntu-24.04`（or 22.04）|
| Cores | 2〜4（retune を回すなら4）|
| Memory | 2048 MB |
| Disk | 8 GB 以上（2年分データで数百MB）|
| Network | vmbr0 / DHCP（外向き通信が通ること）|
| Unprivileged | はい（既定でOK）|

起動後、コンソール or SSH で入る。

## 2. 基本パッケージ

```bash
apt update && apt -y upgrade
apt -y install git python3-venv python3-pip tzdata
# (numpy/pandas の wheel で足りない場合の保険)
apt -y install build-essential python3-dev
```

## 3. リポジトリ取得

```bash
cd /opt
git clone https://github.com/ShinoharaTa/auto-trading.git   # public なら HTTPS が手軽
cd auto-trading
```

> private の場合は GitHub の Personal Access Token（HTTPS）か、コンテナに deploy key を置いて SSH クローン。

## 4. Python 環境

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

## 5. 設定ファイル（秘密情報）

```bash
cp config.example.toml config.toml
cp .env.example .env
nano .env          # DISCORD_WEBHOOK_URL=... を実値に
```

`.env` / `config.toml` は `.gitignore` 済み（コミットされない）。

## 6. データのバックフィル（必須）

ウォームアップ用に直近を取得しないと指標が出ない。1h戦略は約14日必要なので余裕をもって60日:

```bash
./.venv/bin/python -m src.data.fetch --pair btc_jpy --start $(date -u -d '60 days ago' +%Y%m%d)
./.venv/bin/python -m src.data.fetch --pair eth_jpy --start $(date -u -d '60 days ago' +%Y%m%d)
```

> retune（ペア別再チューニング）も回すなら各ペア2年分を取得（時間がかかる）。ペーパー稼働だけなら上記でOK。

動作確認:

```bash
./.venv/bin/python -m src.live.paper --once
./.venv/bin/python -m src.live.notify_discord --dry-run
```

## 7. 常駐化（systemd）

```bash
# パスを実値に置換（/opt/auto-trading）。root で動かすなら User 行は不要。
sed 's#/path/to/auto-trading#/opt/auto-trading#g' \
  scripts/auto-trading-paper.service | tee /etc/systemd/system/auto-trading-paper.service

systemctl daemon-reload
systemctl enable --now auto-trading-paper
systemctl status auto-trading-paper --no-pager
journalctl -u auto-trading-paper -f      # ログを追う（Ctrl-C で抜ける）
```

## 8. 日次 Discord 通知（cron, 18:00 JST）

```bash
crontab -e
```

末尾に追記:

```cron
CRON_TZ=Asia/Tokyo
0 18 * * * /opt/auto-trading/scripts/notify.sh >> /opt/auto-trading/state/cron.log 2>&1
```

（任意）月次の再チューニングも回すなら:

```cron
0 3 1 * * /opt/auto-trading/scripts/retune.sh >> /opt/auto-trading/state/cron.log 2>&1
```

## 9. 稼働確認チェックリスト

- `systemctl status auto-trading-paper` が **active (running)**
- `journalctl -u auto-trading-paper` に毎分 `equity=¥...` が出る
- `state/paper.sqlite` が生成され、`snapshot` 行が増えていく
- 手動で `./scripts/notify.sh` を実行 → Discord に届く

## 運用メモ

- **再起動耐性**: systemd が `Restart=always` で自動復帰。Proxmox 側でコンテナを
  「起動時に自動開始」にしておくとホスト再起動後も復帰。
- **1ヶ月後のレビュー**: `state/paper.sqlite` の `trade` / `fillstat` / `snapshot` と
  Discord ログで判定（特に **B_intraday の指値約定率**）。
- バックアップ対象は `state/`（運用状態）だけ。コードは Git にある。
