#!/usr/bin/env bash
# Broker launcher: log in to Infisical with the machine identity from
# /etc/coach/infisical.env (already in the environment via systemd), fetch the
# prod secrets, and exec the broker with them as environment variables.
set -euo pipefail
: "${INFISICAL_PROJECT_ID:?set in /etc/coach/broker.env}"
: "${INFISICAL_UNIVERSAL_AUTH_CLIENT_ID:?set in /etc/coach/infisical.env}"
: "${INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET:?set in /etc/coach/infisical.env}"
ENV_NAME="${INFISICAL_ENV:-dev}"
TOKEN="$(infisical login --method=universal-auth \
  --client-id="$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" \
  --client-secret="$INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET" --silent --plain)"
unset INFISICAL_UNIVERSAL_AUTH_CLIENT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET
exec infisical run --silent --token="$TOKEN" --projectId="$INFISICAL_PROJECT_ID" --env="$ENV_NAME" -- "$@"
