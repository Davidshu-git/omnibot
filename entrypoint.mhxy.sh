#!/bin/sh
set -eu

# Canonical mhxy env names. Export legacy names only inside the process for
# modules that have not yet been migrated.
export TG_BOT_TOKEN="${MHXY_TG_BOT_TOKEN:-${TG_BOT_TOKEN:-}}"
export ALLOWED_TG_USERS="${MHXY_ALLOWED_TG_USERS:-${ALLOWED_TG_USERS:-}}"
export GAME_TG_BOT_TOKEN="${MHXY_TG_BOT_TOKEN:-${GAME_TG_BOT_TOKEN:-}}"
export GAME_ALLOWED_TG_USERS="${MHXY_ALLOWED_TG_USERS:-${GAME_ALLOWED_TG_USERS:-}}"
export REMOTE_MODE="${MHXY_REMOTE_MODE:-${REMOTE_MODE:-false}}"
export REMOTE_HOST="${MHXY_REMOTE_HOST:-${REMOTE_HOST:-}}"
export REMOTE_USER="${MHXY_REMOTE_USER:-${REMOTE_USER:-}}"

case "$(printf '%s' "$REMOTE_MODE" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    if [ -z "$REMOTE_HOST" ] || [ -z "$REMOTE_USER" ]; then
      echo "MHXY_REMOTE_MODE is enabled, but MHXY_REMOTE_HOST or MHXY_REMOTE_USER is missing" >&2
      exit 1
    fi

    mkdir -p /root/.ssh_runtime
    if [ -f /root/.ssh_host/id_towin ]; then
      cp /root/.ssh_host/id_towin /root/.ssh_runtime/id_towin
      chmod 600 /root/.ssh_runtime/id_towin
    else
      echo "MHXY remote mode requires /root/.ssh_host/id_towin" >&2
      exit 1
    fi

    cat > /root/.ssh_runtime/config <<EOF
Host ${REMOTE_HOST}
    IdentityFile /root/.ssh_runtime/id_towin
    StrictHostKeyChecking no
    User ${REMOTE_USER}
EOF
    chmod 600 /root/.ssh_runtime/config
    ln -sfn /root/.ssh_runtime /root/.ssh
    ;;
esac

exec "$@"
