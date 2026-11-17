#!/bin/sh
# h100-check 中转站同步脚本：定时从 GitHub 拉取 main 分支和所有版本标签，放到 $OUT 供 nginx 提供下载。
# 走 codeload.github.com（源码 tar 包）和 api.github.com（标签列表），不依赖常被墙的 raw.githubusercontent.com。
#
# 环境变量：
#   PUBLIC_BASE  中转站对外地址（必填），如 http://1.2.3.4/h100；会写进 run.sh，作为默认下载源
#   REPO         GitHub 仓库，默认 rivia7/h100-check
#   INTERVAL     同步间隔（秒），默认 600
#   OUT          输出目录，默认 /data
#
# 产物：$OUT/run.sh、$OUT/h100_check.py（= main），$OUT/<版本>/{h100_check.py,run.sh,SHA256SUMS}，$OUT/index.txt
# h100_check.py 与 GitHub 上的文件逐字节一致；只有 run.sh 被改写了下载地址（标签目录里的 run.sh 默认运行同一版本）。
set -u
REPO="${REPO:-rivia7/h100-check}"
OUT="${OUT:-/data}"
INTERVAL="${INTERVAL:-600}"
PUBLIC_BASE="${PUBLIC_BASE:-}"
PUBLIC_BASE="${PUBLIC_BASE%/}"
MARKER='# ---- end of h100_check.py ----'
RAW="https://raw.githubusercontent.com/${REPO}"

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
    if [ "$#" -ne 1 ] || [ ! -s "$src/run.sh" ] || [ "$(tail -n 1 "$src/h100_check.py" 2>/dev/null)" != "$MARKER" ]; then
        log "内容不完整，跳过：${ref}"
        rm -rf "$tmp"
        return 1
    fi
    sed -e "s#${RAW}#${PUBLIC_BASE}#g" -e "s#H100_CHECK_REF:-main#H100_CHECK_REF:-${ref}#" "$src/run.sh" >"$tmp/run.sh"
    if grep -q 'raw\.githubusercontent\.com' "$tmp/run.sh" || ! grep -q "${PUBLIC_BASE}" "$tmp/run.sh"; then
        log "改写 run.sh 失败：${ref}"
        rm -rf "$tmp"
        return 1
    fi
    mkdir -p "$OUT/$ref"
    put "$src/h100_check.py" "$OUT/$ref/h100_check.py"
    put "$tmp/run.sh" "$OUT/$ref/run.sh"
    (cd "$OUT/$ref" && sha256sum h100_check.py run.sh >"$tmp/SHA256SUMS") && put "$tmp/SHA256SUMS" "$OUT/$ref/SHA256SUMS"
    rm -rf "$tmp"
    return 0
}

list_tags() {
    curl -fsSL --connect-timeout 15 --max-time 30 "https://api.github.com/repos/${REPO}/tags?per_page=100" |
        sed -n 's/^ *"name": *"\([^"]*\)".*/\1/p' | grep -E '^v?[0-9][0-9A-Za-z._-]*$'
}

write_index() {
    ver="$(sed -n "s/^VERSION = '\(.*\)'\$/\1/p" "$OUT/main/h100_check.py" 2>/dev/null)"
    sum="$(cd "$OUT/main" 2>/dev/null && sha256sum h100_check.py | cut -d' ' -f1)"
    refs="$(cd "$OUT" && for d in */; do [ -f "$d/h100_check.py" ] && printf '%s ' "${d%/}"; done)"
    cat >"$OUT/index.txt.tmp" <<EOF
h100-check 中转站 —— 自动同步自 https://github.com/${REPO}（每 ${INTERVAL} 秒）
最近成功同步：${LAST_OK:-尚未成功}    最近一次尝试：${LAST_TRY}${LAST_ERR:+（失败：${LAST_ERR}）}
main 版本：${ver:--}    h100_check.py sha256：${sum:--}

一键运行（在 GPU 服务器上，只读检测，不影响线上业务）：
  curl -fsSL ${PUBLIC_BASE}/run.sh | sudo bash

带参数时接在 bash -s -- 后面，例如不限型号、只做一致性检查：
  curl -fsSL ${PUBLIC_BASE}/run.sh | sudo bash -s -- --expect any

运行指定版本：
  curl -fsSL ${PUBLIC_BASE}/<版本>/run.sh | sudo bash

没有 curl 时：
  wget -qO- ${PUBLIC_BASE}/run.sh | sudo bash

可用版本：${refs:--}
每个版本目录下有 h100_check.py、run.sh、SHA256SUMS。
注意：中转站走 HTTP 明文。h100_check.py 与 GitHub 上的文件逐字节一致，可用 sha256 比对。
EOF
    mv -f "$OUT/index.txt.tmp" "$OUT/index.txt"
}

mkdir -p "$OUT"
LAST_OK=''
while :; do
    LAST_TRY="$(date '+%F %T')"
    LAST_ERR=''
    if sync_ref main; then
        put "$OUT/main/run.sh" "$OUT/run.sh"
        put "$OUT/main/h100_check.py" "$OUT/h100_check.py"
    else
        LAST_ERR='main 同步失败'
    fi
    if tags="$(list_tags)"; then
        for t in $tags; do
            [ -f "$OUT/$t/h100_check.py" ] && continue      # 标签内容不变，已有的不再重复下载
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
