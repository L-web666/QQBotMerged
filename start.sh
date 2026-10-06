#!/usr/bin/env bash
# ============================================================
#  QQ 机器人合并版 · Linux/macOS 启动脚本
#  崩溃自动重启；连续失败 5 次（每次运行不足 60 秒算失败）就停止。
# ============================================================
set -u

cd "$(dirname "$0")" || { echo "[错误] 无法进入脚本所在目录"; exit 1; }

PY=${PYTHON:-python3}
MAX_FAILS=5
MIN_UPTIME=60

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[错误] 没有找到 $PY，请先安装 Python 3.8+"
  exit 1
fi

if ! "$PY" -c "import flask, requests, websocket" >/dev/null 2>&1; then
  if [ -f requirements.txt ]; then
    echo "[提示] 缺少依赖，正在安装..."
    "$PY" -m pip install -r requirements.txt || {
      echo "[错误] 依赖安装失败，请手动执行：$PY -m pip install -r requirements.txt"
      exit 1
    }
  else
    echo "[错误] 缺少依赖且没有 requirements.txt，请手动安装：flask requests websocket-client"
    exit 1
  fi
fi

# Ctrl+C 处理：收到一次 SIGINT 就设置标志并退出循环
STOP=0
trap 'STOP=1; echo; echo "[信息] 收到 Ctrl+C，正在停止..."' INT TERM

FAIL_COUNT=0

while [ "$STOP" -eq 0 ]; do
  echo "============================================================"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 正在启动 QQ 机器人合并版 ..."
  echo "============================================================"

  START=$(date +%s)
  "$PY" run.py
  CODE=$?
  END=$(date +%s)
  UPTIME=$((END - START))

  # 被 Ctrl+C 打断，直接退出
  if [ "$STOP" -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已停止。"
    exit 0
  fi

  if [ "$UPTIME" -ge "$MIN_UPTIME" ]; then
    FAIL_COUNT=0
    echo "[信息] 本次运行 $UPTIME 秒，失败计数已清零。"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "[警告] 本次只运行了 $UPTIME 秒（退出码 $CODE），连续失败：$FAIL_COUNT / $MAX_FAILS"
  fi

  if [ "$FAIL_COUNT" -ge "$MAX_FAILS" ]; then
    echo
    echo "[错误] 连续 $MAX_FAILS 次启动后很快就退出，已停止自动重启。"
    echo "       请查看上面的日志找原因（常见：端口被占用、config.json 损坏、依赖缺失）。"
    exit 1
  fi

  echo "[信息] 3 秒后重启（Ctrl+C 停止）..."
  # sleep 放到后台，等它或 STOP 信号
  sleep 3 &
  SLEEP_PID=$!
  wait "$SLEEP_PID" 2>/dev/null
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已停止。"