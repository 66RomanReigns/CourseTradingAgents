#!/usr/bin/env bash
set -euo pipefail

KEYS_FILE="${TRADINGLAB_KEYS_FILE:-$HOME/.config/tradinglab/tradinglab.keys}"
if [[ ! -f "$KEYS_FILE" ]]; then
  echo "error: API key file not found: $KEYS_FILE" >&2
  echo "run: scripts/configure_api_keys.sh" >&2
  exit 2
fi
permissions="$(stat -c '%a' "$KEYS_FILE")"
if [[ "$permissions" != "600" ]]; then
  echo "error: API key file permissions must be 600, found $permissions" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$KEYS_FILE"
set +a

required=(TWELVE_DATA_API_KEY ALPHA_VANTAGE_API_KEY FRED_API_KEY SEC_USER_AGENT GOOGLE_API_KEY ZHIPU_API_KEY TRADINGLAB_API_TOKEN)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "error: missing $name in $KEYS_FILE" >&2
    exit 2
  fi
done

if [[ $# -eq 0 ]]; then
  printf '%s\n' "API keys loaded from $KEYS_FILE"
  printf '%s\n' "available variables: ${required[*]}"
  exit 0
fi
export ZHIPU_BASE_URL="${ZHIPU_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
export ZHIPU_MODEL="${ZHIPU_MODEL:-glm-4.7-flash}"
exec "$@"
