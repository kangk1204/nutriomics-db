#!/bin/sh
set -eu
APP_DIR=/home/keunsoo/projects/nutriomics-literature
export LITERATURE_HOME="${LITERATURE_HOME:-$APP_DIR/runtime}"
[ ! -f "$LITERATURE_HOME/maintenance.flag" ] || exit 0
mkdir -p "$LITERATURE_HOME/logs"
cd "$APP_DIR"
if ! .venv/bin/supervisorctl -c scripts/supervisord.conf pid >/dev/null 2>&1; then
    .venv/bin/supervisord -c scripts/supervisord.conf
fi
# Supervisor handles unexpected exits; retry services that exhausted startup attempts.
for service in literature_api literature_ui source_oa source_extract source_extract_v4 source_extract_v5; do
    state=$(.venv/bin/supervisorctl -c scripts/supervisord.conf status "$service" 2>/dev/null || true)
    case "$state" in
        *FATAL*|*STOPPED*) .venv/bin/supervisorctl -c scripts/supervisord.conf start "$service" ;;
    esac
done
