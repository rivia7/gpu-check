#!/bin/sh
# gpu-check 中转站同步脚本：定时从 GitHub 拉取 main 分支和所有版本标签，放到 $OUT 供 nginx 提供下载。
# 走 codeload.github.com（源码 tar 包）和 api.github.com（标签列表），不依赖常被墙的 raw.githubusercontent.com。
#
# 环境变量：
#   PUBLIC_BASE  中转站对外地址（必填），如 http://1.2.3.4/gpu；会写进 run.sh，作为默认下载源
#   REPO         GitHub 仓库，默认 rivia7/gpu-check
#   INTERVAL     同步间隔（秒），默认 600
#   OUT          输出目录，默认 /data
#
# 产物：$OUT/run.sh、$OUT/gpu_check.py（= main），$OUT/<版本>/{gpu_check.py,run.sh,SHA256SUMS}，$OUT/index.txt
# 项目原名 h100-check：v1.3.0 及以前的版本里脚本叫 h100_check.py，同步时保留原文件名。
# 脚本与 GitHub 上的文件逐字节一致；只有 run.sh 被改写了下载地址（标签目录里的 run.sh 默认运行同一版本）。
set -u
REPO="${REPO:-rivia7/gpu-check}"
OUT="${OUT:-/data}"
INTERVAL="${INTERVAL:-600}"
PUBLIC_BASE="${PUBLIC_BASE:-}"
PUBLIC_BASE="${PUBLIC_BASE%/}"

log() { echo "$(date '+%F %T') $*"; }

case "$PUBLIC_BASE" in
    http://*|https://*) ;;
    *) log "PUBLIC_BASE 未设置或格式不对（应为 http(s)://主机[:端口]/路径）：'${PUBLIC_BASE}'"; exit 1 ;;
esac
case "$PUBLIC_BASE" in
    *[!A-Za-z0-9.:/_-]*) log "PUBLIC_BASE 含有不支持的字符：'${PUBLIC_BASE}'"; exit 1 ;;
esac
case "$REPO" in
    *[!A-Za-z0-9._/-]*|*..*) log "REPO 不合法：'${REPO}'"; exit 1 ;;
esac

# 原子替换：先写临时文件再 rename，下载中途不会读到半个文件
put() {   # put <源文件> <目标文件>
    cp "$1" "$2.tmp.$$" && mv -f "$2.tmp.$$" "$2"
}

# 目录里的脚本文件名：v1.4.0 起为 gpu_check.py，之前为 h100_check.py
pyname() {   # pyname <目录>
    if [ -f "$1/gpu_check.py" ]; then
        echo gpu_check.py
    elif [ -f "$1/h100_check.py" ]; then
        echo h100_check.py
    else
        return 1
    fi
}

# 同步一个版本（分支或标签）到 $OUT/<ref>/
sync_ref() {
    ref="$1"
    tmp="$(mktemp -d)" || return 1
    if ! curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 \
            -o "$tmp/src.tgz" "https://codeload.github.com/${REPO}/tar.gz/${ref}"; then
        log "下载失败：${ref}"
        rm -rf "$tmp"
        return 1
    fi
    mkdir "$tmp/x"
    if ! tar -xzf "$tmp/src.tgz" -C "$tmp/x"; then
        log "解包失败：${ref}"
        rm -rf "$tmp"
        return 1
    fi
    set -- "$tmp"/x/*/
    src="${1%/}"
    name="$(pyname "$src")" || name=''
    if [ "$#" -ne 1 ] || [ -z "$name" ] || [ ! -s "$src/run.sh" ] ||
       [ "$(tail -n 1 "$src/$name" 2>/dev/null)" != "# ---- end of ${name} ----" ]; then
        log "内容不完整，跳过：${ref}"
        rm -rf "$tmp"
        return 1
    fi
    # 默认下载源改成中转站（新旧仓库名都认）；标签目录里的 run.sh 默认运行同一版本（新旧两种写法都认）
    sed -e "s#https://raw\.githubusercontent\.com/[A-Za-z0-9._-]*/[A-Za-z0-9._-]*#${PUBLIC_BASE}#g" \
        -e "s#^\( *local default_ref=\)'main'#\1'${ref}'#" \
        -e "s#H100_CHECK_REF:-main#H100_CHECK_REF:-${ref}#" "$src/run.sh" >"$tmp/run.sh"
    if grep -q 'raw\.githubusercontent\.com' "$tmp/run.sh" || ! grep -q "${PUBLIC_BASE}" "$tmp/run.sh" ||
       ! grep -q -e "default_ref='${ref}'" -e "H100_CHECK_REF:-${ref}}" "$tmp/run.sh"; then
        log "改写 run.sh 失败：${ref}"
        rm -rf "$tmp"
        return 1
    fi
    mkdir -p "$OUT/$ref"
    put "$src/$name" "$OUT/$ref/$name"
    put "$tmp/run.sh" "$OUT/$ref/run.sh"
    for f in gpu_check.py h100_check.py; do [ "$f" = "$name" ] || rm -f "$OUT/$ref/$f"; done
    (cd "$OUT/$ref" && sha256sum "$name" run.sh >"$tmp/SHA256SUMS") && put "$tmp/SHA256SUMS" "$OUT/$ref/SHA256SUMS"
    rm -rf "$tmp"
    return 0
}

