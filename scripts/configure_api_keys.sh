#!/usr/bin/env bash
set -euo pipefail

TARGET="${TRADINGLAB_KEYS_FILE:-$HOME/.config/tradinglab/tradinglab.keys}"
mkdir -p "$(dirname "$TARGET")"
umask 077

echo "TradeLab-Agent API key setup"
echo "Values are read silently and written outside the Git repository."

read -rsp "Twelve Data API key: " TWELVE_DATA_API_KEY
echo
read -rsp "Alpha Vantage API key: " ALPHA_VANTAGE_API_KEY
echo
read -rsp "FRED API key: " FRED_API_KEY
echo
read -rp "SEC User-Agent (application + contact email): " SEC_USER_AGENT
read -rsp "Google/Gemini API key: " GOOGLE_API_KEY
echo
read -rsp "Zhipu/GLM API key: " ZHIPU_API_KEY
echo
read -r TRADINGLAB_API_TOKEN < <(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

for name in TWELVE_DATA_API_KEY ALPHA_VANTAGE_API_KEY FRED_API_KEY SEC_USER_AGENT GOOGLE_API_KEY ZHIPU_API_KEY TRADINGLAB_API_TOKEN; do
  value="${!name}"
  if [[ -z "$value" ]]; then
    echo "error: $name cannot be empty" >&2
    exit 2
  fi
done

temporary="${TARGET}.tmp.$$"
cat >"$temporary" <<EOF
TWELVE_DATA_API_KEY=${TWELVE_DATA_API_KEY}
ALPHA_VANTAGE_API_KEY=${ALPHA_VANTAGE_API_KEY}
FRED_API_KEY=${FRED_API_KEY}
SEC_USER_AGENT=${SEC_USER_AGENT}
GOOGLE_API_KEY=${GOOGLE_API_KEY}
ZHIPU_API_KEY=${ZHIPU_API_KEY}
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ZHIPU_MODEL=glm-4.7-flash
TRADINGLAB_API_TOKEN=${TRADINGLAB_API_TOKEN}
EOF
chmod 600 "$temporary"
mv "$temporary" "$TARGET"

unset TWELVE_DATA_API_KEY ALPHA_VANTAGE_API_KEY FRED_API_KEY SEC_USER_AGENT GOOGLE_API_KEY ZHIPU_API_KEY TRADINGLAB_API_TOKEN
printf 'saved: %s\n' "$TARGET"
printf 'permissions: %s\n' "$(stat -c '%a' "$TARGET")"
printf '%s\n' 'The file is outside the repository and values were not printed.'
