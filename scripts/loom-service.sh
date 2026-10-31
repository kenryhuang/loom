#!/usr/bin/env bash
# Manage the Loom local service (loom serve).
#
# Usage:
#   scripts/loom-service.sh start [-- extra args for `loom serve`]
#   scripts/loom-service.sh stop
#   scripts/loom-service.sh status
#   scripts/loom-service.sh restart [-- extra args for `loom serve`]
#
# Overrides (env):
#   LOOM_HOST      bind host          (default 127.0.0.1)
#   LOOM_PORT      bind port          (default 8765)
#   LOOM_DATA_DIR  service data dir   (default <repo>/runs/service)
#   LOOM_CONFIG    model config file  (default <repo>/config.yaml, then config.yml)
#   LOOM_BIN       loom executable    (default <repo>/.venv/bin/loom, falls back to `uv run loom`)
# Explicit --config overrides LOOM_CONFIG. Restart uses the arguments supplied
# on that invocation; repeat any extra flags that should remain enabled.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${LOOM_HOST:-127.0.0.1}"
PORT="${LOOM_PORT:-8765}"
DATA_DIR="${LOOM_DATA_DIR:-$ROOT/runs/service}"
PID_FILE="$DATA_DIR/loom.pid"
LOG_FILE="$DATA_DIR/loom.log"
STOP_TIMEOUT="${LOOM_STOP_TIMEOUT:-20}"
START_TIMEOUT="${LOOM_START_TIMEOUT:-15}"

if [[ -x "${LOOM_BIN:-}" ]]; then
    LAUNCH=("$LOOM_BIN")
elif [[ -x "$ROOT/.venv/bin/loom" ]]; then
    LAUNCH=("$ROOT/.venv/bin/loom")
else
    LAUNCH=(uv run loom)
fi

# Print the service PID if it is recorded, alive, and really a `loom serve` process.
running_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o command= 2>/dev/null | grep -q "loom.*serve" || return 1
    echo "$pid"
}

# Any HTTP response (even 401) means the server is answering on the port.
port_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://$HOST:$PORT/" 2>/dev/null || true
}

# Resolve before restart stops anything, so a missing config leaves it running.
prepare_config() {
    CONFIG_PATH="${LOOM_CONFIG:-}"
    CONFIG_EXPLICIT=0
    while (( $# )); do
        case "$1" in
            --config)
                [[ $# -ge 2 && -n "$2" ]] || { echo "error: --config requires a file" >&2; return 1; }
                CONFIG_PATH="$2"
                CONFIG_EXPLICIT=1
                shift 2
                ;;
            --config=*)
                CONFIG_PATH="${1#--config=}"
                [[ -n "$CONFIG_PATH" ]] || { echo "error: --config requires a file" >&2; return 1; }
                CONFIG_EXPLICIT=1
                shift
                ;;
            *) shift ;;
        esac
    done
    if [[ -z "$CONFIG_PATH" ]]; then
        if [[ -f "$ROOT/config.yaml" ]]; then
            CONFIG_PATH="$ROOT/config.yaml"
        elif [[ -f "$ROOT/config.yml" ]]; then
            CONFIG_PATH="$ROOT/config.yml"
        fi
    fi
    if [[ -n "$CONFIG_PATH" && ! -f "$CONFIG_PATH" ]]; then
        echo "error: config file not found: $CONFIG_PATH" >&2
        return 1
    fi
}

cmd_start() {
    local pid
    if pid="$(running_pid)"; then
        echo "loom service already running (pid $pid, http://$HOST:$PORT)"
        return 0
    fi
    rm -f "$PID_FILE"
    if [[ "$(port_code)" != "000" ]]; then
        echo "error: http://$HOST:$PORT already answers, but no loom service pid file; refusing to start" >&2
        exit 1
    fi
    mkdir -p "$DATA_DIR"
    echo "starting loom service on $HOST:$PORT (log: $LOG_FILE)"
    local serve_args=(serve --host "$HOST" --port "$PORT" --data-dir "$DATA_DIR")
    if [[ -n "$CONFIG_PATH" ]]; then
        echo "  config:    $CONFIG_PATH"
        if [[ "$CONFIG_EXPLICIT" == 0 ]]; then
            serve_args+=(--config "$CONFIG_PATH")
        fi
    fi
    nohup "${LAUNCH[@]}" "${serve_args[@]}" "$@" \
        >>"$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" >"$PID_FILE"
    local waited=0
    while (( waited < START_TIMEOUT )); do
        if ! kill -0 "$new_pid" 2>/dev/null; then
            echo "error: loom service exited during startup; last log lines:" >&2
            tail -n 20 "$LOG_FILE" >&2
            rm -f "$PID_FILE"
            exit 1
        fi
        if [[ "$(port_code)" != "000" ]]; then
            echo "loom service started (pid $new_pid)"
            echo "  api:       http://$HOST:$PORT"
            echo "  web:       http://$HOST:$PORT/web/"
            echo "  credential: $DATA_DIR/credential"
            return 0
        fi
        sleep 1
        (( waited += 1 ))
    done
    echo "error: service process alive (pid $new_pid) but port $PORT not answering after ${START_TIMEOUT}s; see $LOG_FILE" >&2
    exit 1
}

cmd_stop() {
    local pid
    if ! pid="$(running_pid)"; then
        echo "loom service not running"
        rm -f "$PID_FILE"
        return 0
    fi
    echo "stopping loom service (pid $pid)"
    kill "$pid" 2>/dev/null || true
    local waited=0
    while (( waited < STOP_TIMEOUT )); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
        (( waited += 1 ))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "did not exit after ${STOP_TIMEOUT}s; sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
    echo "loom service stopped"
}

cmd_status() {
    local pid
    if ! pid="$(running_pid)"; then
        echo "loom service not running"
        [[ -f "$PID_FILE" ]] && echo "  (stale pid file: $PID_FILE)"
        return 3
    fi
    local code
    code="$(port_code)"
    echo "loom service running (pid $pid)"
    ps -p "$pid" -o etime=,rss= | awk '{printf "  uptime: %s  rss: %.0f MB\n", $1, $2/1024}'
    if [[ "$code" != "000" ]]; then
        echo "  http:  http://$HOST:$PORT responding (status $code)"
        echo "  web:   http://$HOST:$PORT/web/"
    else
        echo "  warning: process alive but http://$HOST:$PORT not responding" >&2
    fi
    echo "  log:   $LOG_FILE"
}

cmd="${1:-}"
shift || true
[[ "${1:-}" != "--" ]] || shift
case "$cmd" in
    start)   prepare_config "$@"; cmd_start "$@" ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    restart)
        prepare_config "$@"
        cmd_stop
        cmd_start "$@"
        ;;
    *)
        echo "usage: $0 {start|stop|status|restart} [-- extra 'loom serve' args]" >&2
        exit 2
        ;;
esac
