#!/usr/bin/env bash
# Run inside the logged-in Linux desktop session; never manufacture its environment.
set -uo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 2
mode=launch
software=1
args=()

usage() {
    cat <<'HELP'
用法：./start-cloud.sh [启动脚本选项] [原有启动器参数]

默认优先使用项目 .venv，并为云桌面启用 --software。
  --check          检查依赖与桌面会话变量，不启动程序、不读取凭据
  --dry-run        仅显示启动计划，不检查依赖、不创建日志、不启动程序
  --native-render  不自动添加 --software；保留用户已有 Qt 环境设置
  -h, --help       显示此帮助
  --              后续参数全部传给 launcher.py

原有参数继续支持：--module settings、--graphics-info、--development 等。
COAT_PYTHON 可指定解释器路径；否则依次查找 .venv、.conda、python3。
正常启动的日志位于 artifacts/logs/cloud-*.log；不会自动安装软件。
HELP
}

while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --check) mode=check ;;
        --dry-run) mode=dry-run ;;
        --native-render) software=0 ;;
        --) shift; args+=("$@"); break ;;
        *) args+=("$1") ;;
    esac
    shift
done

if [[ -n ${COAT_PYTHON:-} ]]; then
    python=$COAT_PYTHON
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    python="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.conda/bin/python" ]]; then
    python="$ROOT/.conda/bin/python"
else
    python=python3
fi
python=$(command -v -- "$python") || {
    printf '%s\n' '错误：找不到 Python。请按 README 的 Linux 部署步骤创建 .venv，或设置 COAT_PYTHON。' >&2
    exit 2
}
# Resolve a caller-relative override before changing the working directory.
if [[ $python != /* ]]; then
    python="$(CDPATH= cd -- "$(dirname -- "$python")" && pwd)/$(basename -- "$python")"
fi
cd -- "$ROOT" || exit 2
if ((software)); then
    found=0
    for arg in "${args[@]}"; do
        [[ $arg == --software ]] && found=1
    done
    ((found)) || args=(--software "${args[@]}")
fi

if [[ $mode == dry-run ]]; then
    printf '项目：%s\nPython：%s\n启动计划（未执行）：' "$ROOT" "$python"
    printf '%q ' "$python" -u "$ROOT/launcher.py" "${args[@]}"
    printf '\n'
    exit 0
fi

# --check is read-only. Only an actual launch creates a private diagnostic log.
if [[ $mode == launch ]]; then
    umask 077
    mkdir -p -- "$ROOT/artifacts/logs" || exit 2
    log=$(mktemp "$ROOT/artifacts/logs/cloud-$(date +%Y%m%d-%H%M%S)-XXXXXX.log") || exit 2
    printf '启动日志：%s\n' "$log"
    exec > >(tee -a -- "$log") 2>&1
fi
printf '项目：%s\nPython：%s\n' "$ROOT" "$python"
PYTHONDONTWRITEBYTECODE=1 "$python" "$ROOT/tools/check_cloud_runtime.py"
status=$?
if ((status)); then
    printf '依赖检查失败。请按 README 安装依赖；脚本不会自动下载或修改环境。\n'
    exit "$status"
fi

# Visible GUI only. Headless computations use the CLI/MCP entry instead.
platform=${QT_QPA_PLATFORM:-}
display=${DISPLAY:-}
for ((i=0; i<${#args[@]}; i++)); do
    case "${args[i]}" in
        -platform) platform=${args[i+1]:-} ;;
        -platform=*) platform=${args[i]#-platform=} ;;
        -display) display=${args[i+1]:-} ;;
        -display=*) display=${args[i]#-display=} ;;
    esac
done
case "$platform" in
    offscreen*|minimal*)
        printf '错误：offscreen/minimal 不会显示桌面窗口。无头计算请使用 README 的 MCP／Python 核心接口。\n' >&2
        exit 3 ;;
esac
if [[ $platform == xcb* && -z $display ]]; then
    printf '错误：Qt xcb 平台需要 DISPLAY。请从使用 X11/XWayland 的桌面会话启动。\n' >&2
    exit 3
fi
if [[ $platform == wayland* && -z ${WAYLAND_DISPLAY:-} ]]; then
    printf '错误：Qt Wayland 平台需要 WAYLAND_DISPLAY。请从 Wayland 桌面会话启动。\n' >&2
    exit 3
fi
if [[ -z $display && -z ${WAYLAND_DISPLAY:-} ]]; then
    printf '错误：当前进程没有 DISPLAY 或 WAYLAND_DISPLAY。请从已登录的远程桌面终端运行此脚本。\n' >&2
    printf '普通 SSH/任务终端不一定共享桌面会话；不要猜测 DISPLAY=:0 或复制其他用户的 D-Bus 地址。\n' >&2
    exit 3
fi
if [[ ( $platform == wayland* || -z $display ) && -z ${XDG_RUNTIME_DIR:-} ]]; then
    printf '错误：Wayland 会话缺少 XDG_RUNTIME_DIR。请回到同一用户的桌面终端重新运行。\n' >&2
    exit 3
fi
if [[ -z ${DBUS_SESSION_BUS_ADDRESS:-} ]]; then
    printf '提醒：未继承 DBUS_SESSION_BUS_ADDRESS；安全凭据服务可能不可用。图形仿真仍可尝试启动。\n'
fi
printf '依赖和会话变量检查通过；未验证显示连接、钱包解锁或 API 可用性。\n'
if [[ $mode == check ]]; then
    exit 0
fi
printf '正在启动；关闭应用窗口后脚本退出。\n'
"$python" -u "$ROOT/launcher.py" "${args[@]}"
status=$?
if ((status)); then
    printf '程序退出码：%s。请查看日志：%s\n' "$status" "$log" >&2
else
    printf '程序已正常退出。日志：%s\n' "$log"
fi
exit "$status"
