#!/usr/bin/env bash
#
# install.sh — 把 garmin-endurance 部署成 systemd user timer（每日兩次）
#
# 用法：
#   chmod +x install.sh && ./install.sh
#
set -euo pipefail

APP_DIR="$HOME/.local/share/garmin-endurance"
CONF_DIR="$HOME/.config/garmin-endurance"
UNIT_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------- #
say "檢查環境"

if ! command -v python3 >/dev/null; then
  echo "找不到 python3，請先安裝：sudo apt install python3 python3-venv" >&2
  exit 1
fi

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "  Python $PYVER"
python3 - <<'EOF' || { echo "需要 Python 3.12 以上（garminconnect 的要求）" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
EOF

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "systemd user session 沒有跑起來。WSL2 請確認 /etc/wsl.conf 有：" >&2
  echo "  [boot]" >&2
  echo "  systemd=true" >&2
  exit 1
fi
echo "  systemd user session OK"

if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  warn "linger 未開啟，未登入時 timer 不會跑。建議執行："
  warn "  sudo loginctl enable-linger $USER"
fi

echo "  時區：$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone)"

# --------------------------------------------------------------------------- #
say "建立目錄與虛擬環境"

mkdir -p "$APP_DIR" "$CONF_DIR" "$UNIT_DIR" "$BIN_DIR"
chmod 700 "$CONF_DIR"

if [[ ! -d "$APP_DIR/venv" ]]; then
  python3 -m venv --copies "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet --upgrade garminconnect curl_cffi openai
echo "  已安裝 garminconnect $("$APP_DIR/venv/bin/pip" show garminconnect | awk '/^Version/{print $2}')"
echo "  已安裝 openai $("$APP_DIR/venv/bin/pip" show openai | awk '/^Version/{print $2}')"

install -m 0755 "$SRC_DIR/garmin_endurance.py" "$APP_DIR/garmin_endurance.py"
install -m 0755 "$SRC_DIR/telegram_adapter.py" "$APP_DIR/telegram_adapter.py"
install -d -m 0755 "$APP_DIR/agent"
install -m 0644 "$SRC_DIR"/agent/*.py "$APP_DIR/agent/"

# --------------------------------------------------------------------------- #
say "寫入設定檔"

if [[ ! -f "$CONF_DIR/env" ]]; then
  cat > "$CONF_DIR/env" <<'EOF'
# Garmin 憑證 —— 只有第一次 login 需要，之後靠 token 自動續期。
# 登入成功後可以把這兩行註解掉或清空。
GARMIN_EMAIL=
GARMIN_PASSWORD=

GARMIN_TOKENS=%h/.garminconnect
GARMIN_DB=%h/.local/share/garmin-endurance/garmin.db
GARMIN_BACKFILL_DAYS=45

# intervals.icu —— merge 子命令用。
# key 在 intervals.icu → Settings → Developer Settings 產生。
# ICU_ATHLETE_ID 留 0 即可，會由 key 自動解析。
ICU_API_KEY=
ICU_ATHLETE_ID=0

# Telegram agent —— garmin-agent.service 用，不跑 agent 就留空。
# TELEGRAM_BOT_TOKEN 跟 @BotFather 申請。
# TELEGRAM_ALLOWED_CHAT_IDS 是逗號分隔的白名單，沒填 agent 會拒絕啟動
# （不然任何人找到這個 bot 都能查你的健康資料）。
# 查自己的 chat id：先跟 bot 說一句話，再開
#   https://api.telegram.org/bot<TOKEN>/getUpdates
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=

# OpenAI，agent 的大腦。
# OPENAI_MODEL 留空會用 agent/core.py 的預設值；型號設錯時 agent 會在啟動時
# 直接報錯並列出這把金鑰實際可用的型號，不會等到你傳訊息才 404。
OPENAI_API_KEY=
OPENAI_MODEL=
EOF
  # systemd 的 EnvironmentFile 不展開 %h，這裡直接寫成絕對路徑
  sed -i "s|%h|$HOME|g" "$CONF_DIR/env"
  chmod 600 "$CONF_DIR/env"
  echo "  已建立 $CONF_DIR/env（權限 600）"
else
  echo "  $CONF_DIR/env 已存在，保留不動"
fi

# 方便手動呼叫的包裝
cat > "$BIN_DIR/garmin-endurance" <<EOF
#!/usr/bin/env bash
set -a
[[ -f "$CONF_DIR/env" ]] && source "$CONF_DIR/env"
set +a
exec "$APP_DIR/venv/bin/python" "$APP_DIR/garmin_endurance.py" "\$@"
EOF
chmod 755 "$BIN_DIR/garmin-endurance"

# --------------------------------------------------------------------------- #
say "安裝 systemd unit"

install -m 0644 "$SRC_DIR/garmin-endurance.service" "$UNIT_DIR/"
install -m 0644 "$SRC_DIR/garmin-endurance.timer"   "$UNIT_DIR/"
install -m 0644 "$SRC_DIR/garmin-agent.service"     "$UNIT_DIR/"
systemctl --user daemon-reload

# --------------------------------------------------------------------------- #
say "完成。接下來手動做這三步："

cat <<EOF

  1. 填入帳密：
       nano $CONF_DIR/env

  2. 首次登入（會提示 MFA 驗證碼，一定要在終端機手動跑一次）：
       garmin-endurance login

     成功後 token 會存到 ~/.garminconnect，之後自動續期，
     此時可以把 env 裡的密碼清掉。

  3. 試跑一次，確認端點有回資料：
       garmin-endurance fetch -v
       garmin-endurance report

     若解析不到分數，跑 \`garmin-endurance probe\` 看原始 JSON。

  4.（選用）填入 ICU_API_KEY 後，把 intervals.icu 的 CTL/ATL 併進來：
       garmin-endurance merge --days 60

  一切正常後，啟用排程（每天 07:20 與 19:20）：
       systemctl --user enable --now garmin-endurance.timer
       systemctl --user list-timers garmin-endurance.timer

  查看執行紀錄：
       journalctl --user -u garmin-endurance.service -n 50

  5.（選用）啟用 Telegram agent：
       在 $CONF_DIR/env 填入 TELEGRAM_BOT_TOKEN、
       TELEGRAM_ALLOWED_CHAT_IDS、OPENAI_API_KEY，然後：

       systemctl --user enable --now garmin-agent.service
       journalctl --user -u garmin-agent.service -f

EOF
