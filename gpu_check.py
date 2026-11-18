#!/bin/sh
''''[ -f "$0" ] || { echo "gpu_check: 请用 curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash" >&2; exit 3; }  # '''
''''command -v python3 >/dev/null 2>&1 && exec python3 "$0" "$@"                        # '''
''''[ -x /usr/libexec/platform-python ] && exec /usr/libexec/platform-python "$0" "$@"    # '''
''''echo "gpu_check: 需要 Python 3.6+（未找到 python3 或 platform-python）" >&2; exit 3 # '''
#
# gpu_check.py —— GPU 服务器只读检测与真伪核验（项目原名 h100-check，v1.3.0 及以前叫 h100_check.py）
#   硬件清单 + IB 网卡 + NVLink 拓扑 + GPU 型号真伪核验：按芯片熔丝决定的设备 ID 识别型号，再与驱动、NVML、nvidia-smi 交叉比对，
#   能识别 H20/H800 冒充 H100、4090 D 冒充 4090、RTX PRO 6000D 冒充 6000、矿卡冒充计算卡、nvidia-smi 被篡改等情况
#   型号库覆盖 Pascal 到 Blackwell：数据中心卡（H/B/A/L/V/T/P 系列）、专业卡（RTX PRO / Ada / A 系列 / Quadro）、
#   GeForce（GTX 10 系 ~ RTX 50 系）和矿卡
#
# 对线上业务零干扰：
#   * 只读：仅读取 sysfs/procfs，调用 nvidia-smi / NVML 的查询接口，以及 dmidecode、lsblk 等只读命令；
#   * 不创建 CUDA 上下文、不分配显存、不运行任何 kernel / 压测 / 带宽测试，不重置任何计数器；
#   * 不修改任何配置（时钟、功耗墙、ECC、MIG、持久模式、Fabric Manager 等一律不碰）；
#   * 自身以 nice 19 + ionice idle 运行，所有外部命令都有超时，默认不写任何文件。
#
# 用法：sudo ./gpu_check.py [-o report.txt] [--expect h100-sxm|h100|a100|4090|pro6000d|any ...] [--expect-gpus 8] [--cuda]
#       默认 --expect h100-sxm：期望 H100 SXM5（NVLink 版），PCIe 版、NVL 版判“形态不符”；其它机器用 --expect 指定型号
#       非 root 也能运行，但内存条、整机序列号、网卡料号、内核日志等需要 root 才能读取。
#
# 退出码：0=全部通过  1=有告警（健康/配置类）  2=真伪不通过（不是期望的型号，或疑似篡改）  3=运行错误
#
import argparse
import ctypes
import hashlib
import io
import os
import platform
import re
import shlex
import socket
import struct
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter

VERSION = '1.4.1'
DEFAULT_EXPECT = 'h100-sxm'   # 默认核验 H100 SXM5，即国内常说的“NVLink 版”（HGX 机型）

OK, INFO, WARN, FAIL = 0, 1, 2, 3
LEVEL_TEXT = {OK: '通过', INFO: '提示', WARN: '警告', FAIL: '不通过'}
CATEGORIES = ['真伪', '驱动软件', 'NVLink', 'IB/RDMA', 'PCIe', 'GPU 健康', '系统']


class G(object):
    """全局运行参数"""
    sysroot = ''        # 仅用于离线/测试：从 <sysroot>/sys、<sysroot>/proc 读取
    test_mode = False
    is_root = False
    color = False


# ============================================================================
# 型号库：PCI 设备 ID 由 GPU 芯片熔丝决定，改名、改标签、改 nvidia-smi 都改不了。
#   family=型号名（与 nvidia-smi / NVML / 驱动报告的名称比对）  keys=同一设备 ID 的多个合法型号名
#   tier=dc 数据中心卡 / ws 专业卡 / gf 消费级（GeForce、TITAN）
#   sms=SM 数  bus=显存位宽(bit)  mem=显存 GB（可列多个合法值）  maxw=最大功耗墙(W)  maxsm/maxmem=最大频率(MHz)
#   links=NVLink 链路数  strict=参考值可信度高，SM/位宽/显存偏差直接判“不通过”（其余型号只告警）
# 型号库里没有的设备 ID，退回用本机 pci.ids（lspci）识别，再与驱动报告的名称交叉核对。
# 来源：设备 ID 与名称取自 NVIDIA 驱动 README 的支持列表（supportedchips.html）和 pci.ids；规格取自 NVIDIA 数据表，
# 并与 TechPowerUp（dbgpu）、维基百科交叉核对，有两个来源一致才写入，拿不准的留空（只识别型号、不比规格）。
# 复核：python3 -I tools/verify_db.py --dbgpu
# ============================================================================

def _chip_arch(chip):
    """芯片代号 → (架构, 计算能力)"""
    c = (chip or '').upper()
    for pre, arch, cc in (('GP100', 'Pascal', '6.0'), ('GP', 'Pascal', '6.1'), ('GV', 'Volta', '7.0'),
                          ('TU', 'Turing', '7.5'), ('GA100', 'Ampere', '8.0'), ('GA', 'Ampere', '8.6'),
                          ('AD', 'Ada Lovelace', '8.9'), ('GH', 'Hopper', '9.0'), ('GB1', 'Blackwell', '10.0'),
                          ('GB2', 'Blackwell', '12.0')):
        if c.startswith(pre):
            return arch, cc
    return None, None


def _sku(sku, family, form, mem, arch='Hopper', **kw):
    hopper = arch == 'Hopper'
    d = dict(sku=sku, family=family, form=form, mem=mem, arch=arch, tier='dc', chip='GH100' if hopper else None,
             cc='9.0' if hopper else None, sms=None, bus=None, maxw=None, maxsm=None, maxmem=None, links=None,
             strict=False, note='', keys=None, src='db')
    d.update(kw)
    return d


_H20 = '面向中国市场的特供版，算力大幅削减（BF16 算力约为 H100 SXM 的 15%）'
_H800 = '面向中国市场的特供版，NVLink 带宽削减到 400GB/s（H100 SXM 为 900GB/s）'
_ES = '没有正式型号名，可能是工程样品（ES/QS），不是正式量产的 H100'
_A800 = '面向中国市场的特供版，NVLink 带宽削减到 400GB/s（A100 SXM 为 600GB/s）'
_L20 = '面向中国市场的特供版，由 L40S 削减而来（SM 数约为 L40S 的 2/3）'
_CN = '面向中国市场的特供版'
_4090D = '面向中国市场的特供版（114 个 SM，RTX 4090 为 128 个，算力低约 5%~10%）'
_5090D = '面向中国市场的特供版（AI 算力受驱动/固件限制）'
_5090DV2 = '面向中国市场的特供版（24GB 显存、384-bit 位宽；RTX 5090 为 32GB、512-bit）'
_6000D = '面向中国市场的特供版（156 个 SM、448-bit、84GB 显存；RTX PRO 6000 为 188 个 SM、512-bit、96GB）'
_CMP = '挖矿专用卡（CMP 系列），没有显示输出，计算性能和 PCIe 带宽受限'
_MINER = '挖矿专用卡（GTX 10 系核心），没有显示输出，PCIe 带宽受限'

GPU_DB = {
    # ---- H100 ----
    0x2330: _sku('H100 SXM5 80GB', 'H100', 'SXM5', 80, sms=132, bus=5120, maxw=700,
                 maxsm=1980, maxmem=2619, links=18, strict=True),
    0x2331: _sku('H100 PCIe 80GB', 'H100', 'PCIe', 80, sms=114, bus=5120, maxw=350,
                 maxsm=1755, maxmem=1593, strict=True),
    0x2321: _sku('H100 NVL 94GB', 'H100', 'NVL', 94, sms=132, maxw=400),
    0x2339: _sku('H100 SXM5 94GB', 'H100', 'SXM5', 94, links=18),
    0x2338: _sku('H100 SXM5 96GB', 'H100', 'SXM5', 96, links=18),
    0x2337: _sku('H100 SXM5 64GB', 'H100', 'SXM5', 64, links=18),
    0x233d: _sku('H100 96GB', 'H100', '?', 96),
    0x2336: _sku('H100 (GH100 变体)', 'H100', '?', None),
    0x2313: _sku('H100 CNX', 'H100', 'PCIe', 80),
    # ---- H800 / H20：特供版，不是 H100 ----
    0x2324: _sku('H800 SXM5 80GB', 'H800', 'SXM5', 80, sms=132, bus=5120, maxw=700, links=8, note=_H800),
    0x2322: _sku('H800 PCIe 80GB', 'H800', 'PCIe', 80, sms=114, bus=5120, note=_H800),
    0x233a: _sku('H800 NVL 94GB', 'H800', 'NVL', 94, note=_H800),
    0x2329: _sku('H20 96GB', 'H20', 'SXM5', 96, note=_H20),
    0x2328: _sku('H20B', 'H20', 'SXM5', None, note=_H20),
    0x232c: _sku('H20-3e 141GB', 'H20', 'SXM5', 141, note=_H20),
    0x230c: _sku('H20 NVL16', 'H20', 'NVL16', None, note=_H20),
    0x230e: _sku('H20 NVL16', 'H20', 'NVL16', None, note=_H20),
    # ---- H200 / GH200 ----
    0x2335: _sku('H200 SXM 141GB', 'H200', 'SXM5', 141, sms=132, bus=6144, maxw=700, links=18),
    0x233b: _sku('H200 NVL 141GB', 'H200', 'NVL', 141, sms=132, bus=6144),
    0x2342: _sku('GH200 Grace Hopper', 'GH200', 'Superchip', None),
    0x2348: _sku('GH200 144GB HBM3e', 'GH200', 'Superchip', 144),
    # ---- 未命名 GH100 ----
    0x2302: _sku('GH100 未命名 ID', 'GH100?', '?', None, note=_ES),
    0x2343: _sku('GH100 未命名 ID', 'GH100?', '?', None, note=_ES),
    0x2345: _sku('GH100-88K-A1', 'GH100?', '?', None, note=_ES),
    0x237e: _sku('GH100 未命名 ID', 'GH100?', '?', None, note=_ES),
}


def _add(ids, sku, family, chip, tier, mem, form='PCIe', **kw):
    arch, cc = _chip_arch(chip)
    for i in ((ids,) if isinstance(ids, int) else ids):
        GPU_DB[i] = _sku(sku, family, form, mem, arch=arch, cc=cc, chip=chip, tier=tier, **kw)


# ---- Blackwell 数据中心 ----
_add((0x2901, 0x290a), 'B200', 'B200', 'GB100', 'dc', None, form='SXM6')
_add(0x2909, 'B200 168GB', 'B200', 'GB100', 'dc', None, form='SXM6')
_add((0x2920, 0x29bc), 'B100', 'B100', 'GB100', 'dc', None, form='SXM6')
_add(0x2941, 'GB200', 'GB200', 'GB100', 'dc', None, form='Superchip')
_add(0x3182, 'B300 SXM6', 'B300', 'GB110', 'dc', None, form='SXM6')
_add((0x31c2, 0x31c3), 'GB300', 'GB300', 'GB110', 'dc', None, form='Superchip')
_add(0x31a1, 'GB300 Max-Q', 'GB300', 'GB110', 'dc', None, form='?')
# ---- Ampere 数据中心 ----
_add(0x20b0, 'A100 SXM4 40GB', 'A100', 'GA100', 'dc', 40, form='SXM4', sms=108, bus=5120, maxw=400, links=12)
_add(0x20b2, 'A100 SXM4 80GB', 'A100', 'GA100', 'dc', 80, form='SXM4', sms=108, bus=5120, maxw=(400, 500), links=12)
_add(0x20b3, 'A100 SXM 64GB', 'A100', 'GA100', 'dc', 64, form='SXM4', links=12)
_add((0x20b1, 0x20f1), 'A100 PCIe 40GB', 'A100', 'GA100', 'dc', 40, sms=108, bus=5120, maxw=250)
_add(0x20b5, 'A100 PCIe 80GB', 'A100', 'GA100', 'dc', 80, sms=108, bus=5120, maxw=300)
_add(0x20b7, 'A30 24GB', 'A30', 'GA100', 'dc', 24, sms=56, bus=3072, maxw=165)
_add(0x20bd, 'A800 SXM4 40GB', 'A800', 'GA100', 'dc', 40, form='SXM4', sms=108, bus=5120, links=8, note=_A800)
_add(0x20f3, 'A800 SXM4 80GB', 'A800', 'GA100', 'dc', 80, form='SXM4', sms=108, bus=5120, links=8, note=_A800)
_add(0x20f5, 'A800 PCIe 80GB', 'A800', 'GA100', 'dc', 80, sms=108, bus=5120, note=_A800)
_add(0x20f6, 'A800 40GB Active', 'A800', 'GA100', 'dc', 40, sms=108, bus=5120, note=_A800)
_add(0x2235, 'A40 48GB', 'A40', 'GA102', 'dc', 48, sms=84, bus=384, maxw=300)
_add(0x2236, 'A10 24GB', 'A10', 'GA102', 'dc', 24, sms=72, bus=384, maxw=150)
# A10G（AWS G5）：NVIDIA/AWS 数据表写 80 个 RT 核心（即 80 个 SM）、300W；TechPowerUp 写成了 A10 的 72 个 SM
_add(0x2237, 'A10G 24GB', 'A10G', 'GA102', 'dc', 24, sms=80, bus=384, maxw=300)
_add(0x2238, 'A10M', 'A10M', 'GA102', 'dc', None)
_add(0x25b6, 'A2 / A16 16GB', 'A2', 'GA107', 'dc', 16, sms=10, bus=128, keys=('A2', 'A16'))
# ---- Ada 数据中心 ----
_add(0x26b5, 'L40 48GB', 'L40', 'AD102', 'dc', 48, sms=142, bus=384, maxw=300)
_add(0x26b9, 'L40S 48GB', 'L40S', 'AD102', 'dc', 48, sms=142, bus=384, maxw=350)
_add((0x26ba, 0x26b7), 'L20 48GB', 'L20', 'AD102', 'dc', 48, sms=92, bus=384, note=_L20)
_add(0x27b8, 'L4 24GB', 'L4', 'AD104', 'dc', 24, sms=58, bus=192, maxw=72)    # 7424 个 CUDA 核心 = 58 个 SM
_add(0x27b6, 'L2', 'L2', 'AD104', 'dc', None, note=_CN)
# ---- Turing / Volta / Pascal 数据中心 ----
_add(0x1eb8, 'T4 16GB', 'T4', 'TU104', 'dc', 16, sms=40, bus=256, maxw=70)
_add((0x1db1, 0x1df5), 'V100 SXM2 16GB', 'V100', 'GV100', 'dc', 16, form='SXM2', sms=80, bus=4096, maxw=300, links=6)
_add(0x1db5, 'V100 SXM2 32GB', 'V100', 'GV100', 'dc', 32, form='SXM2', sms=80, bus=4096, maxw=300, links=6)
_add(0x1db8, 'V100 SXM3 32GB', 'V100', 'GV100', 'dc', 32, form='SXM3', sms=80, bus=4096, links=6)
_add(0x1db4, 'V100 PCIe 16GB', 'V100', 'GV100', 'dc', 16, sms=80, bus=4096, maxw=250)
_add(0x1db6, 'V100 PCIe 32GB', 'V100', 'GV100', 'dc', 32, sms=80, bus=4096, maxw=250)
_add(0x1db3, 'V100 FHHL 16GB', 'V100', 'GV100', 'dc', 16, sms=80, bus=4096)
_add((0x1db2, 0x1db7), 'V100 DGXS', 'V100', 'GV100', 'dc', (16, 32), sms=80, bus=4096)
_add(0x1df6, 'V100S PCIe 32GB', 'V100S', 'GV100', 'dc', 32, sms=80, bus=4096, maxw=250)
_add((0x1df0, 0x1df2), 'V100 PG50x-216', 'V100', 'GV100', 'dc', None, form='?', sms=80, bus=4096)   # 驱动名 Tesla PG500/PG503-216
_add(0x15f7, 'P100 PCIe 12GB', 'P100', 'GP100', 'dc', 12, sms=56, bus=3072, maxw=250)
_add(0x15f8, 'P100 PCIe 16GB', 'P100', 'GP100', 'dc', 16, sms=56, bus=4096, maxw=250)
_add(0x15f9, 'P100 SXM2 16GB', 'P100', 'GP100', 'dc', 16, form='SXM2', sms=56, bus=4096, maxw=300, links=4)
_add(0x1b38, 'P40 24GB', 'P40', 'GP102', 'dc', 24, sms=30, bus=384, maxw=250)
_add(0x1bb3, 'P4 8GB', 'P4', 'GP104', 'dc', 8, sms=20, bus=256, maxw=75)
# ---- 专业卡（RTX / Quadro）----
_add(0x2bb1, 'RTX PRO 6000 Blackwell', 'RTX PRO 6000', 'GB202', 'ws', 96, sms=188, bus=512)
_add(0x2bb4, 'RTX PRO 6000 Blackwell Max-Q', 'RTX PRO 6000', 'GB202', 'ws', 96, sms=188, bus=512)
_add(0x2bb5, 'RTX PRO 6000 Blackwell Server', 'RTX PRO 6000', 'GB202', 'dc', 96, sms=188, bus=512)
# RTX PRO 6000D：NVIDIA 驱动里的名称是“NVIDIA RTX 6000D”（没有 PRO）。0x2bb9 是实际出货的服务器版，
# 规格取自 8 张实卡；0x2bb2、0x2bbc 只见于 pci.ids，驱动未收录，规格不明
_add(0x2bb9, 'RTX PRO 6000D Blackwell Server', 'RTX PRO 6000D', 'GB202', 'dc', 84, sms=156, bus=448, note=_6000D)
_add(0x2bb2, 'RTX PRO 6000D Blackwell', 'RTX PRO 6000D', 'GB202', 'ws', None, note=_6000D)
_add(0x2bbc, 'RTX PRO 6000D Blackwell Max-Q', 'RTX PRO 6000D', 'GB202', 'ws', None, note=_6000D)
_add(0x2bbd, 'RTX PRO 5500 Blackwell', 'RTX PRO 5500', 'GB202', 'ws', 84)
_add(0x2bb3, 'RTX PRO 5000 Blackwell', 'RTX PRO 5000', 'GB202', 'ws', (48, 72), sms=110, bus=384)
_add(0x2c31, 'RTX PRO 4500 Blackwell', 'RTX PRO 4500', 'GB203', 'ws', 32, sms=82, bus=256)
_add(0x2c3a, 'RTX PRO 4500 Blackwell Server', 'RTX PRO 4500', 'GB203', 'dc', 32, sms=82, bus=256)
_add((0x2c34, 0x2c33), 'RTX PRO 4000 Blackwell', 'RTX PRO 4000', 'GB203', 'ws', 24, sms=70, bus=192)
_add(0x2d30, 'RTX PRO 2000 Blackwell', 'RTX PRO 2000', 'GB206', 'ws', 16, sms=34, bus=128)
_add(0x26b1, 'RTX 6000 Ada 48GB', 'RTX 6000 Ada', 'AD102', 'ws', 48, sms=142, bus=384, maxw=300)
_add(0x26b3, 'RTX 5880 Ada 48GB', 'RTX 5880 Ada', 'AD102', 'ws', 48, sms=110, bus=384)
_add(0x26b2, 'RTX 5000 Ada 32GB', 'RTX 5000 Ada', 'AD102', 'ws', 32, sms=100, bus=256, maxw=250)
_add(0x27b1, 'RTX 4500 Ada 24GB', 'RTX 4500 Ada', 'AD104', 'ws', 24, sms=60, bus=192, maxw=210)
_add(0x27b2, 'RTX 4000 Ada 20GB', 'RTX 4000 Ada', 'AD104', 'ws', 20, sms=48, bus=160, maxw=130)
_add(0x27b0, 'RTX 4000 SFF Ada 20GB', 'RTX 4000 Ada', 'AD104', 'ws', 20, sms=48, bus=160, maxw=70)
_add(0x28b0, 'RTX 2000 Ada 16GB', 'RTX 2000 Ada', 'AD107', 'ws', 16, sms=22, bus=128)
_add(0x2230, 'RTX A6000 48GB', 'RTX A6000', 'GA102', 'ws', 48, sms=84, bus=384, maxw=300)
_add(0x2233, 'RTX A5500 24GB', 'RTX A5500', 'GA102', 'ws', 24, sms=80, bus=384, maxw=230)
_add(0x2231, 'RTX A5000 24GB', 'RTX A5000', 'GA102', 'ws', 24, sms=64, bus=384, maxw=230)
_add(0x2232, 'RTX A4500 20GB', 'RTX A4500', 'GA102', 'ws', 20, sms=56, bus=320, maxw=200)
_add((0x24b0, 0x24b1), 'RTX A4000 16GB', 'RTX A4000', 'GA104', 'ws', 16, sms=48, bus=256)
_add(0x2531, 'RTX A2000 6GB', 'RTX A2000', 'GA106', 'ws', 6, sms=26, bus=192)
_add(0x2571, 'RTX A2000 12GB', 'RTX A2000', 'GA106', 'ws', 12, sms=26, bus=192)
_add((0x1e30, 0x1e78), 'Quadro RTX 6000/8000', 'RTX 6000', 'TU102', 'ws', (24, 48), sms=72, bus=384,
     keys=('RTX 6000', 'RTX 8000'))
