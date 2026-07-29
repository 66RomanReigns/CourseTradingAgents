#!/usr/bin/env bash
set -euo pipefail

TARGET="${TRADINGLAB_KEYS_FILE:-$HOME/.config/tradinglab/tradinglab.keys}"
mkdir -p "$(dirname "$TARGET")"
umask 077

echo "TradeLab-Agent API key setup"
echo "Values are read silently and written outside the Git repository."

read -rsp "Alpha Vantage API key: " ALPHA_VANTAGE_API_KEY
echo
read -rp "SEC User-Agent (application + contact email): " SEC_USER_AGENT
read -rsp "Google/Gemini API key: " GOOGLE_API_KEY
echo
read -rsp "DeepSeek API key: " DEEPSEEK_API_KEY
echo
read -r TRADINGLAB_API_TOKEN < <(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

for name in ALPHA_VANTAGE_API_KEY SEC_USER_AGENT GOOGLE_API_KEY DEEPSEEK_API_KEY TRADINGLAB_API_TOKEN; do
  value="${!name}"
  if [[ -z "$value" ]]; then
    echo "error: $name cannot be empty" >&2
    exit 2
  fi
done

temporary="${TARGET}.tmp.$$"
cat >"$temporary" <<EOF
ALPHA_VANTAGE_API_KEY=${ALPHA_VANTAGE_API_KEY}
SEC_USER_AGENT=${SEC_USER_AGENT}
GOOGLE_API_KEY=${GOOGLE_API_KEY}
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
TRADINGLAB_API_TOKEN=${TRADINGLAB_API_TOKEN}
EOF
chmod 600 "$temporary"
mv "$temporary" "$TARGET"

unset ALPHA_VANTAGE_API_KEY SEC_USER_AGENT GOOGLE_API_KEY DEEPSEEK_API_KEY TRADINGLAB_API_TOKEN
printf 'saved: %s\n' "$TARGET"
printf 'permissions: %s\n' "$(stat -c '%a' "$TARGET")"
printf '%s\n' 'The file is outside the repository and values were not printed.'