list_tags() {
    curl -fsSL --connect-timeout 15 --max-time 30 "https://api.github.com/repos/${REPO}/tags?per_page=100" |
        sed -n 's/^ *"name": *"\([^"]*\)".*/\1/p' | grep -E '^v?[0-9][0-9A-Za-z._-]*$'
}

write_index() {
    name="$(pyname "$OUT/main")" || name=gpu_check.py
    ver="$(sed -n "s/^VERSION = '\(.*\)'\$/\1/p" "$OUT/main/$name" 2>/dev/null)"
    sum="$(cd "$OUT/main" 2>/dev/null && sha256sum "$name" | cut -d' ' -f1)"
    refs="$(cd "$OUT" && for d in */; do pyname "$d" >/dev/null && printf '%s ' "${d%/}"; done)"
    cat >"$OUT/index.txt.tmp" <<EOF
gpu-check 中转站 —— 自动同步自 https://github.com/${REPO}（每 ${INTERVAL} 秒）
最近成功同步：${LAST_OK:-尚未成功}    最近一次尝试：${LAST_TRY}${LAST_ERR:+（失败：${LAST_ERR}）}
main 版本：${ver:--}    ${name} sha256：${sum:--}

一键运行（在 GPU 服务器上，只读检测，不影响线上业务）：
  curl -fsSL ${PUBLIC_BASE}/run.sh | sudo bash

不加参数时默认按 H100 SXM5（NVLink 版）核验。其它机器用 --expect 指定期望型号，参数接在 bash -s -- 后面：
  curl -fsSL ${PUBLIC_BASE}/run.sh | sudo bash -s -- --expect 4090

不确定型号时，用 --expect any 只识别型号、检查各数据源是否一致：
  curl -fsSL ${PUBLIC_BASE}/run.sh | sudo bash -s -- --expect any

运行指定版本：
  curl -fsSL ${PUBLIC_BASE}/<版本>/run.sh | sudo bash

没有 curl 时：
  wget -qO- ${PUBLIC_BASE}/run.sh | sudo bash

可用版本：${refs:--}
每个版本目录下有 gpu_check.py（v1.3.0 及以前叫 h100_check.py）、run.sh、SHA256SUMS。
注意：中转站走 HTTP 明文。脚本与 GitHub 上的文件逐字节一致，可用 sha256 比对。
EOF
    mv -f "$OUT/index.txt.tmp" "$OUT/index.txt"
}

mkdir -p "$OUT"
LAST_OK=''
while :; do
    LAST_TRY="$(date '+%F %T')"
    LAST_ERR=''
    if sync_ref main && name="$(pyname "$OUT/main")"; then
        put "$OUT/main/run.sh" "$OUT/run.sh"
        put "$OUT/main/$name" "$OUT/$name"
        for f in gpu_check.py h100_check.py; do [ "$f" = "$name" ] || rm -f "$OUT/$f"; done
    else
        LAST_ERR='main 同步失败'
    fi
    if tags="$(list_tags)"; then
        for t in $tags; do
            pyname "$OUT/$t" >/dev/null && continue      # 标签内容不变，已有的不再重复下载
            if sync_ref "$t"; then log "新增版本：$t"; else LAST_ERR="${LAST_ERR:+$LAST_ERR；}$t 同步失败"; fi
        done
    else
        LAST_ERR="${LAST_ERR:+$LAST_ERR；}获取标签列表失败"
    fi
    [ -z "$LAST_ERR" ] && LAST_OK="$LAST_TRY"
    write_index
    log "同步完成${LAST_ERR:+，有错误：$LAST_ERR}"
    sleep "$INTERVAL"
done