_add(0x1e36, 'Quadro RTX 6000', 'RTX 6000', 'TU102', 'ws', 24, sms=72, bus=384)
_add(0x1eb0, 'Quadro RTX 5000', 'RTX 5000', 'TU104', 'ws', 16, sms=48, bus=256)
_add(0x1eb1, 'Quadro RTX 4000', 'RTX 4000', 'TU104', 'ws', 8, sms=36, bus=256)
_add(0x25b0, 'RTX A1000 8GB', 'RTX A1000', 'GA107', 'ws', 8, sms=18, bus=128)
_add(0x25b2, 'RTX A400 4GB', 'RTX A400', 'GA107', 'ws', 4, sms=6, bus=64)
_add(0x1fb0, 'T1000 4GB', 'T1000', 'TU117', 'ws', 4, sms=14, bus=128)       # 驱动名也可能是“NVIDIA DGX Display”
_add(0x1ff0, 'T1000 8GB', 'T1000', 'TU117', 'ws', 8, sms=14, bus=128)
_add(0x1fb1, 'T600 4GB', 'T600', 'TU117', 'ws', 4, sms=10, bus=128)
_add(0x1fb2, 'T400', 'T400', 'TU117', 'ws', (2, 4), sms=6, bus=64)
_add(0x1ff2, 'T400 4GB', 'T400', 'TU117', 'ws', 4, sms=6, bus=64)
_add(0x1dba, 'Quadro GV100 32GB', 'Quadro GV100', 'GV100', 'ws', 32, sms=80, bus=4096, keys=('Quadro GV100', 'TITAN V'))
_add(0x1b30, 'Quadro P6000 24GB', 'Quadro P6000', 'GP102', 'ws', 24, sms=30, bus=384)
_add(0x1bb0, 'Quadro P5000 16GB', 'Quadro P5000', 'GP104', 'ws', 16, sms=20, bus=256)
_add(0x1bb1, 'Quadro P4000 8GB', 'Quadro P4000', 'GP104', 'ws', 8, sms=14, bus=256)
_add(0x1c31, 'Quadro P2200 5GB', 'Quadro P2200', 'GP106', 'ws', 5, sms=10, bus=160)
_add(0x1c30, 'Quadro P2000 5GB', 'Quadro P2000', 'GP106', 'ws', 5, sms=8, bus=160)
_add(0x1cfa, 'Quadro P2000 (GP107)', 'Quadro P2000', 'GP107', 'ws', None)
_add((0x1cb1, 0x1cfb), 'Quadro P1000 4GB', 'Quadro P1000', 'GP107', 'ws', 4, bus=128)
_add(0x1cb6, 'Quadro P620 2GB', 'Quadro P620', 'GP107', 'ws', 2, sms=4, bus=128)
_add(0x1cb2, 'Quadro P600 2GB', 'Quadro P600', 'GP107', 'ws', 2, sms=3, bus=128)
_add(0x1cb3, 'Quadro P400 2GB', 'Quadro P400', 'GP107', 'ws', 2, sms=2, bus=64)
# ---- 消费级 GeForce RTX 50 / 40 / 30 / 20 与 TITAN（功耗墙和频率随板卡厂商不同，不做比对）----
_add(0x2b85, 'RTX 5090', 'RTX 5090', 'GB202', 'gf', 32, sms=170, bus=512)
_add(0x2b87, 'RTX 5090 D', 'RTX 5090 D', 'GB202', 'gf', 32, sms=170, bus=512, note=_5090D)
_add(0x2b8c, 'RTX 5090 D V2', 'RTX 5090 D V2', 'GB202', 'gf', 24, sms=170, bus=384, note=_5090DV2)
_add(0x2c02, 'RTX 5080', 'RTX 5080', 'GB203', 'gf', 16, sms=84, bus=256)
_add(0x2c05, 'RTX 5070 Ti', 'RTX 5070 Ti', 'GB203', 'gf', 16, sms=70, bus=256)
_add(0x2f02, 'RTX 5070 SUPER', 'RTX 5070 SUPER', 'GB205', 'gf', None)
_add((0x2f04, 0x2c09), 'RTX 5070', 'RTX 5070', 'GB205', 'gf', 12, sms=48, bus=192)
_add(0x2d04, 'RTX 5060 Ti', 'RTX 5060 Ti', 'GB206', 'gf', (8, 16), sms=36, bus=128)
_add((0x2d05, 0x2f06), 'RTX 5060', 'RTX 5060', 'GB206', 'gf', 8, sms=30, bus=128)
_add(0x2d83, 'RTX 5050', 'RTX 5050', 'GB207', 'gf', 8, sms=20, bus=128)
_add(0x2684, 'RTX 4090', 'RTX 4090', 'AD102', 'gf', 24, sms=128, bus=384)
_add(0x2685, 'RTX 4090 D', 'RTX 4090 D', 'AD102', 'gf', 24, sms=114, bus=384, note=_4090D)
_add((0x2702, 0x2703), 'RTX 4080 SUPER', 'RTX 4080 SUPER', 'AD103', 'gf', 16, sms=80, bus=256)
_add((0x2704, 0x2687), 'RTX 4080', 'RTX 4080', 'AD103', 'gf', 16, sms=76, bus=256)
_add((0x2705, 0x2689), 'RTX 4070 Ti SUPER', 'RTX 4070 Ti SUPER', 'AD103', 'gf', 16, sms=66, bus=256)
_add(0x2782, 'RTX 4070 Ti', 'RTX 4070 Ti', 'AD104', 'gf', 12, sms=60, bus=192)
_add(0x2783, 'RTX 4070 SUPER', 'RTX 4070 SUPER', 'AD104', 'gf', 12, sms=56, bus=192)
_add((0x2786, 0x2709), 'RTX 4070', 'RTX 4070', 'AD104', 'gf', 12, sms=46, bus=192)
_add(0x2803, 'RTX 4060 Ti 8GB', 'RTX 4060 Ti', 'AD106', 'gf', 8, sms=34, bus=128)
_add(0x2805, 'RTX 4060 Ti 16GB', 'RTX 4060 Ti', 'AD106', 'gf', 16, sms=34, bus=128)
_add(0x2788, 'RTX 4060 Ti', 'RTX 4060 Ti', 'AD104', 'gf', (8, 16), sms=34, bus=128)
_add((0x2882, 0x2808), 'RTX 4060', 'RTX 4060', 'AD107', 'gf', 8, sms=24, bus=128)
_add(0x2203, 'RTX 3090 Ti', 'RTX 3090 Ti', 'GA102', 'gf', 24, sms=84, bus=384)
_add(0x2204, 'RTX 3090', 'RTX 3090', 'GA102', 'gf', 24, sms=82, bus=384)
_add(0x2208, 'RTX 3080 Ti', 'RTX 3080 Ti', 'GA102', 'gf', 12, sms=80, bus=384)
_add((0x2206, 0x2216), 'RTX 3080 10GB', 'RTX 3080', 'GA102', 'gf', 10, sms=68, bus=320)
_add(0x220a, 'RTX 3080 12GB', 'RTX 3080', 'GA102', 'gf', 12, sms=70, bus=384)
_add((0x2482, 0x2207), 'RTX 3070 Ti', 'RTX 3070 Ti', 'GA104', 'gf', 8, sms=48, bus=256)
_add((0x2484, 0x2488, 0x248d), 'RTX 3070', 'RTX 3070', 'GA104', 'gf', 8, sms=46, bus=256)
_add((0x2486, 0x2489, 0x248e, 0x24c9, 0x2414), 'RTX 3060 Ti', 'RTX 3060 Ti', 'GA104', 'gf', 8, sms=38, bus=256)
_add((0x2503, 0x2504, 0x2509, 0x2544, 0x2487), 'RTX 3060 12GB', 'RTX 3060', 'GA106', 'gf', 12, sms=28, bus=192)
_add(0x24c7, 'RTX 3060 8GB', 'RTX 3060', 'GA104', 'gf', 8, sms=28, bus=128)
_add(0x2507, 'RTX 3050 8GB', 'RTX 3050', 'GA106', 'gf', 8, sms=20, bus=128)
_add(0x2582, 'RTX 3050 8GB', 'RTX 3050', 'GA107', 'gf', 8, sms=20, bus=128)
_add(0x2508, 'RTX 3050 OEM 8GB', 'RTX 3050', 'GA106', 'gf', 8, sms=18, bus=128)
_add(0x2584, 'RTX 3050 6GB', 'RTX 3050', 'GA107', 'gf', 6, sms=18, bus=96)
_add((0x1e04, 0x1e07), 'RTX 2080 Ti', 'RTX 2080 Ti', 'TU102', 'gf', 11, sms=68, bus=352)
_add(0x1e81, 'RTX 2080 SUPER', 'RTX 2080 SUPER', 'TU104', 'gf', 8, sms=48, bus=256)
_add((0x1e82, 0x1e87), 'RTX 2080', 'RTX 2080', 'TU104', 'gf', 8, sms=46, bus=256)
_add((0x1e84, 0x1ec2, 0x1ec7), 'RTX 2070 SUPER', 'RTX 2070 SUPER', 'TU104', 'gf', 8, sms=40, bus=256)
_add((0x1f02, 0x1f07), 'RTX 2070', 'RTX 2070', 'TU106', 'gf', 8, sms=36, bus=256)
_add((0x1f06, 0x1f42, 0x1f47), 'RTX 2060 SUPER', 'RTX 2060 SUPER', 'TU106', 'gf', 8, sms=34, bus=256)
_add((0x1f08, 0x1e89), 'RTX 2060', 'RTX 2060', 'TU106', 'gf', 6, sms=30, bus=192)
_add(0x1f03, 'RTX 2060 12GB', 'RTX 2060', 'TU106', 'gf', 12, sms=34, bus=192)
_add(0x1e02, 'TITAN RTX', 'TITAN RTX', 'TU102', 'gf', 24, sms=72, bus=384)
_add(0x1d81, 'TITAN V', 'TITAN V', 'GV100', 'gf', 12, sms=80, bus=3072)
_add(0x2182, 'GTX 1660 Ti', 'GTX 1660 Ti', 'TU116', 'gf', 6, sms=24, bus=192)
_add(0x21c4, 'GTX 1660 SUPER', 'GTX 1660 SUPER', 'TU116', 'gf', 6, sms=22, bus=192)
_add(0x2184, 'GTX 1660', 'GTX 1660', 'TU116', 'gf', 6, sms=22, bus=192)
_add(0x2187, 'GTX 1650 SUPER', 'GTX 1650 SUPER', 'TU116', 'gf', 4, sms=20, bus=128)
_add((0x1f82, 0x1f0a, 0x2188), 'GTX 1650', 'GTX 1650', 'TU117', 'gf', 4, sms=14, bus=128)
_add(0x1f83, 'GTX 1630', 'GTX 1630', 'TU117', 'gf', 4, sms=8, bus=64)
_add(0x1b02, 'TITAN Xp', 'TITAN Xp', 'GP102', 'gf', 12, sms=30, bus=384)
_add(0x1b00, 'TITAN X (Pascal)', 'TITAN X', 'GP102', 'gf', 12, sms=28, bus=384)
_add(0x1b06, 'GTX 1080 Ti', 'GTX 1080 Ti', 'GP102', 'gf', 11, sms=28, bus=352)
_add(0x1b80, 'GTX 1080', 'GTX 1080', 'GP104', 'gf', 8, sms=20, bus=256)
_add(0x1b82, 'GTX 1070 Ti', 'GTX 1070 Ti', 'GP104', 'gf', 8, sms=19, bus=256)
_add(0x1b81, 'GTX 1070', 'GTX 1070', 'GP104', 'gf', 8, sms=15, bus=256)
_add((0x1c03, 0x1c06, 0x1b83), 'GTX 1060 6GB', 'GTX 1060', 'GP106', 'gf', 6, sms=10, bus=192)
_add(0x1c04, 'GTX 1060 5GB', 'GTX 1060', 'GP106', 'gf', 5, sms=10, bus=160)
_add((0x1c02, 0x1b84), 'GTX 1060 3GB', 'GTX 1060', 'GP106', 'gf', 3, sms=9, bus=192)
_add(0x1c82, 'GTX 1050 Ti', 'GTX 1050 Ti', 'GP107', 'gf', 4, sms=6, bus=128)
_add(0x1c81, 'GTX 1050 2GB', 'GTX 1050', 'GP107', 'gf', 2, sms=5, bus=128)
_add(0x1c83, 'GTX 1050 3GB', 'GTX 1050', 'GP107', 'gf', 3, sms=6, bus=96)
_add(0x1d01, 'GT 1030', 'GT 1030', 'GP108', 'gf', 2, sms=3, bus=64)
_add(0x1d02, 'GT 1010', 'GT 1010', 'GP108', 'gf', 2, sms=2, bus=64)
# ---- 矿卡：没有显示输出，常被刷 BIOS 冒充游戏卡或计算卡出售 ----
_add(0x20c2, 'CMP 170HX', 'CMP 170HX', 'GA100', 'gf', (8, 10), sms=70, note=_CMP)
_add(0x220d, 'CMP 90HX', 'CMP 90HX', 'GA102', 'gf', 10, sms=50, bus=320, note=_CMP)
_add(0x248a, 'CMP 70HX', 'CMP 70HX', 'GA104', 'gf', 8, bus=256, note=_CMP)
_add(0x1e09, 'CMP 50HX', 'CMP 50HX', 'TU102', 'gf', 10, sms=56, bus=320, note=_CMP)
_add(0x1f0b, 'CMP 40HX', 'CMP 40HX', 'TU106', 'gf', 8, sms=36, bus=256, note=_CMP)
_add(0x2189, 'CMP 30HX', 'CMP 30HX', 'TU116', 'gf', 6, sms=22, bus=192, note=_CMP)
_add(0x1b87, 'P104-100', 'P104-100', 'GP104', 'gf', None, note=_MINER)
_add(0x1bc7, 'P104-101', 'P104-101', 'GP104', 'gf', None, note=_MINER)
_add(0x1c07, 'P106-100', 'P106-100', 'GP106', 'gf', 6, note=_MINER)
_add(0x1c09, 'P106-090', 'P106-090', 'GP106', 'gf', 3, note=_MINER)


# 从型号名称里提取“型号键”，用来比对不同来源报告的是不是同一型号：
#   “NVIDIA GeForce RTX 4090 D” → RTX 4090 D   “NVIDIA A100-SXM4-80GB” → A100   “Tesla T4” → T4
_KEY_RES = [
    (re.compile(r'\bRTX PRO ?(\d{4}) ?(D?)\b'), lambda m: 'RTX PRO ' + m.group(1) + m.group(2)),
    (re.compile(r'\bRTX (\d{4})E?(?: SFF)? ?ADA\b'), lambda m: 'RTX %s ADA' % m.group(1)),
    (re.compile(r'\bRTX (A\d{3,4})(?![0-9])'), lambda m: 'RTX ' + m.group(1)),
    (re.compile(r'\bRTX ?([2-6]000) ?D\b'), lambda m: 'RTX PRO %sD' % m.group(1)),   # 驱动名“NVIDIA RTX 6000D”
    (re.compile(r'\b(RTX|GTX) ?(\d{4})( ?TI)?( ?SUPER)?( ?D)?( ?V2)?\b'),
     lambda m: ' '.join(x.strip() for x in m.groups() if x)),
    (re.compile(r'\bGT ?(\d{4})\b'), lambda m: 'GT ' + m.group(1)),
    (re.compile(r'\bGTX TITAN( X| Z| BLACK)?\b'), lambda m: 'GTX TITAN' + (m.group(1) or '')),   # Maxwell/Kepler，不是 Pascal 的 TITAN X
    (re.compile(r'\bTITAN (RTX|XP|X|V)\b'), lambda m: 'TITAN ' + m.group(1)),
    (re.compile(r'\bQUADRO (GV100|GP100)\b'), lambda m: 'QUADRO ' + m.group(1)),
    (re.compile(r'(?<![A-Z0-9])(P(?:400|600|620|1000|2000|2200|4000|5000|6000))(?![0-9])'), lambda m: 'QUADRO ' + m.group(1)),
    (re.compile(r'(?<![A-Z0-9])(T(?:400|600|1000))(?![0-9])'), lambda m: m.group(1)),
    (re.compile(r'\bDGX DISPLAY\b'), lambda m: 'T1000'),
    (re.compile(r'\bMATROX D SERIES D14[58]0\b'), lambda m: 'QUADRO P1000'),   # Matrox 多屏卡，Quadro P1000 芯片
    (re.compile(r'\bCMP ?(\d{2,3}) ?HX\b'), lambda m: 'CMP %sHX' % m.group(1)),
    (re.compile(r'\bP(10[46]) ?(\d{3})\b'), lambda m: 'P%s-%s' % m.groups()),       # 矿卡 P104-100 / P106-100
    (re.compile(r'(?<![A-Z0-9])(GH200|GB200|GB300|H200|H100|H800|H20|B200|B300|B100|A100|A800|A30|A40|A16|A10M|A10G|A10|'
                r'A2|L40S|L40|L20|L4|L2|V100S|V100|P100|P40|P4|T4)(?![0-9])'), lambda m: m.group(1)),
    (re.compile(r'\bPG50[03]\b'), lambda m: 'V100'),     # V100 定制板卡，驱动名“Tesla PG500-216 / PG503-216”
    (re.compile(r'\bPG509\b'), lambda m: 'A100'),      # A100 SXM4 板卡代号，如驱动名“NVIDIA PG509-210”（0x20b2 子系统 1625）
]


def model_key(name):
    t = re.sub(r'[\s\-_/,()\[\]]+', ' ', (name or '').upper())
    for rx, fn in _KEY_RES:
        m = rx.search(t)
        if m:
            return fn(m)
    return None


def db_keys(db):
    """该型号可接受的型号键（大写）；未命名的工程样品 ID 返回空集合"""
    if not db or db['family'].endswith('?'):
        return set()
    return set(k.upper() for k in (db.get('keys') or (db['family'],)))


KNOWN_KEYS = set()
for _d in GPU_DB.values():
    KNOWN_KEYS |= db_keys(_d)


def cores_per_sm(cc):
    """每个 SM 的 FP32 核心数：GP100/Volta/Turing/GA100 为 64，其余为 128"""
    try:
        ma, mi = [int(x) for x in str(cc).split('.')[:2]]
    except ValueError:
        return None
    if (ma, mi) in ((6, 0), (7, 0), (7, 2), (7, 5), (8, 0)):
        return 64
    return 128 if ma in (6, 8, 9, 10, 11, 12) else None


def db_from_pciids(name, devid):
    """型号库里没有的设备 ID：用本机 pci.ids 的名称（如“AD102 [GeForce RTX 4090]”）兜底识别"""
    m = re.match(r'^\s*(\S+)\s*\[(.+)\]\s*$', name or '')
    chip, label = (m.group(1), m.group(2)) if m else (None, (name or '').strip())
    key = model_key(label)
    if not key:
        return None
    arch, cc = _chip_arch(chip)
    up = label.upper()
    tier = 'gf' if ('GEFORCE' in up or 'TITAN' in up) else ('ws' if ('RTX' in up or 'QUADRO' in up) else 'dc')
    return _sku(label, key, '?', None, arch=arch, cc=cc, chip=chip, tier=tier, src='pci.ids')

NVSWITCH_DB = {0x1af1: 'NVSwitch Gen2 (A100)', 0x22a3: 'NVSwitch Gen3 (H100)'}

MLX_DB = {
    0x1013: 'ConnectX-4', 0x1015: 'ConnectX-4 Lx', 0x1017: 'ConnectX-5', 0x1019: 'ConnectX-5 Ex',
    0x101b: 'ConnectX-6', 0x101d: 'ConnectX-6 Dx', 0x101f: 'ConnectX-6 Lx', 0x1021: 'ConnectX-7',
    0x1023: 'ConnectX-8', 0x1025: 'ConnectX-9', 0xa2d6: 'BlueField-2', 0xa2d9: 'BlueField-3 Lx',
    0xa2dc: 'BlueField-3', 0x1014: 'ConnectX-4 VF', 0x1016: 'ConnectX-4 Lx VF',
    0x1018: 'ConnectX-5 VF', 0x101a: 'ConnectX-5 Ex VF', 0x101c: 'ConnectX-6 VF', 0x101e: 'ConnectX VF',
}

# Xid 是 NVIDIA 驱动写进内核日志的 GPU 错误码；报告里写成“Xid 79（说明）”，所以说明里不再用括号
XID_DESC = {
    13: '图形引擎异常，多为应用问题', 31: 'GPU 访问显存出错，多为应用问题',
    43: 'GPU 停止处理，多为应用问题', 45: '进程被终止后的清理', 48: '双比特 ECC 错误，无法纠正',
    61: 'GPU 内部微控制器报错', 62: 'GPU 内部微控制器停止响应', 63: '显存坏页已屏蔽或已做行重映射',
    64: '显存坏页屏蔽或行重映射失败', 68: '视频解码引擎异常', 69: '图形引擎类错误', 74: 'NVLink 错误',
    79: 'GPU 掉卡，fallen off the bus', 92: '单比特 ECC 错误过多', 94: 'ECC 错误，已隔离，只影响出错的进程',
    95: 'ECC 错误，未能隔离，需要复位 GPU', 109: '上下文切换超时', 119: 'GSP RPC 超时', 120: 'GSP 错误',
}
XID_APP = {13, 31, 43, 45, 68, 69, 109}

