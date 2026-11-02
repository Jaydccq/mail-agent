#!/usr/bin/env bash
# Install mail-agent from this checkout, then start the setup wizard.
#   ./install.sh              install and run `mail-agent setup` (if already set up: upgrade and restart)
#   ./install.sh --no-setup   install only
# Uses uv if present (it fetches a suitable Python by itself), else pipx, else a private venv
# under ~/.local/share/mail-agent with python3 >= 3.11. Re-running it upgrades in place.
set -euo pipefail
cd "$(dirname "$0")"

BIN_DIR="$HOME/.local/bin"
run_setup=1
[ "${1:-}" = "--no-setup" ] && run_setup=0

py_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }

if command -v uv >/dev/null 2>&1; then
  echo "Installing with uv…"
  uv tool install --force --reinstall --quiet .
  BIN="$(uv tool dir --bin 2>/dev/null || echo "$BIN_DIR")/mail-agent"
elif command -v pipx >/dev/null 2>&1; then
  echo "Installing with pipx…"
  pipx install --force . >/dev/null
  BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$BIN_DIR")/mail-agent"
else
  PY=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "mail-agent needs Python 3.11+ (or uv, which brings its own Python). The quickest fix:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "then open a new terminal and run ./install.sh again."
    exit 1
  fi
  VENV="$HOME/.local/share/mail-agent/venv"
  echo "Installing into $VENV with $PY…"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet --upgrade .
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV/bin/mail-agent" "$BIN_DIR/mail-agent"
  BIN="$BIN_DIR/mail-agent"
fi

echo "✅ Installed: $BIN"
case ":$PATH:" in
  *":$(dirname "$BIN"):"*) ;;
  *) echo "   ($(dirname "$BIN") is not on your PATH yet; add it, or run mail-agent as $BIN)" ;;
esac

if [ -f "${MAIL_AGENT_HOME:-$HOME/.mail-agent}/config.toml" ]; then
  # 升级：保留配置，后台服务在跑就重启到新版本
  "$BIN" restart || true
  echo "Already set up. To change settings: mail-agent setup"
elif [ "$run_setup" = 1 ]; then
  echo
  exec "$BIN" setup
else
  echo "Next: mail-agent setup"
fi
