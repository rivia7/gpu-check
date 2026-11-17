#!/usr/bin/env python3
"""
复核 gpu_check.py 的内置型号库（维护者用，需要联网；GPU 服务器上用不到）。

  1. 名称核对：NVIDIA 驱动 README 的支持列表里，型号库每个设备 ID 的官方名称都必须能识别成该条目的型号。
     识别错了，真卡会被判“名称不一致”；识别不出，会被判“待确认”。
     RTX PRO 6000D 的驱动名叫“NVIDIA RTX 6000D”（没有 PRO），当初就是这样漏掉的。
  2. 缺口：列出官方列表里有、型号库里没有的台式机 / 服务器 GPU（按 pci.ids 排除笔记本和嵌入式芯片）。
  3. 规格（加 --dbgpu）：与 dbgpu（PyPI 包，数据来自 TechPowerUp）按型号名称比对 SM 数、位宽、显存、功耗。
     TechPowerUp 的数据有不少错误，分歧项要再查 NVIDIA 数据表；已经核实、以官方为准的列在 VERIFIED 里。

用法：python3 -I tools/verify_db.py [--driver 580.178.04 ...] [--dbgpu]
退出码：0 全部通过；1 有名称识别错误、未核实的规格分歧，或官方列表里出现了型号库没有的新 ID
"""
import argparse
import datetime
import html
import importlib.util
import io
import json
import os
import pickle
import re
import sys
import tarfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NV = 'https://download.nvidia.com/XFree86/Linux-x86_64/'
PCIIDS = 'https://raw.githubusercontent.com/pciutils/pciids/master/pci.ids'
# 与 TechPowerUp 不一致、但已按官方资料核实过的条目：设备 ID → 依据
VERIFIED = {
    0x1db1: 'NVIDIA V100 数据表：SXM2 版 300W',
    0x1db5: 'NVIDIA V100 数据表：SXM2 版 300W',
    0x1db4: 'NVIDIA V100 数据表：PCIe 版 250W',
    0x2237: 'NVIDIA/AWS A10G 数据表：80 个 RT 核心（即 80 个 SM）、300W',
    0x26b9: 'NVIDIA L40S 产品页：350W',
    0x27b8: 'L4 有 7424 个 CUDA 核心，÷128 = 58 个 SM',
}
# 官方列表里有、但有意不收录的 ID：缺口清单里单独列出，不算问题
SKIP = {
    0x1bb4: 'Tesla P6 是刀片服务器用的 MXM 模块',
    0x20b6: 'PG506-232 是工程板卡，公开资料不足',
    0x20fd: 'AX800 是 GPU + BlueField-3 DPU 的融合加速卡，公开资料不足',
    0x25ad: 'RTX 2050 只有笔记本版',
    0x25ed: 'RTX 2050 只有笔记本版',
    0x2e12: 'GB10 是 DGX Spark 的 SoC 集成 GPU',
    0x3041: 'Rubin（VR NVL72）尚无公开规格',
}
TOKENS = ['SXM5', 'SXM4', 'SXM3', 'SXM2', 'SXM', 'PCIe', 'NVL', 'DGXS', 'FHHL', 'Server', 'Max-Q', 'SFF', 'CNX']


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'gpu-check-verify-db'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_hc():
    spec = importlib.util.spec_from_file_location('gpu_check', os.path.join(HERE, '..', 'gpu_check.py'))
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


def official_names(versions):
    names = {}
    for v in versions:
        t = get(NV + v + '/README/supportedchips.html').decode('utf-8', 'replace')
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', t, re.S):
            c = [html.unescape(re.sub(r'<[^>]+>', '', x)).strip() for x in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            if len(c) >= 2 and re.match(r'^[0-9A-Fa-f]{4}\b', c[1]):
                names.setdefault(int(c[1][:4], 16), set()).add(c[0])
    return names


def pci_ids():
    out, inside = {}, False
    for line in get(PCIIDS).decode('utf-8', 'replace').splitlines():
        if line.startswith('10de '):
            inside = True
        elif inside and re.match(r'^[0-9a-f]{4} ', line):
            break
        elif inside:
            m = re.match(r'^\t([0-9a-f]{4})  (.*)$', line)
            if m:
                out[int(m.group(1), 16)] = m.group(2).strip()
    return out


def dbgpu_records():
    meta = json.loads(get('https://pypi.org/pypi/dbgpu/json').decode())
    url = next(u['url'] for u in meta['urls'] if u['filename'].endswith('.tar.gz'))
    with tarfile.open(fileobj=io.BytesIO(get(url, 120)), mode='r:gz') as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith('/data.pkl'))
        raw = tf.extractfile(member).read()

    class SafeUnpickler(pickle.Unpickler):      # 数据是 pickle：只放行 datetime.date，防止执行任意代码
        def find_class(self, module, name):
            if (module, name) == ('datetime', 'date'):
                return datetime.date
            raise pickle.UnpicklingError('不允许的对象 %s.%s' % (module, name))

    recs = [g for g in SafeUnpickler(io.BytesIO(raw)).load() if g.get('manufacturer') == 'NVIDIA'
            and not re.search(r'Mobile|Embedded|Laptop|MXM', g['name'])]
    return meta['info']['version'], recs


def tokens(s):
    return set(t for t in TOKENS if re.search(r'(?<![A-Za-z])%s(?![A-Za-z0-9])' % re.escape(t), s, re.I))