THROTTLE_CN = {
    'sw_power_cap': '触发功耗墙', 'hw_slowdown': '硬件降频', 'hw_thermal_slowdown': '硬件过热降频',
    'hw_power_brake_slowdown': '电源侧强制降频(power brake)', 'sw_thermal_slowdown': '驱动过热降频',
    'sync_boost': '与同组 GPU 同步频率', 'applications_clocks_setting': '受应用时钟设置限制',
    'display_clocks_setting': '受显示时钟设置限制',
}
THROTTLE_BAD = {'hw_slowdown', 'hw_thermal_slowdown', 'hw_power_brake_slowdown', 'sw_thermal_slowdown'}

TOPO_RANK = {'X': 0, 'PIX': 1, 'PXB': 2, 'PHB': 3, 'NODE': 4, 'SOC': 5, 'SYS': 5}


# ============================================================================
# 基础工具
# ============================================================================

def P(path):
    return G.sysroot + path if G.sysroot else path


def read_text(path, default=None, limit=1 << 22):
    try:
        with open(P(path), 'rb') as f:
            return f.read(limit).decode('utf-8', 'replace').strip()
    except Exception:
        return default


def read_hex(path):
    s = read_text(path)
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None


def read_int(path):
    s = read_text(path)
    try:
        return int(s.split()[0]) if s else None
    except (ValueError, IndexError):
        return None


def listdir(path):
    try:
        return sorted(os.listdir(P(path)))
    except Exception:
        return []


def exists(path):
    return os.path.exists(P(path))


def link_name(path):
    """符号链接目标的最后一段（驱动名、PCI 地址等）"""
    full = P(path)
    try:
        return os.path.basename(os.path.realpath(full)) if os.path.exists(full) else None
    except Exception:
        return None


def which(name, extra=()):
    dirs = os.environ.get('PATH', '').split(os.pathsep)
    dirs += ['/usr/sbin', '/sbin', '/usr/local/sbin', '/usr/bin', '/bin'] + list(extra)
    for d in dirs:
        if d:
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def _dec(b):
    return (b or b'').decode('utf-8', 'replace')


def run(cmd, timeout=30):
    """运行只读命令，返回 (rc, stdout, stderr)。命令不存在返回 127，超时返回 124。"""
    if not cmd or not cmd[0]:
        return 127, '', 'not found'
    env = dict(os.environ)
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    env.pop('LD_PRELOAD', None)       # 不让本进程环境里的 LD_PRELOAD 影响被调用的工具
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, close_fds=True)
    except OSError as e:
        return 127, '', str(e)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out, err = b'', b''
        return 124, _dec(out), '超时（%ss）' % timeout
    return p.returncode, _dec(out), _dec(err)


def first_line(*texts):
    for t in texts:
        for line in (t or '').splitlines():
            if line.strip():
                return line.strip()[:200]
    return ''


def num(s):
    """'700.00 W' -> 700.0；'81559 MiB' -> 81559.0；无法解析返回 None"""
    if s is None:
        return None
    m = re.search(r'-?\d+(?:\.\d+)?', str(s))
    return float(m.group(0)) if m else None


def norm_bdf(s):
    """统一 PCI 地址格式：00000000:18:00.0 / 0000:18:00.0 -> 0000:18:00.0"""
    if not s:
        return None
    m = re.search(r'([0-9a-fA-F]+):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])', s)
    if not m:
        return s.strip().lower()
    return '%04x:%s:%s.%s' % (int(m.group(1), 16), m.group(2).lower(), m.group(3).lower(), m.group(4))


def short_bdf(bdf):
    return bdf[5:] if bdf and bdf.startswith('0000:') else (bdf or '-')


def pcie_gen(speed):
    m = re.match(r'\s*([\d.]+)\s*GT/s', speed or '')
    if not m:
        return None
    v = float(m.group(1))
    for gen, gts in ((6, 64), (5, 32), (4, 16), (3, 8), (2, 5), (1, 2.5)):
        if abs(v - gts) < 0.6:
            return gen
    return None


def fmt_link(speed, width):
    gen = pcie_gen(speed)
    w = (width or '').strip().rstrip('x')
    if gen is None and not w:
        return '-'
    return 'Gen%s x%s' % (gen if gen else '?', w if w else '?')


def compress_ids(ids):
    ids = sorted(set(i for i in ids if i is not None))
    out, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else '%d-%d' % (ids[i], ids[j]))
        i = j + 1
    return ','.join(out)


def s(v, default='-'):
    if v is None or v == '':
        return default
    return str(v)


def iv(v, default='-'):
    """数值取整显示；0 显示为 0（不会因为 0.0 为假而显示成默认值）"""
    if v is None or v == '':
        return default
    try:
        return str(int(round(float(v))))
    except (TypeError, ValueError):
        return str(v)


