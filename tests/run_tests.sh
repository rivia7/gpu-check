#!/usr/bin/env bash
# 离线回归测试：为每个场景生成模拟的 sysfs/procfs 与假命令，运行 h100_check.py，校验：
#   1) 退出码；2) 报告中的关键结论；3) 只调用了白名单里的只读命令；4) stderr 无异常输出。
# 用法：tests/run_tests.sh [输出目录]        （可用 PYTHON=python3.8 指定解释器）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-${TMPDIR:-/tmp}/h100_check_tests}"
PY="${PYTHON:-python3}"
mkdir -p "${OUT}"
case "$(uname -s)" in Darwin) LIB="${OUT}/libmocknv.dylib" ;; *) LIB="${OUT}/libmocknv.so" ;; esac
cc -shared -fPIC -O1 -o "${LIB}" "$HERE/mocknv.c" || exit 3

ALLOWED='^(nvidia-smi (-q -x|topo -m|nvlink -s|nvlink -e)|dmidecode -t 17|lsblk .*|lspci -D -mm -nn|systemctl show nvidia-fabricmanager .*|dpkg-query (-S|-W) .*|dpkg --verify .*|dmesg ?|ofed_info -s|systemd-detect-virt --vm)$'
pass=0
fail=0

check() {   # check <场景> <期望退出码> <报告中必须出现的文本>...   （额外参数放在环境变量 ARGS 里）
    local sc="$1" want="$2" dir="${OUT}/$1${ARGS:+-x}" ok=1 rc pat
    shift 2
    "$PY" "$HERE/make_fixture.py" "${sc}" "${dir}" || { echo "FAIL ${sc}：夹具生成失败"; fail=$((fail + 1)); return; }
    PATH="${dir}/bin:$PATH" MOCK_NV_CONF="${dir}/nvml.conf" "$PY" "$HERE/../h100_check.py" \
        --sysroot "${dir}/root" --nvml-lib "${LIB}" --cuda --cuda-lib "${LIB}" --assume-root --no-color ${ARGS:-} \
        >"${dir}/report.txt" 2>"${dir}/stderr.txt"
    rc=$?
    [ "${rc}" = "${want}" ] || { echo "  [${sc}] 退出码 ${rc}，期望 ${want}"; ok=0; }
    for pat in "$@"; do
        grep -qF -- "${pat}" "${dir}/report.txt" || { echo "  [${sc}] 报告中缺少：${pat}"; ok=0; }
    done
    if grep -vE "$ALLOWED" "${dir}/data/calls.log" | sed 's/^/    非白名单调用: /' | grep .; then ok=0; fi
    if [ -s "${dir}/stderr.txt" ]; then echo "  [${sc}] stderr 非空："; sed 's/^/    /' "${dir}/stderr.txt"; ok=0; fi
    if [ $ok = 1 ]; then
        echo "PASS ${sc} ${ARGS:-}(rc=${rc})"
        pass=$((pass + 1))
    else
        echo "FAIL ${sc}（报告：${dir}/report.txt）"
        fail=$((fail + 1))
    fi
}

check h100 0 '总体结论：[通过]' '8/8 张 GPU 确认为 H100 SXM5 80GB' '真 H100 SXM5' 'SW0:5 SW1:4 SW2:4 SW3:5' \
    'MCX75310AAS-NEAT' 'mlx5_0(PIX)' 'M321R8GA0BB0-CQKZJ' 'NVML 直连交叉验证'
check h20-fake 2 '实为 H20 96GB，不是 H100' 'nvidia-smi 报告“NVIDIA H100 80GB HBM3”' '文件在安装后被修改' \
    'nvidia-smi 上报设备 ID 0x2330' '总体结论：[不通过]'
check h20 2 '实为 H20 96GB，不是 H100' '非H100: H20' '总体结论：[不通过]'
check h800 2 '实为 H800 SXM5 80GB，不是 H100' '非H100: H800 SXM5' '| 8/8 ' 'NV8'
check degraded 1 '链路 17 未激活' 'mlx5_4/1 端口状态 DOWN' 'nvidia-fabricmanager 未运行' 'Xid 79(GPU 掉卡' \
    '待生效的行重映射' 'PCIe 降级' '硬件降频' '不可纠正 ECC' '[通过，有告警]'
check nodriver 1 '待确认' '只完成了硬件 ID 核验' '未绑定任何驱动' '未找到 nvidia-smi'
check runfile 1 '多半是用 .run 安装包覆盖' '8 张 GPU 确认为真 H100 SXM5 80GB' '[通过，有告警]'
check h100pcie 0 '真 H100 PCIe' '8 张 GPU 确认为真 H100 PCIe 80GB' '本机 GPU 无 NVLink'
ARGS='--expect h100-sxm' check h100pcie 2 '实为 H100 PCIe 80GB，与期望的 H100 SXM5 版不符' '形态不符: H100 PCIe'
ARGS='--expect any' check h20 0 '8 张 GPU 确认为真 H20 96GB'

# ---- run.sh 一键入口：模拟 curl ... | bash（下载地址用 file://，复用上面生成的 h100 场景）
expect_rc() {   # expect_rc <名称> <期望退出码> <实际退出码> <输出文件> <必须出现的文本>
    if [ "$3" = "$2" ] && grep -qF -- "$5" "$4"; then
        echo "PASS $1 (rc=$3)"; pass=$((pass + 1))
    else
        echo "FAIL $1：退出码 $3（期望 $2），或 $4 中缺少：$5"; fail=$((fail + 1))
    fi
}
H="${OUT}/h100"
PATH="${H}/bin:$PATH" MOCK_NV_CONF="${H}/nvml.conf" H100_CHECK_URL="file://${HERE}/../h100_check.py" \
    bash -s -- --sysroot "${H}/root" --nvml-lib "${LIB}" --assume-root --no-color \
    <"${HERE}/../run.sh" >"${OUT}/runsh.txt" 2>"${OUT}/runsh.err"
expect_rc 'run.sh 经管道执行' 0 $? "${OUT}/runsh.txt" '总体结论：[通过]'
head -c 60000 "${HERE}/../h100_check.py" >"${OUT}/truncated.py"
H100_CHECK_URL="file://${OUT}/truncated.py" bash -s <"${HERE}/../run.sh" >/dev/null 2>"${OUT}/trunc.err"
expect_rc 'run.sh 拒绝不完整下载' 3 $? "${OUT}/trunc.err" '内容不完整'
bash <"${HERE}/../h100_check.py" >/dev/null 2>"${OUT}/guard.err"
expect_rc '误把 .py 管道给 bash' 3 $? "${OUT}/guard.err" 'run.sh | sudo bash'

echo "合计：通过 ${pass}，失败 ${fail}（输出目录 ${OUT}）"
[ ${fail} = 0 ]
