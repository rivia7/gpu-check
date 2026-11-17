#!/usr/bin/env python3
"""
型号名称识别规则与型号库的自洽性测试（离线，不需要夹具）：
  - NVIDIA 驱动给设备 ID 起的官方名称（摘自驱动 README 的支持列表）都要解析成正确的型号键。
    解析错了，真卡会被判“名称不一致”；解析不出，会被判“待确认”。
  - --expect 的常见写法都要能识别。
  - 型号库每一条的型号名都要能识别为它自己的型号键，芯片代号都要能推出架构。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('gpu_check', os.path.join(HERE, '..', 'gpu_check.py'))
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

NAMES = [
    ('NVIDIA H100 80GB HBM3', 'H100'), ('NVIDIA H100 PCIe', 'H100'), ('NVIDIA H100 NVL', 'H100'),
    ('NVIDIA H800', 'H800'), ('NVIDIA H20', 'H20'), ('NVIDIA H200', 'H200'), ('NVIDIA GH200 480GB', 'GH200'),
    ('NVIDIA A100-SXM4-80GB', 'A100'), ('NVIDIA PG509-210', 'A100'), ('NVIDIA A800-SXM4-80GB', 'A800'),
    ('NVIDIA A10G', 'A10G'), ('NVIDIA A10M', 'A10M'), ('NVIDIA L40S', 'L40S'), ('NVIDIA L20', 'L20'),
    ('NVIDIA L4', 'L4'), ('Tesla V100-SXM2-16GB', 'V100'), ('Tesla PG503-216', 'V100'), ('Tesla T4', 'T4'),
    ('Tesla P40', 'P40'),
    ('NVIDIA RTX 6000D', 'RTX PRO 6000D'), ('NVIDIA RTX PRO 6000 Blackwell Server Edition', 'RTX PRO 6000'),
    ('NVIDIA RTX 6000 Ada Generation', 'RTX 6000 ADA'), ('Quadro RTX 6000', 'RTX 6000'),
    ('NVIDIA RTX A6000', 'RTX A6000'), ('NVIDIA RTX A400', 'RTX A400'),
    ('NVIDIA GeForce RTX 4090 D', 'RTX 4090 D'), ('NVIDIA GeForce RTX 5090 D v2', 'RTX 5090 D V2'),
    ('NVIDIA GeForce GTX 1080 Ti', 'GTX 1080 TI'), ('NVIDIA GeForce GTX 1660 SUPER', 'GTX 1660 SUPER'),
    ('NVIDIA GeForce GT 1030', 'GT 1030'), ('NVIDIA TITAN Xp COLLECTORS EDITION', 'TITAN XP'),
    ('NVIDIA TITAN X (Pascal)', 'TITAN X'), ('NVIDIA GeForce GTX TITAN X', 'GTX TITAN X'), ('NVIDIA TITAN V JHH Special Edition', 'TITAN V'),
    ('Quadro P6000', 'QUADRO P6000'), ('Quadro GV100', 'QUADRO GV100'), ('Matrox D-Series D1450', 'QUADRO P1000'),
    ('NVIDIA T400 4GB', 'T400'), ('NVIDIA T1000 8GB', 'T1000'), ('NVIDIA DGX Display', 'T1000'),
    ('NVIDIA CMP 90HX', 'CMP 90HX'), ('NVIDIA P106-100', 'P106-100'),
    ('NVIDIA Graphics Device', None),
]
EXPECTS = [
    ('h100-sxm', 'H100', 'SXM'), ('h100-nvlink', 'H100', 'SXM'), ('h100', 'H100', None), ('h100-pcie', 'H100', 'PCIE'),
    ('pro6000d', 'RTX PRO 6000D', None), ('6000d', 'RTX PRO 6000D', None), ('4090d', 'RTX 4090 D', None),
    ('1080ti', 'GTX 1080 TI', None), ('p6000', 'QUADRO P6000', None), ('t1000', 'T1000', None),
    ('cmp170hx', 'CMP 170HX', None), ('a10g', 'A10G', None), ('1030', 'GT 1030', None), ('gt1030', 'GT 1030', None),
    ('a1000', 'RTX A1000', None), ('p40', 'P40', None), ('p106-100', 'P106-100', None), ('1060', 'GTX 1060', None),
]

bad = []
for name, want in NAMES:
    got = hc.model_key(name)
    if got != want:
        bad.append('名称“%s”识别为 %s，应为 %s' % (name, got, want))
for text, key, form in EXPECTS:
    r = hc.parse_expect(text)
    if not r or (r['key'], r['form']) != (key, form):
        bad.append('--expect %s 解析为 %s，应为 %s/%s' % (text, r and (r['key'], r['form']), key, form))
if hc.parse_expect('any') is not None or hc.parse_expect('no-such-gpu') is not False:
    bad.append('--expect any / 无法识别的型号 处理不对')
for devid, d in sorted(hc.GPU_DB.items()):
    keys = hc.db_keys(d)
    if keys and hc.model_key(d['sku']) not in keys:
        bad.append('0x%04x 型号名“%s”识别为 %s，不在 %s 中' % (devid, d['sku'], hc.model_key(d['sku']), sorted(keys)))
    if d['chip'] and not (d['arch'] and d['cc']):
        bad.append('0x%04x 芯片代号 %s 推不出架构' % (devid, d['chip']))

for line in bad:
    print(line)
print('%d 个驱动名、%d 种 --expect 写法、%d 个设备 ID%s' % (len(NAMES), len(EXPECTS), len(hc.GPU_DB),
                                                    '，%d 处问题' % len(bad) if bad else ' 全部通过'))
sys.exit(1 if bad else 0)