def compare_dbgpu(hc):
    ver, recs = dbgpu_records()
    print('\n== 3. 规格比对：dbgpu %s（TechPowerUp 数据，%d 款 NVIDIA 台式机 / 服务器 GPU）==' % (ver, len(recs)))
    for g in recs:
        g['key'] = hc.model_key(g['name'])
    seen, same, absent, diffs = set(), 0, [], []
    for devid, d in sorted(hc.GPU_DB.items()):
        sig = (d['sku'], d['sms'], d['bus'], d['mem'], d['maxw'])
        if d['family'].endswith('?') or sig in seen:
            continue
        seen.add(sig)
        c = [g for g in recs if g['key'] in hc.db_keys(d)]
        want = tokens(d['sku'])
        c = [g for g in c if (want & tokens(g['name'])) if want] or \
            [g for g in c if not (tokens(g['name']) & {'Max-Q', 'SFF', 'Server', 'CNX'})] or c
        mems = d['mem'] if isinstance(d['mem'], tuple) else ((d['mem'],) if d['mem'] else ())
        if mems:
            c = [g for g in c if g['memory_size_gb'] and round(g['memory_size_gb']) in mems]
        if not c:
            absent.append('0x%04x %s' % (devid, d['sku']))
            continue

        def vals(f):
            return sorted(set(g[f] for g in c if g[f] is not None))
        issues = []
        if d['sms'] and d['sms'] not in vals('streaming_multiprocessors'):
            issues.append('SM 本库 %s / 对方 %s' % (d['sms'], vals('streaming_multiprocessors')))
        if d['bus'] and d['bus'] not in vals('memory_bus_bits'):
            issues.append('位宽 本库 %s / 对方 %s' % (d['bus'], vals('memory_bus_bits')))
        mw = d['maxw'] if isinstance(d['maxw'], tuple) else ((d['maxw'],) if d['maxw'] else ())
        if mw and not set(mw) & set(vals('thermal_design_power_w')):
            issues.append('功耗 本库 %s / 对方 TDP %s' % (d['maxw'], vals('thermal_design_power_w')))
        if not issues:
            same += 1
        else:
            why = VERIFIED.get(devid)
            diffs.append('  %s0x%04x %-28s %s%s' % ('（已核实）' if why else '', devid, d['sku'], '；'.join(issues),
                                                   '\n        依据：' + why if why else ''))
    print('  一致 %d 项；分歧 %d 项（其中已核实 %d 项）' % (same, len(diffs), sum('已核实' in x for x in diffs)))
    for line in diffs:
        print(line)
    print('  dbgpu 没有对应型号或显存版本：%s' % ('、'.join(absent) or '无'))
    return sum('已核实' not in x for x in diffs)


def main():
    ap = argparse.ArgumentParser(description='复核 gpu_check.py 的内置型号库（需要联网）')
    ap.add_argument('--driver', nargs='*', default=[], metavar='版本', help='再核对这些驱动版本的支持列表（默认只取最新版）')
    ap.add_argument('--dbgpu', action='store_true', help='再与 dbgpu（TechPowerUp 数据）比对规格')
    a = ap.parse_args()
    hc = load_hc()
    latest = get(NV + 'latest.txt').decode().split()[0]
    versions = [latest] + [v for v in a.driver if v != latest]
    names, pci = official_names(versions), pci_ids()

    print('== 1. 名称核对：型号库 %d 个设备 ID，对照驱动 %s 的支持列表 ==' % (len(hc.GPU_DB), ' / '.join(versions)))
    ok = bad = 0
    for devid, d in sorted(hc.GPU_DB.items()):
        keys = hc.db_keys(d)
        for nm in sorted(names.get(devid, ())) if keys else ():
            k = hc.model_key(nm)
            if k in keys:
                ok += 1
            else:
                bad += 1
                print('  0x%04x %-28s 驱动名“%s”识别为 %s，应为 %s' % (devid, d['sku'], nm, k or '（无法识别）',
                                                                 ' / '.join(sorted(keys))))
    unlisted = [devid for devid in sorted(hc.GPU_DB) if devid not in names]
    print('  一致 %d 个，问题 %d 个；驱动未收录、按 pci.ids 收录的 ID %d 个' % (ok, bad, len(unlisted)))

    print('\n== 2. 缺口：官方列表里有、型号库没有的台式机 / 服务器 GPU ==')
    miss = 0
    for devid in sorted(names):
        label = pci.get(devid, '')
        chip = label.split(' ')[0] if label else ''
        mobile = re.search(r'M$', chip) or re.search(r'Mobile|Max-Q|Laptop|Embedded|Jetson|Tegra|DRIVE|N1X', label + ' '.join(names[devid]))
        if devid < 0x1b00 or devid in hc.GPU_DB or devid in SKIP or mobile:     # 0x1b00 以前是 Maxwell 及更早的芯片
            continue
        miss += 1
        print('  0x%04x %-50s pci.ids: %s' % (devid, ' / '.join(sorted(names[devid]))[:50], label or '-'))
    print('  新 ID %d 个；有意不收录 %d 个：' % (miss, len(SKIP)))
    for devid, why in sorted(SKIP.items()):
        print('    0x%04x %s' % (devid, why))

    spec_issues = compare_dbgpu(hc) if a.dbgpu else 0
    return 1 if (bad or miss or spec_issues) else 0


if __name__ == '__main__':
    sys.exit(main())
