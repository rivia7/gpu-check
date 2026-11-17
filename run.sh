#!/usr/bin/env bash
# gpu-check 一键运行入口（只读检测，不影响线上业务）
#
#   curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash -s -- --expect 4090 -o /tmp/gpu_report.txt
#
# 只做三件事：找到 Python 3.6+ → 把 gpu_check.py 下载到内存并检查是否完整 → 交给 Python 从标准输入执行。
# 除了 -o 指定的报告文件，不在磁盘上写任何东西。
#
# 可选环境变量（经 sudo 传入时写成 sudo env 变量=值 bash；改名前的 H100_CHECK_* 旧变量名仍然有效）：
#   GPU_CHECK_REF     运行哪个版本（分支/标签），默认 main
#   GPU_CHECK_BASE    下载源根地址，默认 GitHub；脚本从 <BASE>/<REF>/gpu_check.py 下载（中转站会把默认值改成自己）
#   GPU_CHECK_URL     直接指定 gpu_check.py 的完整下载地址，优先于以上两项
#   GPU_CHECK_PYTHON  指定 Python 解释器
#
# 全部代码包在 main 函数里：下载不完整时 bash 解析不到结尾，什么都不会执行。

main() {
    set -u
    local default_ref='main'          # 中转站会把标签目录里的这个值改成对应版本
    local default_base='https://raw.githubusercontent.com/rivia7/gpu-check'
    local ref="${GPU_CHECK_REF:-${H100_CHECK_REF:-$default_ref}}"
    local base="${GPU_CHECK_BASE:-${H100_CHECK_BASE:-$default_base}}"
    local url="${GPU_CHECK_URL:-${H100_CHECK_URL:-${base%/}/${ref}/gpu_check.py}}"
    local marker='# ---- end of gpu_check.py ----'
    local py="" c src

    for c in ${GPU_CHECK_PYTHON:-${H100_CHECK_PYTHON:-}} python3 /usr/libexec/platform-python \
             python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3.6 python; do
        if command -v "$c" >/dev/null 2>&1 &&
           "$c" -c 'import sys; sys.exit(sys.version_info < (3, 6))' >/dev/null 2>&1; then
            py="$c"
            break
        fi
    done
    if [ -z "$py" ]; then
        echo "gpu_check: 需要 Python 3.6+（未找到 python3 / platform-python）" >&2
        return 3
    fi

    echo "gpu_check: 下载 ${url}" >&2
    if command -v curl >/dev/null 2>&1; then
        src="$(curl -fsSL --connect-timeout 10 --max-time 120 --retry 2 "$url")" || src=""
    elif command -v wget >/dev/null 2>&1; then
        src="$(wget -qO- -T 30 "$url")" || src=""
    else
        echo "gpu_check: 需要 curl 或 wget" >&2
        return 3
    fi
    case "$src" in
        *"$marker"*) ;;
        *)
            echo "gpu_check: 下载失败或内容不完整：${url}" >&2
            echo "  服务器访问不了 GitHub 时：用 GPU_CHECK_BASE 指向中转站，或在能上网的机器上下载 gpu_check.py 后执行：" >&2
            echo "    ssh <服务器> 'sudo python3 -' < gpu_check.py" >&2
            return 3
            ;;
    esac

    printf '%s\n' "$src" | "$py" - "$@"
}

main "$@"