def fmt_duration(sec):
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    return ('%d 天 %d 小时' % (d, h)) if d else ('%d 小时 %d 分' % (h, rem // 60))


# ---------------------------------------------------------------- 颜色与表格

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _c(code, t):
    return '\x1b[%sm%s\x1b[0m' % (code, t) if (G.color and t) else t


def red(t): return _c('1;31', t)
def green(t): return _c('32', t)
def yellow(t): return _c('1;33', t)
def cyan(t): return _c('36', t)
def bold(t): return _c('1', t)
def dim(t): return _c('2', t)


def lvl_color(level, t):
    return {OK: green, INFO: cyan, WARN: yellow, FAIL: red}.get(level, str)(t)


def badge(level):
    return lvl_color(level, LEVEL_TEXT[level])


def dwidth(t):
    """终端显示宽度（中文/全角占 2 列）"""
    w = 0
    for ch in ANSI_RE.sub('', t):
        if unicodedata.combining(ch) or ord(ch) < 32:
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return w


def clip(t, maxw):
    """按显示宽度截断（只用于不含颜色的字符串）"""
    if maxw is None or dwidth(t) <= maxw:
        return t
    out, w = [], 0
    for ch in t:
        cw = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
        if w + cw > maxw - 2:
            break
        out.append(ch)
        w += cw
    return ''.join(out) + '..'


def pad(t, width, align='l'):
    gap = max(0, width - dwidth(t))
    if align == 'r':
        return ' ' * gap + t
    if align == 'c':
        return ' ' * (gap // 2) + t + ' ' * (gap - gap // 2)
    return t + ' ' * gap


class Report(object):
    def __init__(self):
        self.lines = []

    def add(self, text=''):
        self.lines.extend(text.split('\n'))

    def section(self, title):
        self.add('')
        self.add(bold(cyan('■ ' + title)))

    def sub(self, title):
        self.add('  ' + bold(title))

    def note(self, text):
        for line in text.split('\n'):
            self.add('  ' + dim(line))

    def table(self, headers, rows, aligns=None, indent=2):
        n = len(headers)
        rows = [None if r is None else [s(c) for c in (list(r) + [''] * n)[:n]] for r in rows]
        widths = [dwidth(h) for h in headers]
        for r in rows:
            if r is not None:
                for i, c in enumerate(r):
                    widths[i] = max(widths[i], dwidth(c))
        aligns = (aligns or '') + 'l' * n
        pre = ' ' * indent
        line = pre + '+' + '+'.join('-' * (w + 2) for w in widths) + '+'
        self.add(line)
        self.add(pre + '|' + '|'.join(' %s ' % pad(bold(h), widths[i]) for i, h in enumerate(headers)) + '|')
        self.add(line.replace('-', '='))
        for r in rows:
            if r is None:
                self.add(line)
                continue
            self.add(pre + '|' + '|'.join(' %s ' % pad(c, widths[i], aligns[i]) for i, c in enumerate(r)) + '|')
        self.add(line)

    def kv(self, pairs, indent=2):
        self.table(['项目', '信息'], [p for p in pairs if p is not None], indent=indent)


class Findings(object):
    items = []   # (category, level, message)

    @classmethod
    def add(cls, cat, level, msg):
        cls.items.append((cat, level, msg))

    @classmethod
    def worst(cls, cats=None):
        lv = [lvl for c, lvl, _ in cls.items if cats is None or c in cats]
        return max(lv) if lv else OK


# ============================================================================
# 采集：系统 / CPU / 内存 / 存储
# ============================================================================

def collect_system():
    st = {}
    st['hostname'] = read_text('/proc/sys/kernel/hostname') or socket.gethostname()
    osr = {}
    for line in (read_text('/etc/os-release', '') or '').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            osr[k.strip()] = v.strip().strip('"')
    st['os'] = osr.get('PRETTY_NAME') or ' '.join(x for x in (osr.get('NAME'), osr.get('VERSION')) if x) or None
    st['kernel'] = read_text('/proc/sys/kernel/osrelease') or platform.release()
    st['arch'] = (read_text('/proc/sys/kernel/arch') if G.test_mode else None) or platform.machine()   # 测试时取夹具里的架构
    up = read_text('/proc/uptime')
    try:
        st['uptime'] = fmt_duration(float(up.split()[0])) if up else None
    except ValueError:
        st['uptime'] = None
    for k in ('sys_vendor', 'product_name', 'product_serial', 'board_vendor', 'board_name',
              'bios_vendor', 'bios_version', 'bios_date'):
        st[k] = read_text('/sys/class/dmi/id/' + k)
    virt = None
    sdv = which('systemd-detect-virt')
    if sdv:
        _, out, _ = run([sdv, '--vm'], 10)
        virt = out.strip() or None
    if virt in (None, 'none'):
        cpuinfo = read_text('/proc/cpuinfo', '') or ''
        if re.search(r'^flags\s*:.*\bhypervisor\b', cpuinfo, re.M):
            virt = 'vm'
        elif virt is None:
            virt = 'none'
    st['virt'] = virt
    cg = read_text('/proc/1/cgroup', '') or ''
    st['container'] = (exists('/.dockerenv') or exists('/run/.containerenv')
                       or bool(re.search(r'docker|kubepods|containerd|lxc', cg)))
    return st


def collect_cpu():
    txt = read_text('/proc/cpuinfo', '') or ''
    model, phys, cores_per, threads = None, set(), None, 0
    for line in txt.splitlines():
        if ':' not in line:
            continue
        k, v = [x.strip() for x in line.split(':', 1)]
        if k == 'processor':
            threads += 1
        elif k == 'model name' and not model:
            model = v
        elif k == 'physical id':
            phys.add(v)
        elif k == 'cpu cores' and cores_per is None:
            try:
                cores_per = int(v)
            except ValueError:
                pass
    sockets = len(phys) or None
    if not model or not sockets or not cores_per:            # ARM（Grace）等平台用 lscpu 补全
        lscpu = which('lscpu')
        if lscpu:
            _, out, _ = run([lscpu], 10)
            kv = {}
            for line in out.splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    kv[k.strip()] = v.strip()
            model = model or kv.get('Model name')
            try:
                sockets = sockets or int(kv.get('Socket(s)', '0')) or None
                cores_per = cores_per or int(kv.get('Core(s) per socket', '0')) or None
            except ValueError:
                pass
    numa = len([n for n in listdir('/sys/devices/system/node') if re.match(r'node\d+$', n)])
    return dict(model=model, sockets=sockets, cores=(cores_per * sockets) if (cores_per and sockets) else None,
                threads=threads or None, numa=numa or None)


def _size_gb(txt):
    m = re.match(r'(\d+)\s*(GB|MB|TB|GiB|MiB|TiB)', txt or '')
    if not m:
        return None
    v, u = int(m.group(1)), m.group(2)[0]
    return v / 1024.0 if u == 'M' else (v * 1024 if u == 'T' else v)


def collect_memory():
    mem = {'total_gib': None, 'dimms': None}
    mt = re.search(r'MemTotal:\s+(\d+) kB', read_text('/proc/meminfo', '') or '')
    if mt:
        mem['total_gib'] = int(mt.group(1)) / 1048576.0
    dmi = which('dmidecode')
    if not G.is_root:
        mem['dimm_note'] = '需要 root 权限运行 dmidecode'
    elif not dmi:
        mem['dimm_note'] = '未安装 dmidecode'
    else:
        rc, out, err = run([dmi, '-t', '17'], 30)
        dimms = []
        for block in re.split(r'\n(?=Handle )', out if rc == 0 else ''):
            if 'Memory Device' not in block:
                continue
            kv = {}
            for line in block.splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    kv.setdefault(k.strip(), v.strip())
            dimms.append(kv)
        if dimms:
            mem['dimms'] = dimms
        else:
            why = (err or out or '').strip().splitlines()
            mem['dimm_note'] = 'dmidecode 没有读到内存条信息%s' % ('：' + clip(why[0], 60) if why else '')
    return mem


def collect_storage():
    lb = which('lsblk')
    if not lb:
        return None
    rc, out, _ = run([lb, '-d', '-n', '-P', '-b', '-o', 'NAME,MODEL,SIZE,TYPE,TRAN,ROTA'], 20)
    if rc != 0:
        rc, out, _ = run([lb, '-d', '-n', '-P', '-b', '-o', 'NAME,MODEL,SIZE,TYPE,ROTA'], 20)
    disks = []
    for line in out.splitlines():
        kv = dict(re.findall(r'([\w:-]+)="([^"]*)"', line))
        if kv.get('TYPE') == 'disk' and not re.match(r'(loop|ram|zram|nbd)', kv.get('NAME', '')):
            disks.append(kv)
    return disks


# ============================================================================
# 采集：PCI 设备（sysfs，内核 PCI 子系统提供，与 NVIDIA 驱动无关）
# ============================================================================

def is_nv_gpu(p):
    return p and p.get('vendor') == 0x10de and (p.get('cls') or 0) >> 8 in (0x0300, 0x0302)


def is_nvswitch(p):
    return p and p.get('vendor') == 0x10de and (p.get('cls') or 0) >> 8 == 0x0680


def scan_pci():
    devs = {}
    for bdf in listdir('/sys/bus/pci/devices'):
        base = '/sys/bus/pci/devices/' + bdf
        vendor = read_hex(base + '/vendor')
        if vendor is None:
            continue
        p = dict(bdf=bdf, vendor=vendor, device=read_hex(base + '/device'), cls=read_hex(base + '/class'))
        # 只对关心的设备（NVIDIA、Mellanox、网卡）读取详细属性，尽量少碰硬件
        if vendor in (0x10de, 0x15b3) or ((p['cls'] or 0) >> 16) == 0x02:
            p.update(svendor=read_hex(base + '/subsystem_vendor'), sdevice=read_hex(base + '/subsystem_device'),
                     driver=link_name(base + '/driver'), numa=read_int(base + '/numa_node'),
                     cur_speed=read_text(base + '/current_link_speed'),
                     cur_width=read_text(base + '/current_link_width'),
                     max_speed=read_text(base + '/max_link_speed'),
                     max_width=read_text(base + '/max_link_width'),
                     is_vf=exists(base + '/physfn'))
        devs[bdf] = p
    return devs


def read_cfg_ids(bdf):
    """实时读取 PCI 配置空间前 64 字节（普通用户也可读），与内核缓存的设备 ID 交叉核对"""
    try:
        with open(P('/sys/bus/pci/devices/%s/config' % bdf), 'rb') as f:
            b = f.read(64)
        if len(b) < 0x30:
            return None
        v, d = struct.unpack_from('<HH', b, 0)
        sv, sd = struct.unpack_from('<HH', b, 0x2c)
        return dict(vendor=v, device=d, svendor=sv, sdevice=sd)
    except Exception:
        return None


def lspci_names():
    lp = which('lspci')
    names = {}
    if not lp:
        return names
    rc, out, _ = run([lp, '-D', '-mm', '-nn'], 20)
    for line in out.splitlines():
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) >= 4:
            names[parts[0].lower()] = re.sub(r'\s*\[[0-9a-fA-F]{4}\]$', '', parts[3])
    return names


def loaded_modules():
    mods = set()
    for line in (read_text('/proc/modules', '') or '').splitlines():
        if line.strip():
            mods.add(line.split()[0])
    return mods


# ============================================================================
# 采集：NVIDIA 驱动、驱动软件完整性
# ============================================================================

def collect_driver():
    d = {'proc_ok': False}
    txt = read_text('/proc/driver/nvidia/version')
    if txt:
        d['proc_ok'] = True
        first = txt.splitlines()[0]
        d['kmod_line'] = re.sub(r'^NVRM version:\s*', '', first).strip()
        m = re.search(r'Kernel Module(?:\s+for\s+\S+)?\s+(\d+\.\d+(?:\.\d+)?)', first)
        d['kmod_version'] = m.group(1) if m else None
        d['kmod_open'] = 'Open Kernel Module' in first
        m = re.search(r'\(([^()]*@[^()]*)\)', first)
        d['kmod_builder'] = m.group(1) if m else None
    d['taint'] = read_text('/sys/module/nvidia/taint')
    d['srcversion'] = read_text('/sys/module/nvidia/srcversion')
    preload = read_text('/etc/ld.so.preload') or ''
    d['preload_file'] = [x.strip() for x in preload.splitlines() if x.strip() and not x.strip().startswith('#')]
    d['preload_env'] = os.environ.get('LD_PRELOAD')
    return d


def locate_all(name, extra_dirs):
    found, seen = [], set()
    for d in os.environ.get('PATH', '').split(os.pathsep) + list(extra_dirs):
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                found.append(p)
    return found


def pkg_verify(path):
    """返回 (包名, 包版本, 校验结果 True/False/None, 说明)。只读：dpkg --verify / rpm -V 仅比对校验和。"""
    if which('dpkg-query'):
        rc, out, _ = run(['dpkg-query', '-S', path], 20)
        line = first_line(out)
        if rc == 0 and ': ' in line and not line.startswith('diversion'):
            pkg = line.rsplit(': ', 1)[0].split(', ')[0].strip()
            _, ver, _ = run(['dpkg-query', '-W', '-f', '${Version}', pkg], 20)
            rc2, out2, _ = run(['dpkg', '--verify', pkg], 90)
            for l2 in out2.splitlines():
                if l2.rstrip().endswith(path) and len(l2) > 2 and l2[2] == '5':
                    return pkg, ver.strip(), False, '与软件包 %s 里的原始文件不一致（校验和不同）' % pkg
            return pkg, ver.strip(), True, '与软件包 %s %s 里的原始文件一致（dpkg 校验）' % (pkg, ver.strip())
    if which('rpm'):
        rc, out, _ = run(['rpm', '-qf', '--qf', '%{NAME} %{VERSION}\n', path], 20)
        parts = first_line(out).split()
        if rc == 0 and len(parts) == 2 and 'not owned' not in out:
            pkg, ver = parts
            rc2, out2, _ = run(['rpm', '-V', pkg], 90)
            for l2 in out2.splitlines():
                if l2.rstrip().endswith(path) and len(l2) > 2 and l2[2] == '5':
                    return pkg, ver, False, '与软件包 %s 里的原始文件不一致（校验和不同）' % pkg
            return pkg, ver, True, '与软件包 %s %s 里的原始文件一致（rpm 校验）' % (pkg, ver)
    return None, None, None, '不是用包管理器安装的（如用 .run 安装包安装），没有软件包可以比对'


def file_check(path):
    info = dict(path=path, real=os.path.realpath(path), elf=None, size=None, sha256=None)
    try:
        with open(info['real'], 'rb') as f:
            data = f.read(64 << 20)
        info['elf'] = data[:4] == b'\x7fELF'
        info['script'] = data[:2] == b'#!'
        if not info['elf']:
            head = data[:300].decode('utf-8', 'replace').splitlines()[:3]
            info['head'] = ' | '.join(re.sub(r'[\x00-\x1f\x7f]', '', h).strip() for h in head if h.strip())
        info['size'] = len(data)
        info['sha256'] = hashlib.sha256(data).hexdigest()
    except Exception as e:
        info['error'] = str(e)
    info['pkg'], info['pkg_ver'], info['pkg_ok'], info['pkg_msg'] = pkg_verify(info['real'])
    return info


def loaded_library_path(substr):
    try:
        with open('/proc/self/maps') as f:
            for line in f:
                if substr in line and '/' in line:
                    return line[line.index('/'):].strip()
    except Exception:
        pass
    return None


# ============================================================================
# 采集：nvidia-smi（只读查询）
# ============================================================================

_NA = ('N/A', '[N/A]', 'Unknown Error', '[Unknown Error]', 'Not Supported', '[Not Supported]',
       'Requested functionality has been deprecated', 'None')


def xt(e, path, default=None):
    if e is None:
        return default
    x = e.find(path)
    if x is None or x.text is None:
        return default
    v = x.text.strip()
    return default if (not v or v in _NA) else v


def xfirst(e, paths):
    for p in paths:
        v = xt(e, p)
        if v is not None:
            return v
    return None


def _sum_unc(e):
    if e is None:
        return None
    total, seen = 0, False
    for c in e:
        if 'uncorrectable' in c.tag:
            v = num((c.text or '').strip())
            if v is not None:
                total += int(v)
                seen = True
    return total if seen else None


def parse_gpu_xml(g):
    d = dict(
        bus_id=norm_bdf(g.get('id') or xt(g, 'pci/pci_bus_id')),
        name=xt(g, 'product_name'), brand=xt(g, 'product_brand'), arch=xt(g, 'product_architecture'),
        serial=xt(g, 'serial'), uuid=xt(g, 'uuid'), vbios=xt(g, 'vbios_version'),
        board_pn=xt(g, 'board_part_number'), gpu_pn=xt(g, 'gpu_part_number'),
        module_id=xfirst(g, ['gpu_module_id', 'module_id', 'platformInfo/module_id']),
        inforom=xt(g, 'inforom_version/img_version'), gsp=xt(g, 'gsp_firmware_version'),
        persistence=xt(g, 'persistence_mode'), mig=xt(g, 'mig_mode/current_mig'),
        compute_mode=xt(g, 'compute_mode'),
        pci_devid=xt(g, 'pci/pci_device_id'), pci_subsys=xt(g, 'pci/pci_sub_system_id'),
        link_gen_cur=xt(g, 'pci/pci_gpu_link_info/pcie_gen/current_link_gen'),
        link_w_cur=xt(g, 'pci/pci_gpu_link_info/link_widths/current_link_width'),
        replay=num(xt(g, 'pci/replay_counter')),
        mem_total=num(xt(g, 'fb_memory_usage/total')), mem_used=num(xt(g, 'fb_memory_usage/used')),
        util=num(xt(g, 'utilization/gpu_util')),
        ecc=xt(g, 'ecc_mode/current_ecc'), ecc_pending=xt(g, 'ecc_mode/pending_ecc'),
        ecc_vol_unc=_sum_unc(g.find('ecc_errors/volatile')),
        ecc_agg_unc=_sum_unc(g.find('ecc_errors/aggregate')),
        rr_corr=num(xt(g, 'remapped_rows/remapped_row_corr')),
        rr_unc=num(xt(g, 'remapped_rows/remapped_row_unc')),
        rr_pending=xt(g, 'remapped_rows/remapped_row_pending'),
        rr_failure=xt(g, 'remapped_rows/remapped_row_failure'),
        temp=num(xt(g, 'temperature/gpu_temp')), mem_temp=num(xt(g, 'temperature/memory_temp')),
        power_draw=num(xfirst(g, ['gpu_power_readings/power_draw', 'gpu_power_readings/instant_power_draw',
                                  'gpu_power_readings/average_power_draw', 'power_readings/power_draw'])),
        power_limit=num(xfirst(g, ['gpu_power_readings/current_power_limit',
                                   'power_readings/enforced_power_limit', 'power_readings/power_limit'])),
        power_max=num(xfirst(g, ['gpu_power_readings/max_power_limit', 'power_readings/max_power_limit'])),
        max_sm=num(xfirst(g, ['max_clocks/sm_clock', 'max_clocks/graphics_clock'])),
        max_mem=num(xt(g, 'max_clocks/mem_clock')),
        fabric_state=xt(g, 'fabric/state'), fabric_status=xt(g, 'fabric/status'),
        reset_required=xt(g, 'gpu_reset_status/reset_required'),
        drain_reset=xt(g, 'gpu_reset_status/drain_and_reset_recommended'),
        recovery=xt(g, 'gpu_recovery_action'),
        procs=len(g.findall('processes/process_info')),
    )
    thr = []
    e = g.find('clocks_event_reasons')
    if e is None:
        e = g.find('clocks_throttle_reasons')
    if e is not None:
        for c in e:
            if (c.text or '').strip() == 'Active':
                key = re.sub(r'^clocks_(event|throttle)_reason_', '', c.tag)
                if key != 'gpu_idle':
                    thr.append(key)
    d['throttle'] = thr
    return d


def collect_smi(path):
    res = {'ok': False, 'error': None, 'gpus': [], 'driver': None, 'cuda': None}
    if not path:
        res['error'] = '未找到 nvidia-smi'
        return res
    rc, out, err = run([path, '-q', '-x'], 120)
    if rc != 0 or '<nvidia_smi_log' not in out:
        res['error'] = first_line(out, err) or ('退出码 %d' % rc)
        return res
    try:
        root = ET.fromstring(out[out.index('<nvidia_smi_log'):])
    except ET.ParseError as e:
        res['error'] = 'XML 解析失败：%s' % e
        return res
    res['driver'] = xt(root, 'driver_version')
    res['cuda'] = xt(root, 'cuda_version')
    for i, g in enumerate(root.findall('gpu')):
        d = parse_gpu_xml(g)
        d['index'] = i
        res['gpus'].append(d)
    res['ok'] = True
    return res


def collect_proc_gpus():
    res = {}
    for bdf in listdir('/proc/driver/nvidia/gpus'):
        kv = {}
        for line in (read_text('/proc/driver/nvidia/gpus/%s/information' % bdf, '') or '').splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                kv[k.strip()] = v.strip()
        res[norm_bdf(bdf)] = dict(model=kv.get('Model'), uuid=kv.get('GPU UUID'), vbios=kv.get('Video BIOS'),
                                  excluded=kv.get('GPU Excluded'))
    return res


def collect_topo(smi):
    res = {'ok': False, 'cols': [], 'rows': {}, 'order': [], 'nic_map': {}, 'error': None}
    if not smi:
        return res
    rc, out, err = run([smi, 'topo', '-m'], 60)
    lines = [ANSI_RE.sub('', l.rstrip('\n')) for l in out.splitlines()]
    hi = next((i for i, l in enumerate(lines) if re.search(r'\bGPU0\b', l)), None)
    if rc != 0 or hi is None:
        res['error'] = first_line(out, err) or '无输出'
        return res
    header = lines[hi]
    tab = '\t' in header
    cols = [c.strip() for c in header.split('\t')] if tab else re.split(r'\s{2,}', header.strip())
    while cols and cols[0] == '':
        cols.pop(0)
    for l in lines[hi + 1:]:
        if not l.strip():
            break
        parts = [c.strip() for c in l.split('\t')] if tab else l.split()
        label, vals = parts[0], parts[1:]
        res['rows'][label] = dict(zip(cols, vals))
        res['order'].append(label)
    for l in lines:
        m = re.match(r'\s*(NIC\d+)\s*:\s*(\S+)', l)
        if m:
            res['nic_map'][m.group(1)] = m.group(2)
    res['cols'] = cols
    res['ok'] = True
    return res


def _parse_by_gpu(out):
    res, cur = {}, None
    for l in out.splitlines():
        m = re.match(r'^GPU (\d+): (.*?)(?: \(UUID: (GPU-[0-9a-fA-F-]+)\))?\s*$', l)
        if m:
            cur = int(m.group(1))
            res[cur] = {'name': m.group(2), 'uuid': m.group(3), 'links': {}}
            continue
        m = re.match(r'^\s+Link (\d+): (.*)$', l)
        if m and cur is not None:
            res[cur]['links'].setdefault(int(m.group(1)), []).append(m.group(2).strip())
    return res


_NVL_ERR_RE = re.compile(r'(replay|recovery|crc|malformed|overrun|rx (remote |general )?errors|integrity|discard)', re.I)
_NVL_SKIP_RE = re.compile(r'(fec|ber|symbol|successful|packets|bytes)', re.I)


def collect_nvlink(smi):
    res = {'status': {}, 'errors': {}, 'error': None}
    if not smi:
        return res
    rc, out, err = run([smi, 'nvlink', '-s'], 60)          # 只读：链路状态/速率
    if rc == 0:
        res['status'] = _parse_by_gpu(out)
    else:
        res['error'] = first_line(out, err)
    rc, out, err = run([smi, 'nvlink', '-e'], 60)          # 只读：错误计数（不重置）
    if rc == 0:
        for gi, info in _parse_by_gpu(out).items():
            total = 0
            for vals in info['links'].values():
                for v in vals:
                    m = re.match(r'(.+?):\s*(\d+)\s*$', v)
                    if m and _NVL_ERR_RE.search(m.group(1)) and not _NVL_SKIP_RE.search(m.group(1)):
                        total += int(m.group(2))
            res['errors'][gi] = total
    return res


def _pkg_version(patterns):
    if which('dpkg-query'):
        rc, out, _ = run(['dpkg-query', '-W', '-f', '${db:Status-Abbrev} ${Package} ${Version}\n'] + patterns, 20)
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 3 and p[0].startswith('ii'):
                return p[1], p[2]
    if which('rpm'):
        rc, out, _ = run(['rpm', '-qa', '--qf', '%{NAME} %{VERSION}-%{RELEASE}\n'] + patterns, 30)
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 2:
                return p[0], p[1]
    return None, None


def collect_fm():
    fm = {'load': None, 'active': None, 'sub': None, 'version': None}
    sc = which('systemctl')
    if sc:
        _, out, _ = run([sc, 'show', 'nvidia-fabricmanager', '-p', 'LoadState', '-p', 'ActiveState',
                         '-p', 'SubState', '--no-pager'], 15)
        kv = dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
        fm.update(load=kv.get('LoadState'), active=kv.get('ActiveState'), sub=kv.get('SubState'))
    _, fm['version'] = _pkg_version(['nvidia-fabricmanager*', 'nvidia-fabric-manager*', 'cuda-drivers-fabricmanager*'])
    return fm


# ============================================================================
# 采集：NVML 直连（ctypes 调用 libnvidia-ml，绕过 nvidia-smi 程序，只读）
# ============================================================================

class _NvmlPciInfo(ctypes.Structure):
    _fields_ = [('busIdLegacy', ctypes.c_char * 16), ('domain', ctypes.c_uint), ('bus', ctypes.c_uint),
                ('device', ctypes.c_uint), ('pciDeviceId', ctypes.c_uint), ('pciSubSystemId', ctypes.c_uint),
                ('busId', ctypes.c_char * 32)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong), ('used', ctypes.c_ulonglong)]


def _cs(buf):
    """ctypes 字符串缓冲区或结构体 char[] 字段（后者直接是 bytes）转 str"""
    try:
        v = buf.value if hasattr(buf, 'value') else buf
        return v.decode('utf-8', 'replace').strip() or None
    except Exception:
        return None


NVML_ERR = {4: '权限不足', 9: '驱动未加载', 12: '找不到驱动库', 18: 'NVML 库与内核模块版本不一致', 999: '未知错误'}


def collect_nvml(libname):
    res = {'ok': False, 'error': None, 'gpus': {}, 'lib': None, 'driver': None}
    try:
        lib = ctypes.CDLL(libname)
    except OSError:
        res['error'] = '无法加载 %s' % libname
        return res

    def fn(name):
        f = getattr(lib, name, None)
        if f is not None:
            f.restype = ctypes.c_int
        return f

    init = fn('nvmlInit_v2') or fn('nvmlInit')
    if init is None:
        res['error'] = 'NVML 库缺少 nvmlInit'
        return res
    rc = init()
    if rc != 0:
        res['error'] = 'nvmlInit 失败（错误码 %d%s）' % (rc, '，' + NVML_ERR[rc] if rc in NVML_ERR else '')
        return res
    byref, c_uint, c_int = ctypes.byref, ctypes.c_uint, ctypes.c_int
    try:
        res['lib'] = loaded_library_path('libnvidia-ml') or libname
        buf = ctypes.create_string_buffer(96)
        f = fn('nvmlSystemGetDriverVersion')
        if f and f(buf, c_uint(96)) == 0:
            res['driver'] = _cs(buf)
        cnt = c_uint(0)
        f = fn('nvmlDeviceGetCount_v2') or fn('nvmlDeviceGetCount')
        if f is None or f(byref(cnt)) != 0:
            res['error'] = 'nvmlDeviceGetCount 失败'
            return res
        get_handle = fn('nvmlDeviceGetHandleByIndex_v2') or fn('nvmlDeviceGetHandleByIndex')
        for i in range(cnt.value):
            h = ctypes.c_void_p()
            if get_handle(c_uint(i), byref(h)) != 0:
                continue
            g = {'index': i}

            def sbuf(fname, size):
                b = ctypes.create_string_buffer(size)
                ff = fn(fname)
                return _cs(b) if (ff and ff(h, b, c_uint(size)) == 0) else None

            def uint(fname, *pre):
                v = c_uint(0)
                ff = fn(fname)
                return v.value if (ff and ff(h, *(pre + (byref(v),))) == 0) else None

            g['name'] = sbuf('nvmlDeviceGetName', 96)
            g['serial'] = sbuf('nvmlDeviceGetSerial', 64)
            g['uuid'] = sbuf('nvmlDeviceGetUUID', 96)
            pci = _NvmlPciInfo()
            f = fn('nvmlDeviceGetPciInfo_v3') or fn('nvmlDeviceGetPciInfo_v2')
            if f and f(h, byref(pci)) == 0:
                g['bus_id'] = norm_bdf(_cs(pci.busId) or _cs(pci.busIdLegacy))
                g['pci_devid'] = pci.pciDeviceId
                g['pci_subsys'] = pci.pciSubSystemId
            mem = _NvmlMemory()
            f = fn('nvmlDeviceGetMemoryInfo')
            if f and f(h, byref(mem)) == 0:
                g['mem_total'] = mem.total
            g['cores'] = uint('nvmlDeviceGetNumGpuCores')
            g['bus_width'] = uint('nvmlDeviceGetMemoryBusWidth')
            g['max_sm'] = uint('nvmlDeviceGetMaxClockInfo', c_int(1))     # NVML_CLOCK_SM
            g['max_mem'] = uint('nvmlDeviceGetMaxClockInfo', c_int(2))    # NVML_CLOCK_MEM
            mn, mx = c_uint(0), c_uint(0)
            f = fn('nvmlDeviceGetPowerManagementLimitConstraints')
            if f and f(h, byref(mn), byref(mx)) == 0:
                g['power_max'] = mx.value / 1000.0
            ma, mi = c_int(0), c_int(0)
            f = fn('nvmlDeviceGetCudaComputeCapability')
            if f and f(h, byref(ma), byref(mi)) == 0:
                g['cc'] = '%d.%d' % (ma.value, mi.value)
            links = {}
            f_state = fn('nvmlDeviceGetNvLinkState')
            f_rtype = fn('nvmlDeviceGetNvLinkRemoteDeviceType')
            f_rpci = fn('nvmlDeviceGetNvLinkRemotePciInfo_v2')
            if f_state:
                for ln in range(18):
                    act = c_int(0)
                    if f_state(h, c_uint(ln), byref(act)) != 0:
                        continue
                    lk = {'active': bool(act.value)}
                    if act.value:
                        rt = c_int(-1)
                        if f_rtype and f_rtype(h, c_uint(ln), byref(rt)) == 0:
                            lk['rtype'] = rt.value          # 0=GPU 1=IBMNPU 2=NVSwitch
                        rp = _NvmlPciInfo()
                        if f_rpci and f_rpci(h, c_uint(ln), byref(rp)) == 0:
                            lk['remote'] = norm_bdf(_cs(rp.busId) or _cs(rp.busIdLegacy))
                    links[ln] = lk
            g['nvlinks'] = links
            if g.get('bus_id'):
                res['gpus'][g['bus_id']] = g
        res['ok'] = True
    finally:
        f = fn('nvmlShutdown')
        if f:
            f()
    return res


# ============================================================================
# 采集：CUDA 设备属性（可选 --cuda；cuInit + cuDeviceGetAttribute，不创建上下文、不占显存）
# ============================================================================

CUDA_ERR = {100: '没有可用 GPU', 101: '设备无效', 802: '系统未就绪，NVSwitch 机器通常是 Fabric Manager 未运行',
            803: '驱动版本不匹配', 999: '未知错误'}


def collect_cuda(libname):
    res = {'ok': False, 'error': None, 'gpus': {}}
    os.environ.pop('CUDA_VISIBLE_DEVICES', None)      # 仅影响本进程
    try:
        lib = ctypes.CDLL(libname)
    except OSError:
        res['error'] = '无法加载 %s' % libname
        return res
    for name in ('cuInit', 'cuDeviceGetCount', 'cuDeviceGet', 'cuDeviceGetName', 'cuDeviceGetAttribute',
                 'cuDeviceGetPCIBusId'):
        f = getattr(lib, name, None)
        if f is None:
            res['error'] = 'libcuda 缺少 %s' % name
            return res
        f.restype = ctypes.c_int
    rc = lib.cuInit(0)
    if rc != 0:
        res['error'] = 'cuInit 失败（错误码 %d%s）' % (rc, '，' + CUDA_ERR[rc] if rc in CUDA_ERR else '')
        return res
    n = ctypes.c_int(0)
    lib.cuDeviceGetCount(ctypes.byref(n))
    for i in range(n.value):
        d = ctypes.c_int(0)
        if lib.cuDeviceGet(ctypes.byref(d), i) != 0:
            continue

        def attr(a):
            v = ctypes.c_int(0)
            return v.value if lib.cuDeviceGetAttribute(ctypes.byref(v), a, d.value) == 0 else None

        name = ctypes.create_string_buffer(256)
        lib.cuDeviceGetName(name, 256, d.value)
        bus = ctypes.create_string_buffer(64)
        lib.cuDeviceGetPCIBusId(bus, 64, d.value)
        g = dict(name=_cs(name), sm=attr(16), bus_width=attr(37), l2=attr(38),
                 cc='%s.%s' % (attr(75), attr(76)))
        bdf = norm_bdf(_cs(bus))
        if bdf:
            res['gpus'][bdf] = g
    res['ok'] = True
    return res


# ============================================================================
# 采集：IB / RDMA 网卡、其它网卡、OFED
# ============================================================================

def parse_vpd(data):
    out, i = {}, 0
    while i < len(data):
        tag = data[i]
        if tag & 0x80:
            if i + 3 > len(data):
                break
            ln = data[i + 1] | (data[i + 2] << 8)
            body = data[i + 3:i + 3 + ln]
            name = tag & 0x7f
            if name == 0x02:
                out['ID'] = body.decode('ascii', 'replace').strip('\x00 ').strip()
            elif name in (0x10, 0x11):
                j = 0
                while j + 3 <= len(body):
                    kw = body[j:j + 2].decode('ascii', 'replace')
                    kl = body[j + 2]
                    if kw in ('PN', 'SN', 'EC', 'MN'):
                        out[kw] = body[j + 3:j + 3 + kl].decode('ascii', 'replace').strip('\x00 ').strip()
                    j += 3 + kl
            i += 3 + ln
        else:
            if (tag >> 3) & 0x0f == 0x0f:      # 结束标记
                out['_end'] = True
                break
            i += 1 + (tag & 0x07)
    return out


def read_vpd(bdf):
    """读取网卡 VPD（型号描述/料号/序列号），需要 root；分块读取，读到结束标记即停止"""
    try:
        data = b''
        with open(P('/sys/bus/pci/devices/%s/vpd' % bdf), 'rb') as f:
            while len(data) < 4096:
                chunk = f.read(256)
                if not chunk:
                    break
                data += chunk
                if parse_vpd(data).get('_end'):
                    break
        return parse_vpd(data)
    except Exception:
        return {}


def _strip_state(v):
    return re.sub(r'^\d+:\s*', '', v) if v else v


def collect_ib(pci):
    hcas = []
    for dev in listdir('/sys/class/infiniband'):
        base = '/sys/class/infiniband/' + dev
        bdf = link_name(base + '/device')
        p = pci.get(bdf) or {}
        h = dict(dev=dev, bdf=bdf, fw=read_text(base + '/fw_ver'), hca_type=read_text(base + '/hca_type'),
                 psid=read_text(base + '/board_id'), node_guid=read_text(base + '/node_guid'),
                 device=p.get('device'), vendor=p.get('vendor'), numa=p.get('numa'), is_vf=p.get('is_vf'),
                 link=fmt_link(p.get('cur_speed'), p.get('cur_width')),
                 link_max=fmt_link(p.get('max_speed'), p.get('max_width')),
                 cur_gen=pcie_gen(p.get('cur_speed')), max_gen=pcie_gen(p.get('max_speed')),
                 cur_w=p.get('cur_width'), max_w=p.get('max_width'))
        h['model'] = MLX_DB.get(p.get('device')) or h['hca_type'] or '-'
        h['netdevs'] = listdir(base + '/device/net')
        ports = []
        for port in listdir(base + '/ports'):
            pb = '%s/ports/%s' % (base, port)
            cnt = {}
            for cn in ('symbol_error', 'port_rcv_errors', 'link_downed', 'link_error_recovery',
                       'local_link_integrity_errors', 'excessive_buffer_overrun_errors'):
                v = read_int(pb + '/counters/' + cn)
                if v is not None:
                    cnt[cn] = v
            ports.append(dict(port=port, state=_strip_state(read_text(pb + '/state')),
                              phys=_strip_state(read_text(pb + '/phys_state')), rate=read_text(pb + '/rate'),
                              link_layer=read_text(pb + '/link_layer'), lid=read_text(pb + '/lid'),
                              sm_lid=read_text(pb + '/sm_lid'), counters=cnt))
        h['ports'] = ports
        h['vpd'] = read_vpd(bdf) if (G.is_root and bdf and p.get('vendor') == 0x15b3) else {}
        hcas.append(h)
    return hcas


def collect_eth(pci, skip_netdevs):
    nics, vfs = [], 0
    for ifn in listdir('/sys/class/net'):
        base = '/sys/class/net/' + ifn
        if not exists(base + '/device') or ifn in skip_netdevs:
            continue
        if exists(base + '/device/physfn'):
            vfs += 1
            continue
        bdf = link_name(base + '/device')
        p = pci.get(bdf) or {}
        speed = read_int(base + '/speed')
        nics.append(dict(ifname=ifn, bdf=bdf, driver=link_name(base + '/device/driver'),
                         speed=speed if speed and speed > 0 else None, state=read_text(base + '/operstate'),
                         mtu=read_text(base + '/mtu'), numa=p.get('numa'), vendor=p.get('vendor'),
                         device=p.get('device'), type=read_text(base + '/type')))
    return nics, vfs


def collect_ofed():
    o = {'ofed': None}
    oi = which('ofed_info')
    if oi:
        rc, out, _ = run([oi, '-s'], 15)
        if rc == 0 and out.strip():
            o['ofed'] = first_line(out).rstrip(':')
    o['mlx5_ver'] = read_text('/sys/module/mlx5_core/version')
    return o


def collect_xid():
    res = {'ok': False, 'src': None, 'xid': [], 'sxid': 0, 'error': None}
    rc, out, _ = run([which('dmesg') or 'dmesg'], 20)
    res['src'] = 'dmesg'
    if rc != 0 or not out.strip():
        jc = which('journalctl')
        if jc:
            rc, out, _ = run([jc, '-k', '-b', '--no-pager', '-o', 'cat'], 40)
            res['src'] = 'journalctl -k -b'
    if rc != 0:
        res['error'] = '无法读取内核日志（通常需要 root 权限）'
        return res
    for line in out.splitlines():
        m = re.search(r'NVRM: Xid \(PCI:([0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})\): (\d+)', line)
        if m:
            dom, bus, dev = m.group(1).split(':')
            res['xid'].append(('%04x:%s:%s' % (int(dom, 16), bus.lower(), dev.lower()), int(m.group(2))))
        elif 'SXid' in line and 'nvswitch' in line.lower():
            res['sxid'] += 1
    res['ok'] = True
    return res


# ============================================================================
# 汇总每张 GPU 的多源数据
# ============================================================================

def assemble_gpus(ctx):
    pci, smi, nv = ctx['pci'], ctx['smi'], ctx['nvml']
    cu = ctx.get('cuda') or {}
    smi_by = dict((g['bus_id'], g) for g in smi.get('gpus', []) if g.get('bus_id'))
    nv_by, cu_by, proc = nv.get('gpus', {}), cu.get('gpus', {}), ctx['proc']
    bdfs = set(b for b, p in pci.items() if is_nv_gpu(p))
    bdfs.update(smi_by)
    bdfs.update(nv_by)
    bdfs.update(proc)
    gpus = []
    for b in sorted(bdfs):
        p = pci.get(b) if is_nv_gpu(pci.get(b)) else None
        rec = dict(bdf=b, pci=p, smi=smi_by.get(b), nvml=nv_by.get(b), cuda=cu_by.get(b), proc=proc.get(b))
        devid = p['device'] if p else None
        if devid is None and rec['smi'] and rec['smi'].get('pci_devid'):
            try:
                devid = int(rec['smi']['pci_devid'][:4], 16)
            except ValueError:
                pass
        rec['devid'] = devid
        rec['db'] = GPU_DB.get(devid)
        if rec['db'] is None and devid is not None and p:
            rec['db'] = db_from_pciids((ctx.get('lspci') or {}).get(b), devid)
        rec['cfg'] = read_cfg_ids(b) if p else None
        rec['idx'] = rec['smi']['index'] if rec['smi'] else (rec['nvml']['index'] if rec['nvml'] else None)
        gpus.append(rec)
    if all(g['idx'] is None for g in gpus):
        for i, g in enumerate(gpus):
            g['idx'] = i
    gpus.sort(key=lambda g: (g['idx'] is None, g['idx'] if g['idx'] is not None else 0, g['bdf']))
    # NVLink 汇总：优先 nvidia-smi nvlink -s，其次 NVML
    nvl = ctx['nvlink']
    for g in gpus:
        st = nvl['status'].get(g['idx']) if g['idx'] is not None else None
        info = None
        if st and st['links']:
            act, inact, speeds = [], [], []
            for ln, vals in sorted(st['links'].items()):
                v = ' '.join(vals)
                m = re.search(r'([\d.]+)\s*GB/s', v)
                if m and 'inactive' not in v.lower():
                    act.append(ln)
                    speeds.append(float(m.group(1)))
                else:
                    inact.append(ln)
            info = dict(total=len(st['links']), active=len(act), inactive=inact, speeds=speeds)
        elif g['nvml'] and g['nvml'].get('nvlinks'):
            lk = g['nvml']['nvlinks']
            info = dict(total=len(lk), active=sum(1 for v in lk.values() if v['active']),
                        inactive=[k for k, v in sorted(lk.items()) if not v['active']], speeds=[])
        if info is not None:
            info['errors'] = nvl['errors'].get(g['idx'])
        g['nvl'] = info
    return gpus


# ============================================================================
# 判定
# ============================================================================

def parse_expect(text):
    """--expect 参数 → None（不限型号）、dict(key, form, fam, label)，无法识别时返回 False"""
    t = (text or '').strip()
    if t.lower() in ('any', 'all', '*', ''):
        return None
    form = None
    m = re.match(r'^(.+?)[-_ ]?(sxm\d?|nvlink|pcie|nvl)$', t, re.I)
    if m:      # nvlink = sxm：国内说的“NVLink 版”就是 SXM 模组（HGX 机型）；H100 NVL 是带 NVLink 桥的 PCIe 卡
        t, form = m.group(1), {'NVLINK': 'SXM', 'PCIE': 'PCIE', 'NVL': 'NVL'}.get(m.group(2).upper(), 'SXM')
    key = next((k for k in (model_key(c) for c in (t, 'RTX ' + t, 'GTX ' + t, 'GT ' + t)) if k in KNOWN_KEYS), None)
    if not key:
        return False
    fam = next((d['family'] for d in GPU_DB.values() if d['family'].upper() == key), key)
    label = fam
    if form:
        forms = Counter(d['form'] for d in GPU_DB.values() if key in db_keys(d) and d['form'].upper().startswith(form))
        if not forms:
            return False
        label = '%s %s' % (fam, forms.most_common(1)[0][0])
    return dict(key=key, form=form, fam=fam, label=label)


def unknown_check(devid, names, exp):
    """型号库和本机 pci.ids 都不认识的设备 ID：只能参考驱动报告的名称。
    驱动名称可能被篡改成期望的型号，所以名称相符也只能判“待确认”；但名称明确是别的型号时，可以判“不是期望型号”。"""
    why = '设备 ID 0x%04x 不在内置型号库中，本机的 pci.ids 也不认识（可能是新型号或工程样品）' % devid
    shown = sorted(set(nm for _, nm in names))
    if not shown:
        return WARN, why
    why += '；驱动报告的名称为“%s”' % '”“'.join(shown)
    keys = set(k for k in (model_key(nm) for nm in shown) if k)
    if exp is not None and keys and exp['key'] not in keys:
        return FAIL, why + '，不是 %s' % exp['fam']
    if len(keys) > 1:
        return WARN, why + '，各来源报告的型号互相矛盾，无法判断'
    if exp is not None and keys:
        return WARN, why + '，与期望的 %s 一致；但驱动名称可能被篡改，又无法用设备 ID 核对，所以只能判待确认' % exp['fam']
    return WARN, why + '；驱动名称可能被篡改，又无法用设备 ID 核对，所以无法确认型号'


def expect_hint(gpus, exp):
    """按默认目标核验、本机却是别的型号时，告诉用户怎样指定 --expect（给出能识别回同一型号的最短写法）"""
    toks = []
    for g in gpus:
        db = g['db']
        if not db:
            continue
        fam = db['family'].lower()
        for t in (re.sub(r'^(nvidia|geforce|quadro|rtx|gtx) ', '', fam).replace(' ', ''), fam.replace(' ', ''),
                  fam.replace(' ', '-')):
            e = parse_expect(t)
            if e and e['key'] in db_keys(db):
                if t not in toks:
                    toks.append(t)
                break
    how = ('请加 --expect %s 重新检测' % ' 或 '.join(toks) if 0 < len(toks) <= 3
           else '请用 --expect 指定期望的型号后重新检测')
    model = '就是 ' + _skus(gpus) if all(g['db'] for g in gpus) else '就不是 ' + exp['fam']
    return ('本次按默认目标 %s 核验。如果这台机器本来%s，%s；'
            '也可以用 --expect any，不限型号，只检查各项数据是否一致。' % (exp['label'], model, how))


def expect_check(db, exp):
    sku, note = db['sku'], db['note']
    via = '（型号库中没有该设备 ID，按本机 pci.ids 识别）' if db.get('src') == 'pci.ids' else ''
    if exp is None:
        return OK, '按设备 ID 识别为 %s%s' % (sku, via)
    keys = db_keys(db)
    if exp['key'] in keys:
        if exp['form'] and not db['form'].upper().startswith(exp['form']):
            if db['form'] == '?':
                return WARN, '无法从设备 ID 判断 %s 是 SXM、PCIe 还是 NVL 形态，不能确认是不是 %s 版' % (sku, exp['label'])
            sfx = {'P': 'pcie', 'N': 'nvl', 'S': 'sxm'}.get(db['form'][:1].upper())
            hint = ('（如果采购的就是 %s 版，请加 --expect %s-%s 重新检测）'
                    % (db['form'], exp['fam'].lower().replace(' ', ''), sfx) if sfx else '')
            return FAIL, '实为 %s，与期望的 %s 版不符%s' % (sku, exp['label'], hint)
        return OK, '按设备 ID 识别为 %s%s' % (sku, via)
    if exp['key'] == 'H100':
        if db['family'] == 'GH100?':
            return WARN, '设备 ID 属于 GH100 芯片，但%s' % note
        if db['family'] in ('H200', 'GH200'):
            return WARN, '实为 %s，不是 H100（规格高于 H100）' % sku
    return FAIL, '实为 %s，不是 %s%s' % (sku, exp['fam'], (' —— ' + note) if note else '')


CRITICAL = ('hwid', 'cfg', 'names', 'swid', 'smi_nvml', 'sm', 'bus', 'mem', 'arch', 'es')
ARCH_WORDS = ('pascal', 'volta', 'turing', 'ampere', 'ada', 'hopper', 'blackwell')


def evaluate_gpu(g, target):
    checks = []

    def add(key, level, value, detail='', crit=None):
        checks.append(dict(key=key, level=level, value=value, detail=detail,
                           crit=(key in CRITICAL) if crit is None else crit))

    p, db, smi, nv, pr, cu = g['pci'], g['db'], g['smi'], g['nvml'], g['proc'], g['cuda']
    devid = g['devid']
    keys = db_keys(db)
    mig = smi and smi.get('mig') == 'Enabled'

    srcs = [('nvidia-smi', smi and smi.get('name')), ('NVML', nv and nv.get('name')),
            ('驱动（/proc）', pr and pr.get('model')), ('CUDA', cu and cu.get('name'))]
    names = [(src, nm) for src, nm in srcs if nm]

    # 1. 硬件设备 ID（PCI 配置空间，芯片熔丝决定）
    if p is None:
        add('hwid', WARN, '-', '在 /sys 里找不到这张 GPU 的 PCI 设备，无法核对设备 ID')
    elif db is None:
        lvl, why = unknown_check(devid, names, target)
        add('hwid', lvl, '0x%04x' % devid, why)
    else:
        lvl, why = expect_check(db, target)
        add('hwid', lvl, '0x%04x' % devid, why)
    # 2. 实时配置空间 vs 内核缓存值
    cfg = g['cfg']
    if p and cfg:
        if cfg['vendor'] == 0xffff:
            add('cfg', WARN, '无响应', 'PCI 配置空间读出来全是 0xFFFF，GPU 可能已掉卡或正在复位')
        elif (cfg['vendor'], cfg['device']) != (p['vendor'], p['device']):
            add('cfg', FAIL, '不一致', '直接读 PCI 配置空间得到的设备 ID 是 0x%04x，与内核记录的 0x%04x 不一致，'
                '疑似在内核层面伪造' % (cfg['device'], p['device']))
    # 3. 名称一致性：nvidia-smi / NVML / 驱动 /proc / CUDA 都要与硬件 ID 同一型号家族
    if not names:
        add('names', WARN, '无数据', '驱动未加载，或 nvidia-smi、NVML 不可用：只核对了设备 ID，'
            '没能与驱动报告的型号和规格交叉比对')
    elif keys:
        bad = ['%s 报告的型号是“%s”' % (src, nm) for src, nm in names if model_key(nm) and model_key(nm) not in keys]
        unk = ['%s 报告的型号是“%s”' % (src, nm) for src, nm in names if not model_key(nm)]
        if bad:
            add('names', FAIL, '不一致', '按设备 ID 应为 %s，但名称对不上：%s，疑似 nvidia-smi 或驱动被篡改'
                % (db['sku'], '；'.join(bad)))
        elif unk:
            add('names', WARN, '无法识别', '认不出以下名称是什么型号，没法与设备 ID 对应的 %s 比对：%s'
                % (db['sku'], '；'.join(unk)))
        else:
            add('names', OK, '一致', '、'.join(src for src, _ in names) + ' 均为 ' + db['family'])
    else:
        add('names', INFO, '-', '名称：' + '；'.join('%s=%s' % x for x in names))
    for src, nm in names:
        if 'Graphics Device' in nm:
            add('es', WARN, nm, '%s 报告的名称是“%s”，说明驱动不认识这个设备，可能是工程样品' % (src, nm))
            break
    # 4. 软件层上报的 PCI ID 是否与硬件一致
    if p:
        hw_pair = (p['device'], p['vendor'])
        bad = []
        if smi and smi.get('pci_devid'):
            try:
                v = int(smi['pci_devid'], 16)
                if (v >> 16, v & 0xffff) != hw_pair:
                    bad.append('nvidia-smi 报告的设备 ID 是 0x%04x' % (v >> 16))
            except ValueError:
                pass
            if smi.get('pci_subsys') and p.get('sdevice') is not None:
                try:
                    v = int(smi['pci_subsys'], 16)
                    if v >> 16 != p['sdevice']:
                        bad.append('nvidia-smi 报告的子系统 ID 是 0x%04x（硬件是 0x%04x）' % (v >> 16, p['sdevice']))
                except ValueError:
                    pass
        if nv and nv.get('pci_devid') is not None:
            v = nv['pci_devid']
            if (v >> 16, v & 0xffff) != hw_pair:
                bad.append('NVML 报告的设备 ID 是 0x%04x' % (v >> 16))
        if bad:
            add('swid', FAIL, '不一致', '软件报告的 PCI ID 与硬件实际值不符（硬件设备 ID 是 0x%04x）：%s，疑似被篡改'
                % (p['device'], '；'.join(bad)))
    # 5. SM 数（NVML 核心数 ÷ 每 SM 核心数（由计算能力决定），或 --cuda 读到的 SM 数）
    sm, sm_src = None, None
    cps = cores_per_sm((nv and nv.get('cc')) or (db and db.get('cc')))
    if cu and cu.get('sm') and not mig:
        sm, sm_src = cu['sm'], 'CUDA'
    elif nv and nv.get('cores') and cps:
        sm, sm_src = nv['cores'] // cps, 'NVML'
    exp = db and db.get('sms')
    if sm is None:
        add('sm', INFO, '-', '驱动没有提供 SM 数（可以加 --cuda 读取）')
    elif not exp:
        add('sm', INFO, str(sm), '')
    elif sm == exp:
        add('sm', OK, str(sm), '')
    else:
        add('sm', FAIL if db['strict'] else WARN, str(sm),
            'SM 数为 %d（来自 %s），%s 的标准值是 %d' % (sm, sm_src, db['sku'], exp))
    # 6. 显存位宽
    bw = (nv and nv.get('bus_width')) or (cu and cu.get('bus_width'))
    exp = db and db.get('bus')
    if not bw:
        add('bus', INFO, '-', '')
    elif not exp or bw == exp:
        add('bus', OK if exp else INFO, str(bw), '')
    else:
        add('bus', FAIL if db['strict'] else WARN, str(bw),
            '显存位宽为 %d-bit，%s 的标准值是 %d-bit' % (bw, db['sku'], exp))
    # 7. 显存容量（优先取 NVML 直连值：nvidia-smi 程序本身可能被替换）
    mib_nv = nv['mem_total'] / 1048576.0 if (nv and nv.get('mem_total')) else None
    mib_smi = smi and smi.get('mem_total')
    mib = mib_nv or mib_smi
    exp = db and db.get('mem')
    exps = exp if isinstance(exp, tuple) else ((exp,) if exp else ())
    if not mib:
        add('mem', INFO, '-', '')
    else:
        gib = mib / 1024.0
        val = '%.1fGiB' % gib
        std = '/'.join(str(e) for e in exps)
        # GDDR 显存开 ECC 时要占掉 1/16 容量，两种值都算标准
        if not exps:
            add('mem', INFO, val, '')
        elif any(abs(gib - e) / float(e) <= 0.06 or abs(gib - e * 15 / 16.0) / (e * 15 / 16.0) <= 0.03 for e in exps):
            add('mem', OK, val, '')
        elif db['tier'] != 'dc' and gib > max(exps) * 1.2:
            g['memmod'] = int(round(gib))
            add('mem', WARN, val, '显存 %.1f GiB，比 %s 的官方规格 %s GB 大，疑似改装大显存（俗称魔改卡）：'
                'GPU 芯片是真的，但板卡不是原厂设计，要留意稳定性和保修' % (gib, db['sku'], std), crit=False)
        else:
            add('mem', FAIL if db['strict'] else WARN, val,
                '显存 %.1f GiB，与 %s 的标称容量 %s GB 不符' % (gib, db['sku'], std))
    # 8. 最大功耗墙（VBIOS 决定）
    p_nv, p_smi = nv and nv.get('power_max'), smi and smi.get('power_max')
    pmax = p_nv or p_smi
    exp = db and db.get('maxw')
    exps = exp if isinstance(exp, tuple) else ((exp,) if exp else ())
    if not pmax:
        add('power', INFO, '-', '')
    elif not exps or any(abs(pmax - e) <= e * 0.03 for e in exps):
        add('power', OK if exps else INFO, '%dW' % pmax, '')
    else:
        add('power', WARN, '%dW' % pmax, '最大功耗墙为 %d W，%s 的标准值是 %s W（可能刷过非原厂 VBIOS，或者型号不对）'
            % (pmax, db['sku'], '/'.join(str(e) for e in exps)))
    # 9. 最大频率
    msm = (nv and nv.get('max_sm')) or (smi and smi.get('max_sm'))
    mmem = (nv and nv.get('max_mem')) or (smi and smi.get('max_mem'))
    bad = []
    if db and db.get('maxsm') and msm and abs(msm - db['maxsm']) > db['maxsm'] * 0.02:
        bad.append('SM %d MHz（标准值 %d MHz）' % (msm, db['maxsm']))
    if db and db.get('maxmem') and mmem and abs(mmem - db['maxmem']) > db['maxmem'] * 0.02:
        bad.append('显存 %d MHz（标准值 %d MHz）' % (mmem, db['maxmem']))
    clk = '%s/%s' % (s(msm and int(msm)), s(mmem and int(mmem))) if (msm or mmem) else '-'
    if bad:
        add('clock', WARN, clk, '最大频率与 %s 的标准值不符：%s' % (db['sku'], '，'.join(bad)))
    else:
        add('clock', OK if (db and db.get('maxsm') and msm) else INFO, clk, '')
    # nvidia-smi 与 NVML 直连的数值必须一致（同一份驱动数据，差异只能来自篡改）
    diffs = []
    if mib_nv and mib_smi and abs(mib_nv - mib_smi) > 0.03 * mib_nv:
        diffs.append('显存（nvidia-smi %d MiB，NVML %d MiB）' % (mib_smi, mib_nv))
    if p_nv and p_smi and abs(p_nv - p_smi) > 0.02 * p_nv:
        diffs.append('最大功耗墙（nvidia-smi %d W，NVML %d W）' % (p_smi, p_nv))
    for key, label in (('max_sm', '最大 SM 频率'), ('max_mem', '最大显存频率')):
        a, b = smi and smi.get(key), nv and nv.get(key)
        if a and b and abs(a - b) > 0.02 * b:
            diffs.append('%s（nvidia-smi %d MHz，NVML %d MHz）' % (label, a, b))
    if smi and nv and smi.get('serial') and nv.get('serial') and smi['serial'] != nv['serial']:
        diffs.append('序列号（nvidia-smi %s，NVML %s）' % (smi['serial'], nv['serial']))
    if diffs:
        add('smi_nvml', FAIL, '不一致', 'nvidia-smi 与直接调用 NVML 读到的数据不一致：%s。两者读的是同一份驱动数据，'
            '本应相同，nvidia-smi 的输出疑似被篡改' % '；'.join(diffs))
    # 10. NVLink 链路
    nl = g.get('nvl')
    exp = db and db.get('links')
    if not nl:
        no_drv = not smi and not nv
        add('nvlink', WARN if (exp and not no_drv) else INFO, '-', '' if no_drv else ('无法读取 NVLink 状态' if exp else ''))
    else:
        val = '%d/%d' % (nl['active'], nl['total'])
        if exp and nl['total'] < exp:
            add('nvlink', WARN, val, '只有 %d 条 NVLink 链路（%s 应为 %d 条）' % (nl['total'], db['sku'], exp))
        elif nl['inactive'] and nl['active'] == 0:
            add('nvlink', WARN, val, 'NVLink 链路全部未激活（有 NVSwitch 的机器请检查 nvidia-fabricmanager 服务）')
        elif nl['inactive']:
            add('nvlink', WARN, val, 'NVLink 链路 %s 未激活（这是硬件健康问题，与真伪无关）'
                % ','.join(str(x) for x in nl['inactive']))
        else:
            add('nvlink', OK if exp else INFO, val, '')
    # 11. GPU 部件号前缀应等于设备 ID（如 2330-885-A1）
    pn = smi and smi.get('gpu_pn')
    if pn and devid is not None and re.match(r'^[0-9A-Fa-f]{4}-', pn):
        if pn[:4].lower() != '%04x' % devid:
            add('partno', WARN, pn, 'GPU 部件号 %s 的前 4 位应与设备 ID（0x%04x）相同，实际对不上' % (pn, devid))
        else:
            add('partno', OK, pn, '')
    # 12. 架构 / 计算能力（主版本号）
    if db and db.get('arch'):
        bad = []
        sa = (smi and smi.get('arch') or '').strip()
        w = sa.split()[0].lower() if sa else ''
        if w in ARCH_WORDS and w != db['arch'].split()[0].lower():
            bad.append('nvidia-smi 报告的架构是 %s' % sa)
        want = (db.get('cc') or '').split('.')[0]
        for src, rec in (('NVML', nv), ('CUDA', cu)):
            if want and rec and rec.get('cc') and rec['cc'].split('.')[0] != want:
                bad.append('%s 报告的计算能力（compute capability）是 %s' % (src, rec['cc']))
        if bad:
            add('arch', FAIL, '不一致', '芯片是 %s 架构，但 %s，疑似被篡改' % (db['arch'], '；'.join(bad)))

    g['checks'] = checks
    by = dict((c['key'], c) for c in checks)
    worst_crit = max([c['level'] for c in checks if c['crit']] or [OK])
    worst_all = max([c['level'] for c in checks] or [OK])
    if db:
        short = db['sku'].rsplit(' ', 1)[0] if re.search(r'\d+GB$', db['sku']) else db['sku']
        actual = '实为 ' + short
    else:       # 型号库不认识：只有驱动报告的名称，不能说“实为”
        short = re.sub(r'^NVIDIA\s+(GeForce\s+)?', '', names[0][1]) if names else '?'
        actual = '驱动名称为 ' + short
    hw_lvl = by.get('hwid', {}).get('level')
    other = target is not None and target['key'] not in keys     # 识别出的型号与期望不同
    if hw_lvl == FAIL:
        g['verdict'] = (FAIL, ('非 %s，' % target['fam'] if other else '形态不符，') + actual)
    elif worst_crit == FAIL:
        g['verdict'] = (FAIL, '存疑，数据互相矛盾')
    elif worst_crit == WARN:
        g['verdict'] = (WARN, '非 %s，%s' % (target['fam'], actual) if (hw_lvl == WARN and other and keys) else '待确认')
    elif g.get('memmod'):
        g['verdict'] = (WARN, '真 %s（改装 %dGB 显存）' % (short, g['memmod']))
    elif worst_all >= WARN:
        g['verdict'] = (WARN, '真 %s（有告警）' % short)
    else:
        g['verdict'] = (OK, '真 ' + short)
    g['auth'] = OK if g['verdict'][1].startswith('真 ') else g['verdict'][0]
    return g


def evaluate(ctx, args):
    F = Findings.add
    gpus = ctx['gpus']
    pci = ctx['pci']
    for g in gpus:
        try:
            evaluate_gpu(g, args.exp)
        except Exception as e:
            ERRORS.append('GPU %s 判定：%s: %s' % (g.get('idx'), type(e).__name__, e))
            g.setdefault('checks', [dict(key='hwid', level=WARN, value='-', detail='判定出错')])
            g['verdict'] = (WARN, '检测出错')
            g['auth'] = WARN

    # ---------------- 真伪
    if not gpus:
        F('真伪', FAIL, '未发现任何 NVIDIA GPU')
    groups = {}
    for g in gpus:
        for c in g['checks']:
            if c['level'] >= WARN:
                cat = '真伪' if c['key'] not in ('nvlink',) else 'NVLink'
                groups.setdefault((cat, c['level'], c['detail']), []).append(g['idx'])
    for (cat, lvl, detail), ids in sorted(groups.items(), key=lambda x: (-x[0][1], x[0][0])):
        F(cat, lvl, 'GPU %s：%s' % (compress_ids(ids), detail))
    verdicts = Counter(g['verdict'][1] for g in gpus)
    ctx['verdicts'] = verdicts

    # ---------------- 驱动软件（nvidia-smi / NVML / 驱动各组件是否被替换、篡改或劫持）
    drv, smi, nv = ctx['drv'], ctx['smi'], ctx['nvml']
    files = ctx['smi_files']
    if not files:
        if any(is_nv_gpu(p) for p in pci.values()):
            F('驱动软件', WARN, '未找到 nvidia-smi（驱动没装？），只能核对设备 ID')
    else:
        f0 = files[0]
        if G.test_mode:
            pass
        elif f0.get('elf') is False:
            F('驱动软件', WARN, '%s 不是 ELF 二进制文件（实际是%s），可能被包装或替换过，请人工检查。文件开头几行：%s'
              % (f0['path'], '脚本' if f0.get('script') else '未知格式', clip(f0.get('head') or '-', 120)))
        if f0.get('pkg_ok') is False:
            run_ver = drv.get('kmod_version') or smi.get('driver')
            pv = f0.get('pkg_ver') or ''
            if run_ver and pv and not pv.split(':')[-1].startswith(run_ver):
                F('驱动软件', WARN, 'nvidia-smi 与软件包 %s（版本 %s）里的原始文件不一致，但当前运行的驱动是 %s，'
                  '与软件包版本不同：多半是用 .run 安装包覆盖了包管理器装的驱动，不一定是篡改，建议人工确认（%s）'
                  % (f0['pkg'], pv, run_ver, f0['path']))
            else:
                F('驱动软件', FAIL, 'nvidia-smi %s；而软件包版本与当前驱动相同，说明文件在安装后被修改过（%s）'
                  % (f0['pkg_msg'], f0['path']))
        if len(files) > 1:
            F('驱动软件', WARN, 'PATH 里有多个 nvidia-smi：%s，请确认实际运行的是哪一个、来源是否可靠'
              % '、'.join(f['path'] for f in files))
    if files and not smi['ok']:
        F('驱动软件', WARN, 'nvidia-smi 执行失败：%s' % smi['error'])
    vers = {}
    if drv.get('kmod_version'):
        vers['内核模块'] = drv['kmod_version']
    if smi.get('driver'):
        vers['nvidia-smi'] = smi['driver']
    if nv.get('driver'):
        vers['NVML'] = nv['driver']
    m = re.search(r'\.so\.(\d+\.\d+(?:\.\d+)?)$', os.path.realpath(nv['lib'])) if nv.get('lib') else None
    if m:
        vers['libnvidia-ml'] = m.group(1)
    ctx['versions'] = vers
    kv_ = vers.get('内核模块')
    forged = [k for k in ('nvidia-smi', 'NVML') if kv_ and vers.get(k) and vers[k] != kv_]
    if forged:
        F('驱动软件', FAIL, '驱动各组件版本不一致：%s。%s 报告的版本与已加载的内核模块不同，却还能正常工作'
          '（正常情况下版本不匹配会直接报错），输出疑似被伪造'
          % ('，'.join('%s=%s' % x for x in vers.items()), '、'.join(forged)))
    elif len(set(vers.values())) > 1:
        F('驱动软件', WARN, '驱动各组件版本不一致：%s（升级驱动后还没重启？）' % '，'.join('%s=%s' % x for x in vers.items()))
    if smi['ok'] and nv['ok'] and len(nv['gpus']) != len(smi['gpus']):
        F('驱动软件', WARN, 'nvidia-smi 看到 %d 张 GPU，直接调用 NVML 看到 %d 张' % (len(smi['gpus']), len(nv['gpus'])))
    if drv.get('preload_file'):
        F('驱动软件', WARN, '/etc/ld.so.preload 里配置了预加载库：%s。它会被注入到所有程序中，可以劫持 NVML 调用，请确认来源'
          % '、'.join(drv['preload_file']))
    if drv.get('preload_env'):
        F('驱动软件', WARN, '当前环境设置了 LD_PRELOAD=%s（本脚本调用外部命令时已将它去掉）' % drv['preload_env'])
    if smi['ok'] and not nv['ok']:
        F('驱动软件', WARN, '无法直接调用 NVML（%s），没法与 nvidia-smi 的输出交叉核对' % nv['error'])
    if args.cuda and ctx.get('cuda') and not ctx['cuda']['ok']:
        F('驱动软件', WARN if smi['ok'] else INFO, 'CUDA 属性查询失败：%s' % ctx['cuda']['error'])

    # ---------------- 系统：GPU 数量、驱动绑定
    hw = [p for p in pci.values() if is_nv_gpu(p)]
    unbound = [p['bdf'] for p in hw if not p.get('driver')]
    vfio = [p['bdf'] for p in hw if p.get('driver') in ('vfio-pci', 'pci-stub')]
    if unbound:
        F('系统', WARN, '%d 张 GPU 未绑定任何驱动：%s' % (len(unbound), ', '.join(short_bdf(b) for b in unbound)))
    if vfio:
        F('系统', INFO, '%d 张 GPU 绑定在 vfio-pci 上（直通给虚拟机用），只能核对设备 ID' % len(vfio))
    on_nv = [p for p in hw if p.get('driver') == 'nvidia']
    if smi['ok'] and on_nv and len(smi['gpus']) != len(on_nv):
        F('驱动软件', WARN, '绑定了 nvidia 驱动的 GPU 有 %d 张，nvidia-smi 只看到 %d 张%s'
          % (len(on_nv), len(smi['gpus']), '（在容器内运行时属正常）' if ctx['sys']['container'] else ''))
    nvsw = [p for p in pci.values() if is_nvswitch(p)]
    sxm = [g for g in gpus if g['db'] and g['db']['form'].startswith('SXM')]
    exp_n = args.expect_gpus or (8 if (len(nvsw) >= 4 and sxm) else None)
    if exp_n and len(hw or gpus) != exp_n:
        F('系统', WARN, '看到 %d 张 GPU，期望 %d 张（缺卡或掉卡？）' % (len(hw or gpus), exp_n))
    vb = set(g['smi']['vbios'] for g in gpus if g['smi'] and g['smi'].get('vbios'))
    if len(vb) > 1:
        F('系统', INFO, 'GPU 之间 VBIOS 版本不一致：%s' % '、'.join(sorted(vb)))
    if ctx['sys']['virt'] not in (None, 'none'):
        F('系统', INFO, '运行在虚拟机里（%s）：看到的 PCI ID 由虚拟化层提供，判定主要依据驱动和 NVML 之间的交叉核对'
          % ctx['sys']['virt'])

    # ---------------- NVLink / NVSwitch / Fabric Manager
    fm = ctx['fm']
    if nvsw and sxm:
        if fm.get('active') != 'active':
            F('NVLink', WARN, '本机有 NVSwitch，但 nvidia-fabricmanager 未运行（状态：%s），GPU 之间的 NVLink 通信和 CUDA '
              '都可能用不了' % (fm.get('active') or ('未安装' if fm.get('load') == 'not-found' else '未知')))
        bad = [g['idx'] for g in gpus if g['smi'] and g['smi'].get('fabric_state')
               and (g['smi']['fabric_state'] != 'Completed' or (g['smi'].get('fabric_status') or 'Success') != 'Success')]
        if bad:
            F('NVLink', WARN, 'GPU %s 加入 NVLink 网络（Fabric）未完成或失败' % compress_ids(bad))
        if fm.get('version') and drv.get('kmod_version') and not fm['version'].startswith(drv['kmod_version']):
            F('NVLink', WARN, 'Fabric Manager 版本 %s 与驱动版本 %s 不一致（两者必须一致）' % (fm['version'], drv['kmod_version']))
        if len(sxm) == 8 and len(nvsw) not in (0, 4) and any(g['db']['family'] == 'H100' for g in sxm):
            F('NVLink', WARN, 'HGX H100 8 卡应有 4 颗 NVSwitch，实际看到 %d 颗' % len(nvsw))
    topo = ctx['topo']
    if topo['ok'] and sxm and nvsw:     # 无 NVSwitch 的机型（如 DGX-1 混合立方网格、4 卡直连）本来就不是两两全互联
        labels = ['GPU%d' % g['idx'] for g in sxm if g['idx'] is not None]
        want = sxm[0]['db'].get('links')
        badp = []
        for a in labels:
            for b in labels:
                if a < b:
                    v = topo['rows'].get(a, {}).get(b)
                    if v and (not v.startswith('NV') or (want and v != 'NV%d' % want)):
                        badp.append('%s-%s=%s' % (a, b, v))
        if badp:
            should = 'NV%d（%d 条 NVLink）' % (want, want) if want else 'NVLink'
            F('NVLink', WARN, 'GPU 之间应全部通过 %s 互联，以下 GPU 对不是：%s%s'
              % (should, '，'.join(badp[:6]), '，共 %d 对' % len(badp) if len(badp) > 6 else ''))
    elif sxm and not topo['ok'] and smi['ok']:
        F('NVLink', WARN, '无法获取 GPU 互联拓扑（nvidia-smi topo -m）：%s' % s(topo.get('error')))
    errs = [(g['idx'], g['nvl']['errors']) for g in gpus if g.get('nvl') and g['nvl'].get('errors')]
    if errs:
        F('NVLink', INFO, 'NVLink 错误计数不为 0：%s（这是累计值，建议隔一段时间再看是否还在增长）'
          % '，'.join('GPU%s=%d' % e for e in errs))
    if ctx['xid'].get('sxid'):
        F('NVLink', WARN, '内核日志里有 %d 条 NVSwitch 错误（SXid）' % ctx['xid']['sxid'])

    # ---------------- IB / RDMA
    hcas = [h for h in ctx['ib'] if not h.get('is_vf')]
    if not hcas:
        F('IB/RDMA', INFO, '未发现 IB/RDMA 网卡')
    rates = Counter()
    for h in hcas:
        for pt in h['ports']:
            name = '%s/%s' % (h['dev'], pt['port'])
            if pt['state'] != 'ACTIVE':
                why = ('物理链路已通，但端口没有激活：请检查 IB 网络的 Subnet Manager（如 opensm、UFM）'
                       if pt['phys'] == 'LinkUp' else '没接线、对端没开或链路故障')
                F('IB/RDMA', WARN, '%s 端口状态 %s / %s（%s）' % (name, s(pt['state']), s(pt['phys']), why))
            elif pt['link_layer'] == 'InfiniBand':
                rates[(pt['rate'] or '').split('(')[0].strip()] += 1
            c = pt['counters']
            if c.get('link_downed') or c.get('symbol_error') or c.get('port_rcv_errors'):
                F('IB/RDMA', INFO, '%s 错误计数：link_downed=%s symbol_error=%s rcv_errors=%s'
                  '（这是累计值，建议隔一段时间再看是否还在增长）'
                  % (name, c.get('link_downed', 0), c.get('symbol_error', 0), c.get('port_rcv_errors', 0)))
        if h['cur_gen'] and h['max_gen'] and (h['cur_gen'] < h['max_gen'] or
                                              (h['cur_w'] and h['max_w'] and h['cur_w'] != h['max_w'])):
            F('PCIe', WARN, '%s（%s）PCIe 降级运行：当前 %s，最高支持 %s'
              % (h['dev'], h['model'], h['link'], h['link_max']))
    if len(rates) > 1:
        F('IB/RDMA', WARN, 'IB 端口速率不一致：%s' % '，'.join('%s x%d' % kv for kv in rates.most_common()))
    pro = [g for g in gpus if not (g['db'] and g['db']['tier'] == 'gf')]    # GeForce 不支持 GPUDirect RDMA
    if hcas and pro and not ({'nvidia_peermem', 'nv_peer_mem'} & ctx['modules']):
        F('IB/RDMA', INFO, '未加载 nvidia_peermem 内核模块：GPUDirect RDMA（网卡直接读写 GPU 显存）需要它；'
          '如果用的是 DMA-BUF 方式，可以忽略')

    # ---------------- PCIe（GPU）
    for g in gpus:
        p = g['pci']
        if not p:
            continue
        mw, cw = p.get('max_width'), p.get('cur_width')
        if mw and mw.isdigit() and int(mw) < 16 and g['db'] and g['db']['arch'] == 'Hopper':
            F('PCIe', WARN, 'GPU %s 的 PCIe 最多只支持 x%s（Hopper 应为 x16）' % (g['idx'], mw))
        elif mw and cw and cw.isdigit() and mw.isdigit() and int(cw) < int(mw):
            F('PCIe', WARN, 'GPU %s 的 PCIe 链路宽度降级：当前 x%s，最高支持 x%s' % (g['idx'], cw, mw))
        mg = pcie_gen(p.get('max_speed'))
        if mg and g['db'] and g['db']['arch'] == 'Hopper' and mg < 5:
            F('PCIe', WARN, 'GPU %s 的 PCIe 最高只支持 Gen%d（Hopper 应为 Gen5）' % (g['idx'], mg))

    # ---------------- GPU 健康（只读快照）
    xid_by = {}
    for bdf3, code in ctx['xid'].get('xid', []):
        xid_by.setdefault(bdf3, []).append(code)
    ctx['xid_by'] = xid_by
    hb = {}     # 同样的问题出现在多张卡上时合并成一条：(级别, 文案) → [GPU 编号]

    def H(level, i, msg):   # msg 里的 {G} 代表 GPU 编号
        hb.setdefault((level, msg), []).append(i)

    for g in gpus:
        d, i = g['smi'], g['idx']
        if not d:
            continue
        if d.get('ecc') == 'Disabled' and g['db'] and g['db']['tier'] == 'dc':
            H(WARN, i, '{G} 的 ECC 已关闭（数据中心卡默认开启；关闭后，显存出现比特翻转既发现不了也纠正不了）')
        if d.get('ecc_vol_unc'):
            H(WARN, i, '{G} 驱动本次加载以来出现了 %d 个不可纠正 ECC 错误' % d['ecc_vol_unc'])
        if d.get('ecc_agg_unc'):
            H(INFO, i, '{G} 历史上累计出现过 %d 个不可纠正 ECC 错误' % d['ecc_agg_unc'])
        if d.get('rr_failure') == 'Yes':
            H(FAIL, i, '{G} 显存行重映射（row remapping）失败，说明显存有故障，建议报修')
        if d.get('rr_pending') == 'Yes':
            H(WARN, i, '{G} 有待生效的行重映射（row remapping），需要在维护窗口复位 GPU 才能生效')
        if d.get('reset_required') == 'Yes' or d.get('drain_reset') == 'Yes':
            H(WARN, i, '{G} 驱动提示需要复位 GPU')
        if d.get('recovery') and d['recovery'] not in ('None', 'GPU_RECOVERY_ACTION_NONE'):
            H(WARN, i, '{G} 驱动建议的恢复操作：%s' % d['recovery'])
        if d.get('temp') and d['temp'] >= 85:
            H(WARN, i, '{G} 核心温度 %d°C 偏高' % d['temp'])
        if d.get('mem_temp') and d['mem_temp'] >= 90:
            H(WARN, i, '{G} 显存温度 %d°C 偏高' % d['mem_temp'])
        badthr = [THROTTLE_CN.get(t, t) for t in d.get('throttle', []) if t in THROTTLE_BAD]
        if badthr:
            H(WARN, i, '{G} 正在降频：%s' % '、'.join(badthr))
        codes = xid_by.get(g['bdf'][:-2], [])
        hard = sorted(set(c for c in codes if c not in XID_APP))
        if hard:
            H(WARN, i, '{G} 内核日志里有 Xid %s' % '、'.join(
                '%d（%s）' % (c, XID_DESC.get(c, '含义见 NVIDIA 的 Xid 文档')) for c in hard))
        elif codes:
            H(INFO, i, '{G} 内核日志里有 Xid %s（这类错误多由应用程序引起，一般不是硬件问题）'
              % '、'.join(str(c) for c in sorted(set(codes))))
        if g['cfg'] and g['cfg']['vendor'] == 0xffff:
            H(FAIL, i, '{G} 的 PCI 配置空间没有响应（掉卡了？）')
    for (lvl, msg), ids in hb.items():
        F('GPU 健康', lvl, msg.replace('{G}', 'GPU ' + compress_ids(ids)))
    if not ctx['xid']['ok']:
        F('GPU 健康', INFO, '没有检查 Xid：%s' % ctx['xid']['error'])


# ============================================================================
# 报告输出
# ============================================================================

def lv_cell(level, text):
    return lvl_color(level, text)


def render(ctx, args, elapsed):
    rep = Report()
    st = ctx['sys']
    W = 108
    title, ver = '  GPU 服务器检测报告', 'gpu_check v' + VERSION
    rep.add(bold('=' * W))
    rep.add(bold(title) + ' ' * max(1, W - dwidth(title) - len(ver)) + dim(ver))
    rep.add(bold('=' * W))
    rep.add('  主机: %s    时间: %s    用户: %s    耗时: %.1fs'
            % (st['hostname'], time.strftime('%Y-%m-%d %H:%M:%S %Z'), 'root' if G.is_root else '非 root', elapsed))
    rep.add('  模式: 只读（不创建 CUDA 上下文、不占显存、不跑负载、不改任何配置），以最低优先级运行（nice 19 + ionice idle）'
            + ('；已启用 --cuda，额外读取 CUDA 设备属性（不创建上下文）' if args.cuda else ''))
    exp = args.exp
    if exp is None:
        goal = '不限型号，只检查各数据源是否一致、规格是否与识别出的型号吻合'
    elif exp['form']:
        goal = exp['label'] + ('，即 NVLink 版' if exp['form'] == 'SXM' else ' 版')
    else:
        goal = exp['label'] + ('，SXM5 / PCIe / NVL 任意形态均可' if exp['key'] == 'H100' else '')
    rep.add('  核验目标: %s（--expect %s%s）' % (goal, args.expect, '，默认值' if args.expect == DEFAULT_EXPECT else ''))
    if not G.is_root:
        rep.add('  ' + yellow('提示: 当前不是以 root 运行，内存条型号、整机序列号、网卡料号、内核日志（Xid）等信息可能读不到'))

    for fn, a in ((render_system, ()), (render_hw, ()), (render_gpu_list, ()), (render_authenticity, (args,)),
                  (render_nvlink, ()), (render_ib, ()), (render_eth, ()), (render_health, ())):
        try:
            fn(rep, ctx, *a)
        except Exception as e:
            ERRORS.append('报告 %s：%s: %s' % (fn.__name__, type(e).__name__, e))
            rep.note('（本节输出出错，已跳过：%s）' % e)
    if ERRORS:
        rep.section('附：脚本自身出错（不影响其它检测项，请反馈给脚本维护者）')
        for e in ERRORS:
            rep.add('  - ' + e)
    render_conclusion(rep, ctx, args)
    return rep


FM_STATE = {'active': '运行中', 'inactive': '未运行', 'failed': '启动失败', 'activating': '正在启动',
            'deactivating': '正在停止'}


def fm_text(fm):
    """Fabric Manager 服务状态，如“未运行（inactive / dead），版本 550.54.15-1”"""
    if fm.get('load') == 'not-found' or (not fm.get('active') and not fm.get('version')):
        return '未安装'
    raw = ' / '.join(x for x in (fm.get('active'), fm.get('sub')) if x)
    return '%s%s%s' % (FM_STATE.get(fm.get('active'), '状态未知'), '（%s）' % raw if raw else '',
                       '，版本 %s' % fm['version'] if fm.get('version') else '')


def render_system(rep, ctx):
    st, drv, smi, fm, ofed = ctx['sys'], ctx['drv'], ctx['smi'], ctx['fm'], ctx['ofed']
    rep.section('一、系统概览')
    need_root = '(需 root 权限)' if not G.is_root else '-'
    gsp = next((g['smi']['gsp'] for g in ctx['gpus'] if g['smi'] and g['smi'].get('gsp')), None)
    if drv.get('kmod_version'):
        kmod = '%s（%s）' % (drv['kmod_version'], '开源内核模块' if drv.get('kmod_open') else '闭源内核模块')
    else:
        kmod = red('未加载 NVIDIA 驱动') if any(is_nv_gpu(p) for p in ctx['pci'].values()) else '-'
    virt = st['virt'] if st['virt'] not in (None, 'none') else '物理机'
    if st['container']:
        virt += '（容器内）'
    rep.kv([
        ('主机名', st['hostname']),
        ('服务器型号', ' '.join(x for x in (st['sys_vendor'], st['product_name']) if x) or '-'),
        ('整机序列号', st['product_serial'] or need_root),
        ('主板', ' '.join(x for x in (st['board_vendor'], st['board_name']) if x) or '-'),
        ('BIOS', ' '.join(x for x in (st['bios_vendor'], st['bios_version'], st['bios_date']) if x) or '-'),
        ('操作系统', s(st['os'])),
        ('内核', '%s (%s)' % (s(st['kernel']), st['arch'])),
        ('开机时长', s(st['uptime'])),
        ('运行环境', virt),
        ('NVIDIA 驱动', kmod),
        ('驱动支持的 CUDA 版本', s(smi.get('cuda'))),
        ('GSP 固件', s(gsp)),
        ('Fabric Manager', fm_text(fm)),
        ('OFED / 网卡驱动', ofed.get('ofed') or ('内核自带 mlx5 驱动' if ctx['ib'] else '-')),
    ])


def render_hw(rep, ctx):
    cpu, mem, disks = ctx['cpu'], ctx['mem'], ctx['disks']
    rep.section('二、CPU / 内存 / 存储')
    dimms = mem.get('dimms')
    if dimms is not None:
        pop = [d for d in dimms if _size_gb(d.get('Size')) not in (None, 0)]
        dimm_txt = '已插 %d 条，共 %d 个槽位' % (len(pop), len(dimms))
    else:
        pop = []
        dimm_txt = '（%s%s）' % (mem.get('dimm_note') or '无数据',
                               '；容器内一般读不到 SMBIOS，请在宿主机上运行' if ctx['sys'].get('container') else '')
    rep.kv([
        ('CPU 型号', s(cpu['model'])),
        ('路数 / 核数 / 线程数', '%s 路 / %s 核 / %s 线程' % (s(cpu['sockets']), s(cpu['cores']), s(cpu['threads']))),
        ('NUMA 节点', s(cpu['numa'])),
        ('内存总量', '%.0f GiB（操作系统可见）' % mem['total_gib'] if mem['total_gib'] else '-'),
        ('内存条', dimm_txt),
    ])
    if pop:
        grp = Counter()
        for d in pop:
            speed = d.get('Speed', '-')
            cfgs = d.get('Configured Memory Speed') or d.get('Configured Clock Speed')
            if cfgs and cfgs != speed:
                speed = '%s（实际运行 %s）' % (speed, cfgs)
            grp[(d.get('Size', '-'), d.get('Type', '-'), speed, d.get('Manufacturer', '-'),
                 d.get('Part Number', '-').strip())] += 1
        rep.table(['数量', '单条容量', '类型', '速率', '厂商', '型号'],
                  [[str(n)] + list(k) for k, n in grp.most_common()], aligns='r')
    if disks:
        grp = Counter()
        for d in disks:
            try:
                size = '%.2f TB' % (int(d.get('SIZE', '0')) / 1e12) if int(d.get('SIZE', '0')) >= 1e12 \
                    else '%.0f GB' % (int(d.get('SIZE', '0')) / 1e9)
            except ValueError:
                size = d.get('SIZE', '-')
            kind = 'HDD' if d.get('ROTA') == '1' else 'SSD'
            grp[(d.get('MODEL', '').strip() or '-', size, (d.get('TRAN') or '-').upper(), kind)] += 1
        names = {}
        for d in disks:
            names.setdefault((d.get('MODEL', '').strip() or '-'), []).append(d.get('NAME'))
        rows = []
        for (model, size, tran, kind), n in grp.most_common():
            devs = names.get(model, [])
            rows.append([str(n), model, size, tran, kind, clip(','.join(devs), 30)])
        rep.table(['数量', '磁盘型号', '容量', '接口', '类型', '设备'], rows, aligns='r')


def render_gpu_list(rep, ctx):
    gpus = ctx['gpus']
    rep.section('三、GPU 清单')
    if not gpus:
        rep.note('未发现 NVIDIA GPU')
        return
    rows = []
    for g in gpus:
        p, d = g['pci'] or {}, g['smi'] or {}
        name = d.get('name') or (g['nvml'] or {}).get('name') or (g['db']['sku'] if g['db'] else '-')
        mem = '%d MiB' % d['mem_total'] if d.get('mem_total') else '-'
        pw = '%s/%sW' % (iv(d.get('power_limit')), iv(d.get('power_max'))) \
            if d.get('power_max') else '-'
        clk = '%s/%s' % (iv(d.get('max_sm')), iv(d.get('max_mem'))) \
            if d.get('max_sm') else '-'
        rows.append([s(g['idx']), short_bdf(g['bdf']), clip(name, 26), mem, pw, clk,
                     fmt_link(p.get('max_speed'), p.get('max_width')), s(p.get('numa') if p.get('numa', -1) >= 0 else None),
                     s(d.get('vbios')), s(d.get('serial')), s(d.get('module_id')),
                     s(p.get('driver'), red('无驱动') if p else '-')])
    rep.table(['GPU', 'PCI 地址', '型号(nvidia-smi)', '显存', '功耗墙 当前/最大', '最大频率 SM/显存(MHz)', 'PCIe 最高规格',
               'NUMA', 'VBIOS', '序列号', '模组 ID', '内核驱动'], rows, aligns='r')
    rows = []
    for g in gpus:
        d = g['smi'] or {}
        rows.append([s(g['idx']), s(d.get('board_pn')), s(d.get('gpu_pn')), s(d.get('inforom')), s(d.get('uuid')),
                     s(d.get('persistence')), s(d.get('mig')), s(d.get('ecc'))])
    if any(g['smi'] for g in gpus):
        rep.table(['GPU', '板卡料号', 'GPU 部件号', 'InfoROM', 'UUID', '持久模式', 'MIG', 'ECC'], rows, aligns='r')
    rep.note('序列号、模组 ID 可以和 GPU 模组上的标签、供应商的装箱单逐一核对。')


def render_authenticity(rep, ctx, args):
    gpus = ctx['gpus']
    rep.section('四、GPU 型号与真伪核验' if args.exp is None else '四、%s 真伪核验' % args.exp['label'])
    rows = []
    for g in gpus:
        by = dict((c['key'], c) for c in g['checks'])

        def cell(key, default='-'):
            c = by.get(key)
            if not c:
                return default
            v = c['value'] if c['value'] not in (None, '') else '-'
            return lv_cell(c['level'], v) if c['level'] != INFO else v

        sku = g['db']['sku'] if g['db'] else ('未知' if g['devid'] is not None else '-')
        lvl, txt = g['verdict']
        rows.append([s(g['idx']), short_bdf(g['bdf']), cell('hwid'), lv_cell(by['hwid']['level'], sku),
                     cell('names'), cell('sm'), cell('bus'), cell('mem'), cell('power'), cell('clock'),
                     cell('nvlink'), lv_cell(lvl, txt)])
    if rows:
        rep.table(['GPU', 'PCI 地址', '设备 ID', '实际型号', '名称核对', 'SM 数', '显存位宽', '显存', '最大功耗墙',
                   '最大频率', 'NVLink', '结论'], rows, aligns='r')
        rep.note('“实际型号”按设备 ID 识别；“名称核对”看驱动、NVML、nvidia-smi 等报告的型号名是否都与实际型号一致。')
    via = [g for g in gpus if g['db'] and g['db'].get('src') == 'pci.ids']
    if via:
        rep.note('GPU %s 的设备 ID（%s）不在内置型号库中，型号是按本机 pci.ids 识别的，并与驱动、NVML、nvidia-smi '
                 '报告的名称核对过；但没有标准规格可以比对，可信度不如内置型号。'
                 % (compress_ids([g['idx'] for g in via]), '、'.join(sorted(set('0x%04x' % g['devid'] for g in via)))))
    # 异常明细
    bad = [f for f in Findings.items if f[0] == '真伪' and f[1] >= WARN]
    if bad:
        rep.sub('异常明细：')
        for cat, lvl, msg in bad:
            rep.add('    %s %s' % (lvl_color(lvl, '[%s]' % LEVEL_TEXT[lvl]), msg))
    # 驱动软件完整性
    rep.sub('驱动软件完整性（检查 nvidia-smi、NVML 是否被替换或劫持）：')
    drv, smi, nv = ctx['drv'], ctx['smi'], ctx['nvml']
    rows = []
    files = ctx['smi_files']
    if files:
        f0 = files[0]
        if G.test_mode:
            elf = (INFO, '测试模式，未检查')
        elif f0.get('elf'):
            elf = (OK, 'ELF 二进制文件')
        else:
            elf = (WARN, '不是 ELF 二进制文件（实际是%s）' % ('脚本' if f0.get('script') else '未知格式'))
        where = f0['path'] if f0['real'] == f0['path'] else '%s -> %s' % (f0['path'], f0['real'])
        rows.append(['nvidia-smi 文件', lv_cell(elf[0], LEVEL_TEXT[elf[0]]),
                     clip('%s，%s 字节，%s' % (elf[1], s(f0.get('size')), where), 100)])
        lvl = OK if f0['pkg_ok'] else (WARN if f0['pkg_ok'] is False else INFO)
        lvl = max([lvl] + [l for c, l, m in Findings.items if c == '驱动软件' and 'nvidia-smi' in m and '软件包' in m])
        rows.append(['nvidia-smi 包校验', lv_cell(lvl, LEVEL_TEXT[lvl]), f0['pkg_msg']])
        rows.append(['nvidia-smi SHA256', '-', s(f0.get('sha256'))])
        if len(files) > 1:
            rows.append(['多个 nvidia-smi', lv_cell(WARN, '警告'), '、'.join(f['path'] for f in files)])
    else:
        rows.append(['nvidia-smi 文件', lv_cell(WARN, '警告'), '未找到 nvidia-smi'])
    vers = ctx.get('versions', {})
    if vers:
        lvl = max([OK] + [l for c, l, m in Findings.items if c == '驱动软件' and '版本不一致' in m])
        rows.append(['驱动各组件版本', lv_cell(lvl, LEVEL_TEXT[lvl]), '，'.join('%s=%s' % kv for kv in vers.items())])
    if nv['ok']:
        rows.append(['NVML 交叉核对', lv_cell(OK, '通过'),
                     clip('绕过 nvidia-smi，直接调用 %s 读取数据' % s(nv.get('lib')), 100)])
    else:
        rows.append(['NVML 交叉核对', lv_cell(WARN if smi['ok'] else INFO, '不可用'), s(nv.get('error'))])
    pre = drv.get('preload_file') or []
    lvl = WARN if (pre or drv.get('preload_env')) else OK
    rows.append(['LD_PRELOAD 劫持', lv_cell(lvl, LEVEL_TEXT[lvl]),
                 '、'.join(pre + ([drv['preload_env']] if drv.get('preload_env') else []))
                 or '/etc/ld.so.preload 为空，也没有设置 LD_PRELOAD'])
    cnt = [('PCI', len([p for p in ctx['pci'].values() if is_nv_gpu(p)])), ('驱动（/proc）', len(ctx['proc'])),
           ('nvidia-smi', len(smi['gpus']) if smi['ok'] else None), ('NVML', len(nv['gpus']) if nv['ok'] else None)]
    cnt = [(k, v) for k, v in cnt if v is not None]
    lvl = OK if len(set(v for _, v in cnt)) <= 1 else WARN
    rows.append(['各来源的 GPU 数量', lv_cell(lvl, LEVEL_TEXT[lvl]), '，'.join('%s=%d' % kv for kv in cnt)])
    if drv.get('kmod_line'):
        rows.append(['内核模块', '-', clip(drv['kmod_line'], 90)])
        rows.append(['模块 srcversion', '-', '%s（taint=%s；可以和装了同版本驱动的正常机器比对）'
                     % (s(drv.get('srcversion')), s(drv.get('taint')))])
    rep.table(['检查项', '结果', '说明'], rows)
    rep.note('判定方法（全程只读，几个数据源互相印证）：\n'
             '  1) 设备 ID：由 GPU 芯片熔丝决定，改名字、贴标签、改 nvidia-smi 都改不了，是识别型号的主要依据；\n'
             '     例如 H100 SXM5 是 0x2330、H100 PCIe 是 0x2331，而 H20 是 0x2329、H800 是 0x2324；'
             'RTX 4090 是 0x2684，4090 D 是 0x2685；\n'
             '  2) 驱动报告的型号：/proc 里的驱动信息、直接调用 NVML（nvidia-smi 底层用的驱动库）、nvidia-smi，'
             '三者都要和设备 ID 对得上；\n'
             '  3) 规格比对：SM 数（GPU 计算单元的数量）、显存位宽、显存容量、功耗墙、最大频率、NVLink 链路数，'
             '要与该型号的标准值一致；\n'
             '  4) 驱动软件：nvidia-smi 是不是原厂的 ELF 二进制文件、与软件包里的原始文件是否一致、驱动各组件版本是否一致、'
             '有没有 LD_PRELOAD 劫持。\n'
             '  局限：防不住内核级伪造（改内核或 PCI 层）；要实测算力，需要在维护窗口对空闲 GPU 单独压测。')


def render_nvlink(rep, ctx):
    gpus, topo, fm, pci = ctx['gpus'], ctx['topo'], ctx['fm'], ctx['pci']
    rep.section('五、NVLink 拓扑')
    if topo['ok']:
        labels = [l for l in topo['order'] if l.startswith('GPU')]
        rows = []
        for a in labels:
            row = topo['rows'][a]
            cells = [a]
            for b in labels:
                v = row.get(b, '-')
                cells.append(dim(v) if v == 'X' else (green(v) if v.startswith('NV') else yellow(v)))
            cells += [row.get('CPU Affinity', '-'), row.get('NUMA Affinity', '-') or '-']
            rows.append(cells)
        rep.sub('GPU 互联矩阵（nvidia-smi topo -m）：')
        rep.table([''] + labels + ['就近的 CPU 核', 'NUMA 节点'], rows, aligns='l' + 'c' * len(labels))
        rep.note('NV# = 通过 # 条 NVLink 互联（HGX 机型经过 NVSwitch）\n'
                 'PIX = 最多经过一个 PCIe 桥（通常在同一个 PCIe Switch 下）；PXB = 经过多个 PCIe 桥，但不经过 CPU\n'
                 'PHB = 经过 CPU 上的 PCIe Host Bridge；NODE = 同一 NUMA 节点内，跨 PCIe Host Bridge；'
                 'SYS = 跨 NUMA 节点（经过 CPU 之间的 QPI/UPI 等互联）')
    else:
        rep.note('无法获取 GPU 互联矩阵：%s' % s(topo.get('error'), 'nvidia-smi 不可用'))
    nvsw = sorted(p['bdf'] for p in pci.values() if is_nvswitch(p))
    sw_idx = dict((b, i) for i, b in enumerate(nvsw))
    gpu_idx = dict((g['bdf'], g['idx']) for g in gpus)
    rows = []
    for g in gpus:
        nl = g.get('nvl')
        if not nl:
            continue
        speeds = Counter(nl['speeds'])
        each = '%.3f GB/s' % speeds.most_common(1)[0][0] if speeds else '-'
        total = '%.1f GB/s' % sum(nl['speeds']) if nl['speeds'] else '-'
        dist = Counter()
        for lk in ((g['nvml'] or {}).get('nvlinks') or {}).values():
            r = lk.get('remote')
            if lk.get('active') and r:
                dist['SW%d' % sw_idx[r] if r in sw_idx else ('GPU%s' % gpu_idx[r] if r in gpu_idx else short_bdf(r))] += 1
        dist_txt = ' '.join('%s:%d' % kv for kv in sorted(dist.items())) or '-'
        lvl = OK if not nl['inactive'] else WARN
        d = g['smi'] or {}
        fab = '%s/%s' % (d['fabric_state'], s(d.get('fabric_status'))) if d.get('fabric_state') else '-'
        rows.append(['GPU%s' % g['idx'], lv_cell(lvl, '%d/%d' % (nl['active'], nl['total'])), each, total,
                     lv_cell(WARN, ','.join(str(x) for x in nl['inactive'])) if nl['inactive'] else '-',
                     dist_txt, s(nl.get('errors')), fab])
    if rows:
        rep.sub('NVLink 链路状态：')
        rep.table(['GPU', '已激活/总数', '单条速率', '合计带宽(单向)', '未激活的链路', '对端:链路数(NVML)', '错误计数',
                   'Fabric 状态'], rows)
    elif any(g['db'] and g['db'].get('links') for g in gpus):
        rep.note('没有读到 NVLink 链路信息')
    else:
        rep.note('本机 GPU 无 NVLink（或没有装 NVLink 桥）')
    if nvsw:
        devs = Counter(NVSWITCH_DB.get(pci[b]['device'], '0x%04x' % pci[b]['device']) for b in nvsw)
        rep.add('  NVSwitch: %d 颗（%s），PCI 地址 %s' % (len(nvsw), '，'.join('%s x%d' % kv for kv in devs.items()),
                                                    ', '.join('SW%d=%s' % (i, short_bdf(b)) for i, b in enumerate(nvsw))))
    if nvsw or fm.get('active') == 'active':
        rep.add('  Fabric Manager: %s' % fm_text(fm))


def _nic_affinity(topo, gpus):
    """返回 ({gpu_idx: [(nic, rel)]}, {nic: [gpu_idx]})：只取最近的关系（PIX/PXB 优先）"""
    g2n, n2g = {}, {}
    if not topo['ok']:
        return g2n, n2g
    nic_cols = [c for c in topo['cols'] if not c.startswith('GPU') and c not in
                ('CPU Affinity', 'NUMA Affinity', 'GPU NUMA ID')]
    for g in gpus:
        row = topo['rows'].get('GPU%s' % g['idx'])
        if not row:
            continue
        best, rank = [], 99
        for c in nic_cols:
            v = row.get(c)
            r = TOPO_RANK.get(v, 99)
            if r < rank:
                best, rank = [], r
            if r == rank and r < 99:
                best.append((topo['nic_map'].get(c, c), v))
        if best and rank <= 2:
            g2n[g['idx']] = best
            for nic, _ in best:
                n2g.setdefault(nic, []).append(g['idx'])
    return g2n, n2g


def render_ib(rep, ctx):
    hcas = [h for h in ctx['ib'] if not h.get('is_vf')]
    vfs = len(ctx['ib']) - len(hcas)
    rep.section('六、IB / RDMA 网卡')
    if not hcas:
        rep.note('未发现 IB/RDMA 设备（/sys/class/infiniband 为空）')
        return
    g2n, n2g = _nic_affinity(ctx['topo'], ctx['gpus'])
    rows = []
    for h in hcas:
        v = h.get('vpd') or {}
        degraded = h['cur_gen'] and h['max_gen'] and (h['cur_gen'] < h['max_gen'] or h['cur_w'] != h['max_w'])
        link = '%s / %s' % (h['link'], h['link_max'])
        rows.append([h['dev'], h['model'], s(v.get('PN'), '(需 root 权限)' if not G.is_root else '-'), s(v.get('SN')),
                     short_bdf(h['bdf']), s(h['numa'] if (h['numa'] is not None and h['numa'] >= 0) else None),
                     yellow(link) if degraded else link, s(h['fw']), s(h['psid'])])
    rep.sub('网卡硬件：')
    rep.table(['设备', '型号', '料号(PN)', '序列号', 'PCI 地址', 'NUMA', 'PCIe 当前/最高', '固件', 'PSID'], rows)
    descs = sorted(set(h['vpd']['ID'] for h in hcas if h.get('vpd') and h['vpd'].get('ID')))
    for dsc in descs:
        rep.note('型号描述: ' + dsc)
    rows = []
    for h in hcas:
        for pt in h['ports']:
            ok = pt['state'] == 'ACTIVE'
            c = pt['counters']
            err = '%s/%s/%s' % (c.get('link_downed', '-'), c.get('symbol_error', '-'), c.get('port_rcv_errors', '-')) if c else '-'
            rate = re.sub(r'\s+', ' ', (pt['rate'] or '-').replace('Gb/sec', 'Gb/s'))
            gl = n2g.get(h['dev'])
            rows.append(['%s:%s' % (h['dev'], pt['port']), s(pt['link_layer']),
                         lv_cell(OK if ok else WARN, s(pt['state'])), s(pt['phys']), rate,
                         s(pt['lid']) if pt['link_layer'] == 'InfiniBand' else '-',
                         ','.join(h['netdevs']) or '-', ('GPU' + compress_ids(gl)) if gl else '-', err])
    rep.sub('端口状态：')
    rep.table(['设备:端口', '链路层', '状态', '物理状态', '速率', 'LID', '网口', '就近 GPU', '错误 down/symbol/rcv'], rows)
    if g2n:
        rows = [['GPU%s' % i, ', '.join('%s(%s)' % x for x in g2n[i])] for i in sorted(g2n)]
        rep.sub('GPU 就近的 RDMA 网卡（用 GPUDirect RDMA 时，GPU 应优先走离它最近的网卡）：')
        rep.table(['GPU', '最近的 RDMA 网卡（PIX = 同一个 PCIe Switch 下，PXB = 隔着多级 PCIe Switch）'], rows)
    mods = ctx['modules']
    stack = [m for m in ('mlx5_core', 'mlx5_ib', 'ib_core', 'ib_uverbs', 'nvidia_peermem', 'nv_peer_mem', 'gdrdrv',
                         'nvidia_fs') if m in mods]
    rep.add('  RDMA 软件栈: %s；已加载模块: %s%s' % (
        ctx['ofed'].get('ofed') or '内核自带驱动', ', '.join(stack) or '-',
        '；另有 %d 个 SR-IOV 虚拟网卡（VF）未列出' % vfs if vfs else ''))


def render_eth(rep, ctx):
    nics, vfs = ctx['eth']
    rep.section('七、其它网卡')
    if not nics:
        rep.note('无')
        return
    names = ctx.get('lspci') or {}
    rows = []
    for n in nics:
        model = MLX_DB.get(n['device']) if n['vendor'] == 0x15b3 else None
        model = model or names.get(n['bdf']) or ('%04x:%04x' % (n['vendor'], n['device']) if n['vendor'] else '-')
        sp = ('%dG' % (n['speed'] // 1000) if n['speed'] >= 1000 else '%dM' % n['speed']) if n['speed'] else '-'
        typ = 'IPoIB' if n['type'] == '32' else 'Ethernet'
        rows.append([n['ifname'], clip(model, 44), typ, s(n['driver']), short_bdf(n['bdf']),
                     s(n['numa'] if (n['numa'] is not None and n['numa'] >= 0) else None), sp,
                     lv_cell(OK if n['state'] == 'up' else INFO, s(n['state'])), s(n['mtu'])])
    rep.table(['网口', '型号', '类型', '驱动', 'PCI 地址', 'NUMA', '速率', '状态', 'MTU'], rows)
    if vfs:
        rep.note('另有 %d 个 SR-IOV 虚拟网口（VF）未列出' % vfs)


def render_health(rep, ctx):
    gpus = ctx['gpus']
    rep.section('八、GPU 运行状态（只读快照）')
    if not any(g['smi'] for g in gpus):
        rep.note('nvidia-smi 不可用，没有运行状态数据')
        return
    rows = []
    for g in gpus:
        d, p = g['smi'], g['pci'] or {}
        if not d:
            continue
        temp = '%s/%s' % (iv(d.get('temp')), iv(d.get('mem_temp')))
        pw = '%s/%sW' % (iv(d.get('power_draw')), iv(d.get('power_limit')))
        mem = '%s/%s' % (iv(d.get('mem_used'), '0'), iv(d.get('mem_total')))
        unc = '%s/%s' % (s(d.get('ecc_vol_unc')), s(d.get('ecc_agg_unc')))
        unc = lv_cell(WARN, unc) if d.get('ecc_vol_unc') else unc
        rr = '待生效' if d.get('rr_pending') == 'Yes' else ('失败' if d.get('rr_failure') == 'Yes' else '正常')
        rr = lv_cell(OK if rr == '正常' else (FAIL if rr == '失败' else WARN), rr) if d.get('rr_pending') else '-'
        thr = [THROTTLE_CN.get(t, t) for t in d.get('throttle', [])]
        thr_txt = lv_cell(WARN, ','.join(thr)) if any(t in THROTTLE_BAD for t in d.get('throttle', [])) else (','.join(thr) or '-')
        codes = (ctx.get('xid_by') or {}).get(g['bdf'][:-2], [])
        xid = ','.join('%d' % c for c in sorted(set(codes))) if codes else ('0' if ctx['xid']['ok'] else '-')
        rows.append(['GPU%s' % g['idx'], temp, pw, '%s%%' % iv(d.get('util'), '0'), mem,
                     str(d.get('procs', 0)), fmt_link(p.get('cur_speed'), p.get('cur_width')), s(d.get('ecc')), unc,
                     rr, thr_txt, lv_cell(WARN, xid) if codes else xid])
    rep.table(['GPU', '温度(°C) 核心/显存', '功耗 当前/上限', '利用率', '显存 已用/总量(MiB)', '进程数', 'PCIe 当前速率',
               'ECC', '不可纠正 ECC 本次/累计', '行重映射', '降频原因', 'Xid'], rows, aligns='l')
    rep.note('空闲时 GPU 的 PCIe 会自动降速节能（如降到 Gen1），属正常现象。\n'
             '不可纠正 ECC：“本次”指驱动本次加载以来，“累计”指这张卡历史上的总数。\n'
             'Xid 是 NVIDIA 驱动写进内核日志的 GPU 错误码，本次从 %s 读取。' % s(ctx['xid'].get('src')))


def _skus(gpus):
    c = Counter(g['db']['sku'] for g in gpus if g['db'])
    return '、'.join(k if len(c) == 1 else '%s x%d' % (k, n) for k, n in c.most_common()) or '-'


def auth_level(gpus):
    """真伪总判定：任一卡不通过/驱动软件有篡改证据 = FAIL；有卡无法确认 = WARN；否则 OK"""
    lvl = max([g.get('auth', g['verdict'][0]) for g in gpus] or [FAIL])
    return FAIL if Findings.worst(['驱动软件']) == FAIL else lvl


def render_conclusion(rep, ctx, args):
    rep.section('九、检测结论')
    rows = []
    gpus = ctx['gpus']
    for cat in CATEGORIES:
        items = [(lvl, msg) for c, lvl, msg in Findings.items if c == cat]
        lvl = max([l for l, _ in items] or [OK])
        if cat == '真伪':
            vt = ctx.get('verdicts') or Counter()
            if lvl == OK and gpus:
                msg = '%d/%d 张 GPU 确认为 %s（设备 ID、驱动报告的型号、规格都对得上）' % (len(gpus), len(gpus), _skus(gpus))
            else:
                msg = '；'.join('%d 张：%s' % (n, v) for v, n in vt.most_common()) or '无 GPU'
        else:
            shown = [m for l, m in sorted(items, key=lambda x: -x[0]) if l >= WARN] or [m for l, m in items]
            msg = clip(shown[0], 96) if shown else '未发现问题'
            if len(shown) > 1:
                msg += '；另有 %d 项，见下方' % (len(shown) - 1)
        rows.append([cat, lv_cell(lvl, LEVEL_TEXT[lvl]), msg])
    rep.table(['类别', '结果', '摘要'], rows)
    auth = auth_level(gpus)
    others = [(c, l, m) for c, l, m in Findings.items if l >= WARN and (c != '真伪' or auth == OK)]
    if others:
        rep.sub('需关注：')
        for c, l, m in sorted(others, key=lambda x: (-x[1], CATEGORIES.index(x[0]))):
            rep.add('    %s [%s] %s' % (lvl_color(l, LEVEL_TEXT[l]), c, m))
    infos = [(c, m) for c, l, m in Findings.items if l == INFO]
    if infos:
        rep.sub('提示信息：')
        for c, m in infos:
            rep.add('    %s [%s] %s' % (cyan('提示'), c, m))
    allw = Findings.worst()
    rep.add('')
    if auth == FAIL:
        bad = [g for g in gpus if g['verdict'][0] == FAIL]
        if not gpus:
            rep.add(red('  总体结论：[不通过] 未发现任何 NVIDIA GPU。'))
        elif bad:
            vc = Counter(g['verdict'][1] for g in bad)
            cnt = '：' + next(iter(vc)) if len(vc) == 1 else '（%s）' % '；'.join('%d 张：%s' % (n, v) for v, n in vc.items())
            if all(g['verdict'][1].startswith('形态不符') for g in bad):
                hints = [re.search(r'（(如果采购的就是[^）]*)）', c['detail']) for g in bad for c in g['checks']
                         if c['key'] == 'hwid']
                hint = next((m.group(1) for m in hints if m), '')
                rep.add(red('  总体结论：[不通过] %d 张 GPU 实为 %s，与期望的 %s 版不符%s。详见第四节。'
                            % (len(bad), _skus(bad), args.exp['label'], '；' + hint if hint else '')))
            else:
                rep.add(red('  总体结论：[不通过] %d 张 GPU 真伪核验不通过%s。详见第四节。' % (len(bad), cnt)))
                # 没指定 --expect、本机只是别的型号（没有篡改迹象）时，提示怎样指定期望型号
                if args.expect == DEFAULT_EXPECT and all(
                        g['verdict'][1].startswith('非') and
                        [c['key'] for c in g['checks'] if c['level'] == FAIL] == ['hwid'] for g in bad):
                    rep.add('  %s：%s' % (cyan('提示'), expect_hint(bad, args.exp)))
        else:
            rep.add(red('  总体结论：[不通过] 驱动软件疑似被篡改，读到的 GPU 信息不可信，详见第四节。'))
    elif auth == WARN:
        rep.add(yellow('  总体结论：[待确认] 有的项目无法确认，暂时不能判定真伪，详见第四节。'))
    elif allw >= WARN:
        rep.add(yellow('  总体结论：[通过，有告警] %d 张 GPU 确认为真 %s；另有 %d 项告警，见上方“需关注”。'
                       % (len(gpus), _skus(gpus), len(others))))
    else:
        rep.add(green('  总体结论：[通过] %d 张 GPU 确认为真 %s，未发现健康或配置问题。' % (len(gpus), _skus(gpus))))
    rep.add('')


# ============================================================================
# 主流程
# ============================================================================

def setup_stdio():
    for name in ('stdout', 'stderr'):
        st = getattr(sys, name)
        enc = (getattr(st, 'encoding', '') or '').lower().replace('-', '')
        if enc != 'utf8' and hasattr(st, 'buffer'):
            try:
                setattr(sys, name, io.TextIOWrapper(st.buffer, encoding='utf-8', errors='replace', line_buffering=True))
            except Exception:
                pass


def lower_priority():
    try:
        os.nice(19)
    except Exception:
        pass
    ion = which('ionice')
    if ion and not G.test_mode:
        run([ion, '-c', '3', '-p', str(os.getpid())], 5)


class Progress(object):
    def __init__(self):
        self.tty = sys.stderr.isatty()

    def __call__(self, msg):
        if self.tty:
            sys.stderr.write('\r\x1b[K[gpu_check] 正在采集：%s ...' % msg)
            sys.stderr.flush()

    def done(self):
        if self.tty:
            sys.stderr.write('\r\x1b[K')
            sys.stderr.flush()


ERRORS = []


def safe(label, fn, default, *a):
    try:
        return fn(*a)
    except Exception as e:      # 真实环境格式千差万别：单项失败只记录，不影响其它检测
        ERRORS.append('%s：%s: %s' % (label, type(e).__name__, e))
        return default


def collect_all(args):
    step = Progress()
    ctx = {}
    step('系统 / CPU / 内存 / 存储')
    ctx['sys'] = safe('系统信息', collect_system, {'hostname': socket.gethostname(), 'virt': None, 'container': False})
    ctx['cpu'] = safe('CPU', collect_cpu, dict(model=None, sockets=None, cores=None, threads=None, numa=None))
    ctx['mem'] = safe('内存', collect_memory, {'total_gib': None, 'dimms': None})
    ctx['disks'] = safe('存储', collect_storage, None)
    step('PCI 设备')
    ctx['pci'] = safe('PCI 扫描', scan_pci, {})
    ctx['modules'] = safe('内核模块', loaded_modules, set())
    step('NVIDIA 驱动与 nvidia-smi 完整性')
    ctx['drv'] = safe('驱动信息', collect_driver, {'proc_ok': False})
    smi_paths = locate_all('nvidia-smi', ['/usr/bin', '/usr/local/bin', '/usr/local/nvidia/bin', '/opt/nvidia/bin'])
    ctx['smi_files'] = safe('nvidia-smi 完整性', lambda: [file_check(smi_paths[0])] + [dict(path=p) for p in smi_paths[1:]]
                            if smi_paths else [], [])
    smi = smi_paths[0] if smi_paths else None
    step('nvidia-smi -q（只读查询）')
    ctx['smi'] = safe('nvidia-smi', collect_smi, {'ok': False, 'error': '解析出错', 'gpus': []}, smi)
    ctx['proc'] = safe('/proc 驱动信息', collect_proc_gpus, {})
    step('直接调用 NVML 查询')
    ctx['nvml'] = safe('NVML', collect_nvml, {'ok': False, 'error': '调用出错', 'gpus': {}}, args.nvml_lib)
    if args.cuda:
        step('CUDA 设备属性（无上下文）')
        ctx['cuda'] = safe('CUDA', collect_cuda, {'ok': False, 'error': '调用出错', 'gpus': {}}, args.cuda_lib)
    step('NVLink 拓扑')
    ctx['topo'] = safe('拓扑', collect_topo, {'ok': False, 'cols': [], 'rows': {}, 'order': [], 'nic_map': {}, 'error': '解析出错'}, smi)
    ctx['nvlink'] = safe('NVLink', collect_nvlink, {'status': {}, 'errors': {}, 'error': '解析出错'}, smi)
    ctx['fm'] = safe('Fabric Manager', collect_fm, {})
    step('IB / 网卡')
    ctx['ib'] = safe('IB 网卡', collect_ib, [], ctx['pci'])
    rdma_netdevs = set(n for h in ctx['ib'] for n in h['netdevs'])
    ctx['eth'] = safe('网卡', collect_eth, ([], 0), ctx['pci'], rdma_netdevs)
    unknown_gpu = any(is_nv_gpu(p) and p.get('device') not in GPU_DB for p in ctx['pci'].values())
    if unknown_gpu or any(n['vendor'] != 0x15b3 for n in ctx['eth'][0]):
        ctx['lspci'] = safe('lspci', lspci_names, {})      # 型号库里没有的 GPU 用本机 pci.ids 兜底识别
    ctx['ofed'] = safe('OFED', collect_ofed, {})
    step('内核日志 Xid')
    ctx['xid'] = safe('Xid', collect_xid, {'ok': False, 'xid': [], 'sxid': 0, 'error': '解析出错'})
    ctx['gpus'] = safe('GPU 汇总', assemble_gpus, [], ctx)
    step.done()
    return ctx


def parse_args():
    ap = argparse.ArgumentParser(
        prog='gpu_check.py',
        description='GPU 服务器只读检测：硬件清单、IB 网卡、NVLink 拓扑、GPU 型号真伪核验（按硬件设备 ID 识别 H20/H800 '
                    '冒充 H100、4090 D 冒充 4090、矿卡等；不影响线上业务）。',
        epilog='退出码：0=通过 1=有告警 2=真伪不通过（不是期望的型号，或疑似篡改） 3=运行错误')
    ap.add_argument('-o', '--output', metavar='FILE', help='同时把报告（无颜色）保存到文件')
    ap.add_argument('--expect', default=DEFAULT_EXPECT, metavar='型号',
                    help='期望的 GPU 型号，默认 h100-sxm：H100 SXM5，即 NVLink 版（HGX 机型），PCIe 版、NVL 版判“形态不符”。'
                         'h100 表示任意 H100 形态均可；也可用 h100-pcie、h100-nvl。形态后缀 -sxm（同义 -nvlink）/-pcie/-nvl '
                         '适用于各型号，如 a100-sxm；其它型号如 h20、a100、a800、l40s、l20、4090、4090d、5090、pro6000d、a6000、6000ada、'
                         '1080ti、cmp170hx；'
                         'any 表示不限型号，只做一致性检查')
    ap.add_argument('--expect-gpus', type=int, metavar='N', help='期望的 GPU 数量（HGX 8 卡机型默认按 8 检查）')
    ap.add_argument('--cuda', action='store_true',
                    help='额外通过 CUDA 驱动 API 读取 SM 数/L2 等属性（只调用 cuInit 与属性查询，不创建上下文、不占显存）')
    ap.add_argument('--no-color', action='store_true', help='禁用颜色')
    ap.add_argument('-V', '--version', action='version', version='gpu_check ' + VERSION)
    # 以下为离线测试用的隐藏参数
    ap.add_argument('--sysroot', help=argparse.SUPPRESS)
    ap.add_argument('--nvml-lib', default='libnvidia-ml.so.1', help=argparse.SUPPRESS)
    ap.add_argument('--cuda-lib', default='libcuda.so.1', help=argparse.SUPPRESS)
    ap.add_argument('--assume-root', action='store_true', help=argparse.SUPPRESS)
    return ap.parse_args()


def main():
    setup_stdio()
    args = parse_args()
    args.exp = parse_expect(args.expect)
    if args.exp is False:
        sys.stderr.write('gpu_check: 无法识别 --expect %s。示例：h100、h100-sxm、h20、a100、a800-sxm、l40s、l20、4090、'
                         '4090d、5090、pro6000d、a6000、6000ada、any（不限型号）\n' % args.expect)
        return 3
    G.sysroot = (args.sysroot or '').rstrip('/')
    G.test_mode = bool(G.sysroot)
    if not G.test_mode and not sys.platform.startswith('linux'):
        sys.stderr.write('gpu_check: 只支持 Linux\n')
        return 3
    G.is_root = args.assume_root or os.geteuid() == 0
    G.color = (sys.stdout.isatty() and not args.no_color and os.environ.get('TERM') != 'dumb'
               and 'NO_COLOR' not in os.environ)
    lower_priority()
    t0 = time.time()
    ctx = collect_all(args)
    try:
        evaluate(ctx, args)
    except Exception as e:
        ERRORS.append('综合判定：%s: %s' % (type(e).__name__, e))
        Findings.add('系统', WARN, '综合判定过程出错，结论可能不完整：%s' % e)
    rep = render(ctx, args, time.time() - t0)
    text = '\n'.join(rep.lines)
    print(text)
    if args.output:
        try:
            with io.open(args.output, 'w', encoding='utf-8') as f:
                f.write(ANSI_RE.sub('', text) + '\n')
            sys.stderr.write('报告已保存到 %s\n' % args.output)
        except Exception as e:
            sys.stderr.write('保存报告失败：%s\n' % e)
    if auth_level(ctx['gpus']) == FAIL:
        return 2
    if Findings.worst() >= WARN:
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
# ---- end of gpu_check.py ----
