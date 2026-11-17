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

check() {   # check <场景> <期望退出码> <报告中必须出现的文本>...（以 ! 开头表示不得出现；额外参数放在环境变量 ARGS 里）
    local sc="$1" want="$2" dir="${OUT}/$1${ARGS:+-$(printf '%s' "${ARGS}" | tr -c 'a-zA-Z0-9' '_')}" ok=1 rc pat
    shift 2
    "$PY" "$HERE/make_fixture.py" "${sc}" "${dir}" || { echo "FAIL ${sc}：夹具生成失败"; fail=$((fail + 1)); return; }
    PATH="${dir}/bin:$PATH" MOCK_NV_CONF="${dir}/nvml.conf" "$PY" "$HERE/../h100_check.py" \
        --sysroot "${dir}/root" --nvml-lib "${LIB}" --cuda --cuda-lib "${LIB}" --assume-root --no-color ${ARGS:-} \
        >"${dir}/report.txt" 2>"${dir}/stderr.txt"
    rc=$?
    [ "${rc}" = "${want}" ] || { echo "  [${sc}] 退出码 ${rc}，期望 ${want}"; ok=0; }
    for pat in "$@"; do
        case "${pat}" in
            !*) ! grep -qF -- "${pat#!}" "${dir}/report.txt" || { echo "  [${sc}] 报告中不应出现：${pat#!}"; ok=0; } ;;
            *) grep -qF -- "${pat}" "${dir}/report.txt" || { echo "  [${sc}] 报告中缺少：${pat}"; ok=0; } ;;
        esac
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
    'MCX75310AAS-NEAT' 'mlx5_0(PIX)' 'M321R8GA0BB0-CQKZJ' 'NVML 直连交叉验证' \
    '核验目标: H100 SXM5 / NVLink 版（--expect h100-sxm，默认值）' '四、H100 SXM5 真伪核验'
ARGS='--expect h100-nvlink' check h100 0 '核验目标: H100 SXM5 / NVLink 版（--expect h100-nvlink）'
check h20-fake 2 '实为 H20 96GB，不是 H100' 'nvidia-smi 报告“NVIDIA H100 80GB HBM3”' '文件在安装后被修改' \
    'nvidia-smi 上报设备 ID 0x2330' '总体结论：[不通过]'
check h20 2 '实为 H20 96GB，不是 H100' '非H100: H20' '总体结论：[不通过]'
check h800 2 '实为 H800 SXM5 80GB，不是 H100' '非H100: H800 SXM5' '| 8/8 ' 'NV8'
check degraded 1 '链路 17 未激活' 'mlx5_4/1 端口状态 DOWN' 'nvidia-fabricmanager 未运行' 'Xid 79(GPU 掉卡' \
    '待生效的行重映射' 'PCIe 降级' '硬件降频' '不可纠正 ECC' '[通过，有告警]'
check nodriver 1 '待确认' '只完成了硬件 ID 核验' '未绑定任何驱动' '未找到 nvidia-smi'
check runfile 1 '多半是用 .run 安装包覆盖' '8 张 GPU 确认为真 H100 SXM5 80GB' '[通过，有告警]'
# 默认期望 SXM5（NVLink 版）：PCIe 版判形态不符，并提示改用 --expect h100-pcie
check h100pcie 2 '实为 H100 PCIe 80GB，与期望的 H100 SXM5 版不符' '形态不符: H100 PCIe' \
    '总体结论：[不通过] 8 张 GPU 的形态与期望不符（形态不符: H100 PCIe 8 张）；如果采购的就是 PCIe 版，请加 --expect h100-pcie'
ARGS='--expect h100' check h100pcie 0 '真 H100 PCIe' '8 张 GPU 确认为真 H100 PCIe 80GB' '本机 GPU 无 NVLink'
ARGS='--expect h100-pcie' check h100pcie 0 '核验目标: H100 PCIe 版（--expect h100-pcie）'
ARGS='--expect any' check h20 0 '8 张 GPU 确认为真 H20 96GB'
# 消费级显卡：默认按 H100 核验 → 不通过；--expect any / 4090 → 通过，且不报 GeForce 无关的 ECC、peermem 提示
check rtx4090 2 '实为 RTX 4090，不是 H100' '非H100: RTX 4090' '四、H100 SXM5 真伪核验'
ARGS='--expect any' check rtx4090 0 '8/8 张 GPU 确认为 RTX 4090' '真 RTX 4090' '| 128 ' '| 0%' '四、GPU 型号与真伪核验' \
    '!ECC 已关闭' '!nvidia_peermem' '!0.0%' '!待确认'
ARGS='--expect 4090' check rtx4090 0 '四、RTX 4090 真伪核验' '核验目标: RTX 4090'
ARGS='--expect 4090' check rtx4090d 2 '实为 RTX 4090 D，不是 RTX 4090' '非RTX 4090: RTX 4090 D' '| 114 '
ARGS='--expect 4090' check rtx4090d-fake 2 '实为 RTX 4090 D，不是 RTX 4090' 'nvidia-smi 报告“NVIDIA GeForce RTX 4090”' \
    'nvidia-smi 上报设备 ID 0x2684' '文件在安装后被修改'
ARGS='--expect 4090' check rtx4090-48g 1 '疑似改装大显存' '真 RTX 4090(改装48G显存)' '[通过，有告警]'
ARGS='--expect any' check pciids 0 '按本机 pci.ids 识别' '真 GeForce RTX 4090'
# RTX PRO 6000D：驱动名“NVIDIA RTX 6000D”要认作 RTX PRO 6000D；冒充 RTX PRO 6000 时判不通过
ARGS='--expect pro6000d' check rtx6000d 0 '总体结论：[通过]' '真 RTX PRO 6000D Blackwell Server' '| 156 ' '| 448 ' '| 一致 '
ARGS='--expect 6000d' check rtx6000d 0 '核验目标: RTX PRO 6000D（--expect 6000d）'
ARGS='--expect pro6000' check rtx6000d 2 '实为 RTX PRO 6000D Blackwell Server，不是 RTX PRO 6000 —— 面向中国市场的特供版' \
    '非RTX PRO 6000: RTX PRO 6000D Blackwell Server'
check rtx6000d 2 '非H100: RTX PRO 6000D Blackwell Server'
# 型号库和 pci.ids 都不认识的设备 ID：参考驱动名称，名称是别的型号就判不通过，名称相符只能判待确认
check unknownid 2 '设备 ID 0x2bfe 不在内置型号库中' '驱动报告的名称为“NVIDIA RTX 6000D”，不是 H100' '非H100: RTX 6000D'
ARGS='--expect 6000d' check unknownid 1 '与期望的 RTX PRO 6000D 一致，但无法用硬件 ID 独立核验' '总体结论：[待确认]'
# 矿卡：CMP 170HX 是 GA100 核心，冒充 A100 时判不通过
ARGS='--expect a100' check cmp170hx 2 '实为 CMP 170HX，不是 A100 —— 挖矿专用卡' '非A100: CMP 170HX'
ARGS='--expect any' check cmp170hx 0 '真 CMP 170HX' '| 70 '

# ---- 型号名称识别规则与型号库自洽性（不需要夹具）
if "$PY" "$HERE/test_keys.py" >"${OUT}/keys.txt" 2>&1; then
    echo "PASS 型号名称规则（$(tail -1 "${OUT}/keys.txt")）"; pass=$((pass + 1))
else
    echo "FAIL 型号名称规则："; sed 's/^/    /' "${OUT}/keys.txt"; fail=$((fail + 1))
fi

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
