#!/usr/bin/env python3
"""
生成 h100_check.py 的离线测试夹具：模拟一台 HGX 8 卡服务器（8x GPU + 4x NVSwitch + 8x CX7 NDR + 双口 CX6 Dx + I350）。
产物：<out>/root（模拟的 /sys /proc /etc）、<out>/bin（假命令，每次调用都记录到 <out>/data/calls.log）、
      <out>/nvml.conf（mocknv 动态库读取的 NVML/CUDA 数据）。

用法：python3 make_fixture.py <场景> <输出目录>
场景：
  h100      正常的 HGX H100 SXM5 80GB
  h20-fake  硬件是 H20，nvidia-smi 被篡改成显示 H100（内核驱动和 NVML 未被篡改）
  h20       诚实的 H20 机器
  h800      诚实的 H800 机器
  degraded  真 H100，但有 NVLink 掉链、IB 端口 down、FM 未运行、Xid 79、待处理行重映射等故障
  nodriver  真 H100 硬件，但未安装/未加载 NVIDIA 驱动
  h100pcie  8 张 H100 PCIe（无 NVSwitch、无 NVLink 桥）
  runfile   真 H100，但驱动是用 .run 覆盖安装在旧版 deb 包之上（包校验不一致，属运维问题而非篡改）
  rtx4090        8 张 GeForce RTX 4090（数值取自一台真实机器的报告：ECC 关闭、空载、无 NVLink/NVSwitch）
  rtx4090d       诚实的 RTX 4090 D 机器
  rtx4090d-fake  硬件是 RTX 4090 D，nvidia-smi 被篡改成显示 RTX 4090
  rtx4090-48g    改装成 48GB 显存的 RTX 4090（魔改卡）
  pciids         设备 ID 不在内置型号库里（虚构的 0x26ff），只能靠本机 pci.ids（lspci）识别
  rtx6000d       8 张 RTX PRO 6000D 服务器版（0x2bb9，驱动名“NVIDIA RTX 6000D”；数值取自一台真实机器的报告）
  unknownid      驱动名同上，但设备 ID 型号库和 pci.ids 都不认识（虚构的 0x2bfe）
注意：H20/H800 的功耗、SM 数等数值只是测试占位，不代表真实规格。
"""
import os
import shutil
import struct
import sys

GPU_BDFS = ['0000:18:00.0', '0000:2a:00.0', '0000:3a:00.0', '0000:5d:00.0',
            '0000:9a:00.0', '0000:ab:00.0', '0000:ba:00.0', '0000:db:00.0']
NVSW_BDFS = ['0000:05:00.0', '0000:06:00.0', '0000:07:00.0', '0000:08:00.0']
IB_BDFS = ['0000:1a:00.0', '0000:3c:00.0', '0000:4d:00.0', '0000:5e:00.0',
           '0000:9c:00.0', '0000:bc:00.0', '0000:cc:00.0', '0000:dc:00.0']
ROCE_BDFS = ['0000:1f:00.0', '0000:1f:00.1']
MGMT_BDFS = ['0000:c1:00.0', '0000:c1:00.1']

SPEC = {
    'h100': dict(devid=0x2330, subsys=0x16c1, name='NVIDIA H100 80GB HBM3', mem=81559, cores=16896, bus=5120,
                 maxw=700, maxsm=1980, maxmem=2619, links=18, pn='2330-885-A1', board_pn='692-2G520-0200-000',
                 inforom='G520.0200.00.05'),
    'h20': dict(devid=0x2329, subsys=0x198b, name='NVIDIA H20', mem=97871, cores=78 * 128, bus=6144,
                maxw=500, maxsm=1980, maxmem=2619, links=18, pn='2329-885-A1', board_pn='692-2G520-0280-000',
                inforom='G520.0280.00.01'),
    'h100pcie': dict(devid=0x2331, subsys=0x1626, name='NVIDIA H100 PCIe', mem=81559, cores=14592, bus=5120,
                     maxw=350, maxsm=1755, maxmem=1593, links=0, pn='2331-885-A1', board_pn='900-21010-0000-000',
                     inforom='1010.0200.00.02'),
    'h800': dict(devid=0x2324, subsys=0x17a6, name='NVIDIA H800', mem=81559, cores=16896, bus=5120,
                 maxw=700, maxsm=1980, maxmem=2619, links=8, pn='2324-885-A1', board_pn='692-2G520-0221-000',
                 inforom='G520.0221.00.01'),
    'rtx4090': dict(devid=0x2684, subsys=0x167c, name='NVIDIA GeForce RTX 4090', mem=24564, cores=128 * 128, bus=384,
                    maxw=600, maxsm=3225, maxmem=10501, links=0, pn='2684-301-A1', board_pn='N/A',
                    inforom='G002.0000.00.03', arch='Ada Lovelace', cc='8.9', brand='GeForce', gen=4, ecc='Disabled',
                    vbios='95.02.3C.C0.5A', util=0),
}
SPEC['rtx4090d'] = dict(SPEC['rtx4090'], devid=0x2685, subsys=0x16f3, name='NVIDIA GeForce RTX 4090 D',
                        cores=114 * 128, pn='2685-301-A1')
SPEC['rtx4090-48g'] = dict(SPEC['rtx4090'], mem=49140)
SPEC['pciids'] = dict(SPEC['rtx4090'], devid=0x26ff, pn='26FF-301-A1')
SPEC['rtx6000d'] = dict(devid=0x2bb9, subsys=0x2091, name='NVIDIA RTX 6000D', mem=85651, cores=156 * 128, bus=448,
                        maxw=600, maxsm=2430, maxmem=12481, links=0, pn='2BB9-891-A1', board_pn='900-2G153-0030-000',
                        inforom='G153.0235.00.02', arch='Blackwell', cc='12.0', brand='NVIDIA RTX', gen=5,
                        ecc='Enabled', vbios='98.02.81.00.02', util=0)
SPEC['unknownid'] = dict(SPEC['rtx6000d'], devid=0x2bfe, pn='2BFE-891-A1')
# 各场景：真实硬件用哪份规格；nvidia-smi 被篡改的场景另指定 nvidia-smi 显示的规格
HW_OF = {'h20-fake': 'h20', 'rtx4090d-fake': 'rtx4090d'}
SMI_OF = {'h20-fake': 'h100', 'rtx4090d-fake': 'rtx4090'}
DEFAULTS = dict(arch='Hopper', cc='9.0', brand='NVIDIA', gen=5, ecc='Enabled', vbios='96.00.99.00.01', util=97)
LINK = {4: '16.0 GT/s PCIe', 5: '32.0 GT/s PCIe'}


def wr(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    with open(path, 'wb') as f:
        f.write(content)
    os.chmod(path, mode)


def ln(path, target):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.lexists(path):
        os.remove(path)
    os.symlink(target, path)


def pci_dev(root, bdf, vendor, device, cls, svendor, sdevice, numa, speed, width, maxspeed, maxwidth,
            driver=None, vpd=None):
    base = '%s/sys/bus/pci/devices/%s' % (root, bdf)
    for k, v in (('vendor', '0x%04x' % vendor), ('device', '0x%04x' % device), ('class', '0x%06x' % cls),
                 ('subsystem_vendor', '0x%04x' % svendor), ('subsystem_device', '0x%04x' % sdevice),
                 ('numa_node', str(numa)), ('current_link_speed', speed), ('current_link_width', str(width)),
                 ('max_link_speed', maxspeed), ('max_link_width', str(maxwidth))):
        wr(base + '/' + k, v + '\n')
    cfg = bytearray(64)
    struct.pack_into('<HH', cfg, 0, vendor, device)
    struct.pack_into('<HH', cfg, 0x2c, svendor, sdevice)
    wr(base + '/config', bytes(cfg))
    if driver:
        os.makedirs('%s/sys/bus/pci/drivers/%s' % (root, driver), exist_ok=True)
        ln(base + '/driver', '../../drivers/' + driver)
    if vpd:
        wr(base + '/vpd', vpd)
    return base


def make_vpd(idstr, pn, sn):
    def large(tag, body):
        return bytes([tag, len(body) & 0xff, len(body) >> 8]) + body
    r = b''
    for kw, val in (('PN', pn), ('EC', 'A7'), ('SN', sn), ('V2', 'NV-' + pn)):
        v = val.encode()
        r += kw.encode() + bytes([len(v)]) + v
    r += b'RV\x01\x00'
    return large(0x82, idstr.encode()) + large(0x90, r) + b'\x78'


def gpu_xml(i, bdf, sp, uuid, serial, extra):
    dom, bus, dev = bdf.split(':')[0], bdf.split(':')[1], bdf.split(':')[2].split('.')[0]
    thr = dict(gpu_idle='Not Active', applications_clocks_setting='Not Active', sw_power_cap='Not Active',
               hw_slowdown='Not Active', hw_thermal_slowdown='Not Active', hw_power_brake_slowdown='Not Active',
               sync_boost='Not Active', sw_thermal_slowdown='Not Active', display_clocks_setting='Not Active')
    thr.update(extra.get('throttle', {}))
    thr_xml = ''.join('<clocks_event_reason_%s>%s</clocks_event_reason_%s>' % (k, v, k) for k, v in thr.items())
    vol_unc = extra.get('vol_unc', 0)
    return '''	<gpu id="0000%(dom)s:%(PBUS)s:%(dev)s.0">
		<product_name>%(name)s</product_name>
		<product_brand>%(brand)s</product_brand>
		<product_architecture>%(arch)s</product_architecture>
		<persistence_mode>Enabled</persistence_mode>
		<mig_mode><current_mig>Disabled</current_mig><pending_mig>Disabled</pending_mig></mig_mode>
		<serial>%(serial)s</serial>
		<uuid>%(uuid)s</uuid>
		<minor_number>%(i)d</minor_number>
		<vbios_version>%(vbios)s</vbios_version>
		<multigpu_board>No</multigpu_board>
		<board_id>0x%(pbus)s00</board_id>
		<board_part_number>%(board_pn)s</board_part_number>
		<gpu_part_number>%(pn)s</gpu_part_number>
		<gpu_fru_part_number>N/A</gpu_fru_part_number>
		<gpu_module_id>%(mod)d</gpu_module_id>
		<inforom_version><img_version>%(inforom)s</img_version><oem_object>2.1</oem_object><ecc_object>7.16</ecc_object><pwr_object>N/A</pwr_object></inforom_version>
		<gpu_reset_status><reset_required>No</reset_required><drain_and_reset_recommended>No</drain_and_reset_recommended></gpu_reset_status>
		<gsp_firmware_version>550.54.15</gsp_firmware_version>
		<pci>
			<pci_bus>%(pbus)s</pci_bus>
			<pci_device>%(dev)s</pci_device>
			<pci_domain>%(dom)s</pci_domain>
			<pci_device_id>%(devid)04X10DE</pci_device_id>
			<pci_bus_id>0000%(dom)s:%(PBUS)s:%(dev)s.0</pci_bus_id>
			<pci_sub_system_id>%(subsys)04X10DE</pci_sub_system_id>
			<pci_gpu_link_info>
				<pcie_gen><max_link_gen>%(gen)d</max_link_gen><current_link_gen>%(gen)d</current_link_gen></pcie_gen>
				<link_widths><max_link_width>16x</max_link_width><current_link_width>16x</current_link_width></link_widths>
			</pci_gpu_link_info>
			<replay_counter>0</replay_counter>
		</pci>
		<fan_speed>N/A</fan_speed>
		<performance_state>P0</performance_state>
		<clocks_event_reasons>%(thr)s</clocks_event_reasons>
		<fb_memory_usage><total>%(mem)d MiB</total><reserved>329 MiB</reserved><used>%(used)d MiB</used><free>%(free)d MiB</free></fb_memory_usage>
		<compute_mode>Default</compute_mode>
		<utilization><gpu_util>%(util)d %%</gpu_util><memory_util>41 %%</memory_util></utilization>
		<ecc_mode><current_ecc>%(ecc)s</current_ecc><pending_ecc>%(ecc)s</pending_ecc></ecc_mode>
		<ecc_errors>
			<volatile><sram_correctable>0</sram_correctable><sram_uncorrectable_parity>0</sram_uncorrectable_parity><sram_uncorrectable_secded>0</sram_uncorrectable_secded><dram_correctable>3</dram_correctable><dram_uncorrectable>%(vol_unc)d</dram_uncorrectable></volatile>
			<aggregate><sram_correctable>0</sram_correctable><sram_uncorrectable_parity>0</sram_uncorrectable_parity><sram_uncorrectable_secded>0</sram_uncorrectable_secded><dram_correctable>15</dram_correctable><dram_uncorrectable>%(vol_unc)d</dram_uncorrectable><sram_threshold_exceeded>No</sram_threshold_exceeded></aggregate>
		</ecc_errors>
		<remapped_rows><remapped_row_corr>0</remapped_row_corr><remapped_row_unc>0</remapped_row_unc><remapped_row_pending>%(rr_pending)s</remapped_row_pending><remapped_row_failure>No</remapped_row_failure></remapped_rows>
		<temperature><gpu_temp>%(temp)d C</gpu_temp><gpu_temp_tlimit>25 C</gpu_temp_tlimit><memory_temp>%(mtemp)d C</memory_temp></temperature>
		<gpu_power_readings><power_state>P0</power_state><power_draw>%(pdraw).2f W</power_draw><current_power_limit>%(maxw).2f W</current_power_limit><requested_power_limit>%(maxw).2f W</requested_power_limit><default_power_limit>%(maxw).2f W</default_power_limit><min_power_limit>200.00 W</min_power_limit><max_power_limit>%(maxw).2f W</max_power_limit></gpu_power_readings>
		<clocks><graphics_clock>1755 MHz</graphics_clock><sm_clock>1755 MHz</sm_clock><mem_clock>%(maxmem)d MHz</mem_clock><video_clock>1545 MHz</video_clock></clocks>
		<max_clocks><graphics_clock>%(maxsm)d MHz</graphics_clock><sm_clock>%(maxsm)d MHz</sm_clock><mem_clock>%(maxmem)d MHz</mem_clock><video_clock>1545 MHz</video_clock></max_clocks>
		<fabric><state>Completed</state><status>Success</status><cliqueId>4026</cliqueId><clusterUuid>N/A</clusterUuid></fabric>
		<processes><process_info><gpu_instance_id>N/A</gpu_instance_id><compute_instance_id>N/A</compute_instance_id><pid>%(pid)d</pid><type>C</type><process_name>python3</process_name><used_memory>%(used)d MiB</used_memory></process_info></processes>
		<accounted_processes></accounted_processes>
		<capabilities><egm>disabled</egm></capabilities>
	</gpu>
''' % dict(dom=dom, pbus=bus, PBUS=bus.upper(), dev=dev, i=i, mod=[2, 4, 3, 1, 7, 5, 8, 6][i], uuid=uuid, serial=serial,
           thr=thr_xml, vol_unc=vol_unc, rr_pending=extra.get('rr_pending', 'No'), temp=extra.get('temp', 61 + i),
           mtemp=70 + i, pdraw=0.87 * sp['maxw'], pid=4000 + i, used=int(sp['mem'] * 0.93),
           free=sp['mem'] - 329 - int(sp['mem'] * 0.93), **dict(DEFAULTS, **sp))


FAKE_HEAD = '''#!/usr/bin/env python3
import os, sys
D = %r
args = sys.argv[1:]
with open(os.path.join(D, 'calls.log'), 'a') as _f:
    _f.write(%r + ' ' + ' '.join(a.replace('\\n', '\\\\n') for a in args) + '\\n')
def cat(name, rc=0):
    p = os.path.join(D, name)
    if not os.path.exists(p):
        sys.exit(1)
    with open(p) as f:
        sys.stdout.write(f.read())
    sys.exit(rc)
'''

FAKE_BODY = {
    'nvidia-smi': '''
a = ' '.join(args)
table = {'-q -x': 'smi.xml', 'topo -m': 'topo.txt', 'nvlink -s': 'nvlink_s.txt', 'nvlink -e': 'nvlink_e.txt'}
if a in table:
    cat(table[a])
sys.stderr.write('mock nvidia-smi: unsupported: %s\\n' % a)
sys.exit(2)
''',
    'dmidecode': "cat('dmidecode17.txt') if args == ['-t', '17'] else sys.exit(2)\n",
    'lsblk': "cat('lsblk.txt')\n",
    'lspci': "cat('lspci.txt')\n",
    'systemctl': "cat('fm.txt') if args[:2] == ['show', 'nvidia-fabricmanager'] else sys.exit(2)\n",
    'dpkg-query': '''
if args[:1] == ['-S'] and args[1].endswith('nvidia-smi'):
    print('nvidia-utils-550-server: ' + args[1]); sys.exit(0)
if args[:1] == ['-W'] and any('fabricmanager' in x for x in args):
    cat('fm_pkg.txt')
if args[:3] == ['-W', '-f', '${Version}'] and args[3:] == ['nvidia-utils-550-server']:
    cat('smi_pkg_ver.txt')
sys.exit(1)
''',
    'dpkg': "cat('dpkg_verify.txt') if args[:1] == ['--verify'] else sys.exit(2)\n",
    'dmesg': "cat('dmesg.txt') if not args else sys.exit(2)\n",
    'ofed_info': "cat('ofed.txt') if args == ['-s'] else sys.exit(2)\n",
    'systemd-detect-virt': "print('none'); sys.exit(1)\n",
}


def topo_text(n, link_label):
    gpu_numa = [0, 0, 0, 0, 1, 1, 1, 1]
    nics = [('mlx5_%d' % j, gpu_numa[j], j) for j in range(8)] + [('mlx5_8', 0, None), ('mlx5_9', 0, None)]
    aff = {0: '0-55,112-167', 1: '56-111,168-223'}
    cols = ['GPU%d' % i for i in range(n)] + ['NIC%d' % j for j in range(len(nics))] + \
           ['CPU Affinity', 'NUMA Affinity', 'GPU NUMA ID']
    out = ['\t' + '\t'.join(cols)]

    def rel(numa_a, numa_b, same):
        return 'PIX' if same else ('NODE' if numa_a == numa_b else 'SYS')

    for i in range(n):
        row = ['GPU%d' % i] + [' X ' if i == k else link_label for k in range(n)]
        row += [rel(gpu_numa[i], nn, pg == i) for _, nn, pg in nics]
        row += [aff[gpu_numa[i]], str(gpu_numa[i]), 'N/A']
        out.append('\t'.join(row))
    for j, (_, nn, pg) in enumerate(nics):
        row = ['NIC%d' % j] + [rel(nn, gpu_numa[i], pg == i) for i in range(n)]
        row += [' X ' if j == k else ('PIX' if (j >= 8 and k >= 8) else rel(nn, nics[k][1], False))
                for k in range(len(nics))]
        out.append('\t'.join(row))
    out += ['', 'Legend:', '', '  X    = Self',
            '  SYS  = Connection traversing PCIe as well as the SMP interconnect between NUMA nodes (e.g., QPI/UPI)',
            '  NODE = Connection traversing PCIe as well as the interconnect between PCIe Host Bridges within a NUMA node',
            '  PHB  = Connection traversing PCIe as well as a PCIe Host Bridge (typically the CPU)',
            '  PXB  = Connection traversing multiple PCIe bridges (without traversing the PCIe Host Bridge)',
            '  PIX  = Connection traversing at most a single PCIe bridge',
            '  NV#  = Connection traversing a bonded set of # NVLinks', '', 'NIC Legend:', '']
    out += ['  NIC%d: %s' % (j, nm) for j, (nm, _, _) in enumerate(nics)]
    return '\n'.join(out) + '\n'


def build(scenario, out):
    if os.path.exists(out):
        shutil.rmtree(out)
    root, data, binp = out + '/root', out + '/data', out + '/bin'
    os.makedirs(data)
    os.makedirs(binp)
    hw = dict(DEFAULTS, **SPEC[HW_OF.get(scenario, scenario if scenario in SPEC else 'h100')])
    geforce = hw['brand'] == 'GeForce'
    pcie = scenario == 'h100pcie' or geforce or not hw['links']     # 无 NVSwitch、无 Fabric Manager
    smi_spec = SPEC[SMI_OF[scenario]] if scenario in SMI_OF else hw
    driver = scenario != 'nodriver'
    deg = scenario == 'degraded'

    # ---------------- /etc /proc 基础信息
    wr(root + '/etc/os-release', 'NAME="Ubuntu"\nVERSION="22.04.4 LTS (Jammy Jellyfish)"\nPRETTY_NAME="Ubuntu 22.04.4 LTS"\n')
    wr(root + '/proc/sys/kernel/hostname', 'gpu-node-01\n')
    wr(root + '/proc/sys/kernel/osrelease', '5.15.0-105-generic\n')
    wr(root + '/proc/sys/kernel/arch', 'x86_64\n')
    wr(root + '/proc/uptime', '1234567.89 99999.00\n')
    wr(root + '/proc/1/cgroup', '0::/init.scope\n')
    cpu = []
    for p in range(224):
        sock = (p % 112) // 56
        cpu.append('processor\t: %d\nvendor_id\t: GenuineIntel\nmodel name\t: Intel(R) Xeon(R) Platinum 8480+\n'
                   'physical id\t: %d\nsiblings\t: 112\ncpu cores\t: 56\nflags\t\t: fpu vme sse sse2 avx512f amx_tile\n'
                   % (p, sock))
    wr(root + '/proc/cpuinfo', '\n'.join(cpu))
    wr(root + '/proc/meminfo', 'MemTotal:       2113523840 kB\nMemFree:        1500000000 kB\n')
    mods = ['mlx5_ib', 'mlx5_core', 'ib_uverbs', 'ib_core', 'igb']
    if driver:
        mods = (['nvidia_peermem'] if not geforce else []) + ['nvidia_uvm', 'nvidia_drm', 'nvidia_modeset', 'nvidia'] + mods
    wr(root + '/proc/modules', ''.join('%s 1000 0 - Live 0x0000000000000000\n' % m for m in mods))
    for k, v in (('sys_vendor', 'Supermicro'), ('product_name', 'SYS-821GE-TNHR'), ('product_serial', 'S7890123456'),
                 ('board_vendor', 'Supermicro'), ('board_name', 'X13DEG-OAD'),
                 ('bios_vendor', 'American Megatrends International, LLC.'), ('bios_version', '2.1'),
                 ('bios_date', '01/16/2024')):
        wr(root + '/sys/class/dmi/id/' + k, v + '\n')
    os.makedirs(root + '/sys/devices/system/node/node0')
    os.makedirs(root + '/sys/devices/system/node/node1')

    # ---------------- GPU / NVSwitch PCI 设备
    uuids = ['GPU-%08x-1111-2222-3333-%012x' % (0xa1b2c3d0 + i, 0x1000 + i) for i in range(8)]
    serials = ['16549220%05d' % (12340 + i) for i in range(8)]
    for i, bdf in enumerate(GPU_BDFS):
        pci_dev(root, bdf, 0x10de, hw['devid'], 0x030200, 0x10de, hw['subsys'], 0 if i < 4 else 1,
                LINK[hw['gen']], 16, LINK[hw['gen']], 16, driver='nvidia' if driver else None)
        if driver:
            wr('%s/proc/driver/nvidia/gpus/%s/information' % (root, bdf),
               'Model: \t\t %s\nIRQ:   \t\t 0\nGPU UUID: \t %s\nVideo BIOS: \t 96.00.99.00.01\nBus Type: \t PCIe\n'
               'DMA Size: \t 52 bits\nBus Location: \t %s\nDevice Minor: \t %d\nGPU Excluded:\t No\n'
               % (hw['name'], uuids[i], bdf, i))
    for bdf in ([] if pcie else NVSW_BDFS):
        pci_dev(root, bdf, 0x10de, 0x22a3, 0x068000, 0x10de, 0x1796, 0, '16.0 GT/s PCIe', 2, '16.0 GT/s PCIe', 2,
                driver='nvidia-nvswitch' if driver else None)
    if driver:
        wr(root + '/proc/driver/nvidia/version',
           'NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  550.54.15  Release Build  '
           '(dvs-builder@U16-I3-B03-4-3)  Tue Mar  5 22:15:33 UTC 2024\nGCC version:  gcc version 12.3.0\n')
        wr(root + '/sys/module/nvidia/taint', 'OE\n')
        wr(root + '/sys/module/nvidia/srcversion', '4B3D3E6CF5C6B7A8D9E0F11\n')

    # ---------------- IB（8x CX7 NDR）+ RoCE（CX6 Dx 双口）
    cx7_vpd = 'NVIDIA ConnectX-7 HHHL Adapter Card, 400GbE / NDR IB (default mode), Single-port OSFP, PCIe 5.0 x16'
    for j, bdf in enumerate(IB_BDFS + ROCE_BDFS):
        ib = j < 8
        dev = 'mlx5_%d' % j
        cur = '16.0 GT/s PCIe' if (deg and j == 7) else ('32.0 GT/s PCIe' if ib else '16.0 GT/s PCIe')
        base = pci_dev(root, bdf, 0x15b3, 0x1021 if ib else 0x101d, 0x020700 if ib else 0x020000, 0x15b3,
                       0x0041 if ib else 0x0016, 0 if (j < 4 or j >= 8) else 1, cur, 16,
                       '32.0 GT/s PCIe' if ib else '16.0 GT/s PCIe', 16, driver='mlx5_core',
                       vpd=make_vpd(cx7_vpd, 'MCX75310AAS-NEAT', 'MT2333XZ0%03d' % j) if ib else
                       make_vpd('ConnectX-6 Dx EN adapter card, 100GbE, Dual-port QSFP56, PCIe 4.0 x16',
                                'MCX623106AN-CDAT', 'MT2215X1%04d' % j))
        ifname = ('ibp%ss0' % int(bdf.split(':')[1], 16)) if ib else ('ens1f%dnp%d' % (j - 8, j - 8))
        os.makedirs(base + '/net/' + ifname)
        ib_base = root + '/sys/class/infiniband/' + dev
        ln(ib_base + '/device', '../../../bus/pci/devices/' + bdf)
        for k, v in (('fw_ver', '28.39.2048' if ib else '22.39.2048'), ('hca_type', 'MT4129' if ib else 'MT4125'),
                     ('board_id', 'MT_0000000838' if ib else 'MT_0000000359'),
                     ('node_guid', 'a088:c203:0012:%04x' % j), ('node_type', '1: CA')):
            wr(ib_base + '/' + k, v + '\n')
        down = deg and j == 4
        pb = ib_base + '/ports/1'
        wr(pb + '/state', '1: DOWN\n' if down else '4: ACTIVE\n')
        wr(pb + '/phys_state', '2: Polling\n' if down else '5: LinkUp\n')
        wr(pb + '/rate', ('400 Gb/sec (4X NDR)' if ib else '100 Gb/sec (4X EDR)') + '\n')
        wr(pb + '/link_layer', ('InfiniBand' if ib else 'Ethernet') + '\n')
        wr(pb + '/lid', '0x%x\n' % (0x20 + j) if ib else '0x0\n')
        wr(pb + '/sm_lid', '0x1\n')
        for cn in ('symbol_error', 'port_rcv_errors', 'link_downed', 'link_error_recovery',
                   'local_link_integrity_errors', 'excessive_buffer_overrun_errors'):
            wr(pb + '/counters/' + cn, ('3' if (deg and j == 4 and cn == 'link_downed') else '0') + '\n')
        nb = root + '/sys/class/net/' + ifname
        ln(nb + '/device', '../../../bus/pci/devices/' + bdf)
        wr(nb + '/operstate', ('down' if down else 'up') + '\n')
        wr(nb + '/mtu', ('4092' if ib else '9000') + '\n')
        wr(nb + '/speed', ('-1' if down else ('400000' if ib else '100000')) + '\n')
        wr(nb + '/type', ('32' if ib else '1') + '\n')
    for j, bdf in enumerate(MGMT_BDFS):
        pci_dev(root, bdf, 0x8086, 0x1521, 0x020000, 0x15d9, 0x1521, 1, '5.0 GT/s PCIe', 4, '5.0 GT/s PCIe', 4,
                driver='igb')
        nb = root + '/sys/class/net/eno%d' % (j + 1)
        ln(nb + '/device', '../../../bus/pci/devices/' + bdf)
        wr(nb + '/operstate', ('up' if j == 0 else 'down') + '\n')
        wr(nb + '/mtu', '1500\n')
        wr(nb + '/speed', ('1000' if j == 0 else '-1') + '\n')
        wr(nb + '/type', '1\n')
    os.makedirs(root + '/sys/class/net/lo')
    wr(root + '/sys/class/net/lo/operstate', 'unknown\n')

    # ---------------- 假命令的数据
    if driver:
        extra = {}
        if deg:
            extra = {1: {'throttle': {'hw_slowdown': 'Active'}}, 2: {'rr_pending': 'Yes'}, 5: {'vol_unc': 1}}
        xml = ['<?xml version="1.0" ?>', '<!DOCTYPE nvidia_smi_log SYSTEM "nvsmi_device_v12.dtd">', '<nvidia_smi_log>',
               '\t<timestamp>Mon Nov 17 11:30:00 2026</timestamp>', '\t<driver_version>550.54.15</driver_version>',
               '\t<cuda_version>12.4</cuda_version>', '\t<attached_gpus>8</attached_gpus>']
        for i, bdf in enumerate(GPU_BDFS):
            xml.append(gpu_xml(i, bdf, smi_spec, uuids[i], serials[i], extra.get(i, {})))
        xml.append('</nvidia_smi_log>\n')
        wr(data + '/smi.xml', '\n'.join(xml))
        wr(data + '/topo.txt', topo_text(8, 'NV%d' % smi_spec['links'] if smi_spec['links'] else 'NODE'))
        s_lines, e_lines = [], []
        for i in range(8):
            head = 'GPU %d: %s (UUID: %s)' % (i, smi_spec['name'], uuids[i])
            s_lines.append(head)
            e_lines.append(head)
            for ln_ in range(hw['links']):
                inactive = deg and i == 3 and ln_ == 17
                s_lines.append('\t Link %d: %s' % (ln_, '<inactive>' if inactive else '26.562 GB/s'))
                rep = 12 if (deg and i == 3 and ln_ == 16) else 0
                e_lines += ['\t Link %d: Replay Errors: %d' % (ln_, rep), '\t Link %d: Recovery Errors: 0' % ln_,
                            '\t Link %d: CRC Errors: 0' % ln_]
        wr(data + '/nvlink_s.txt', '\n'.join(s_lines) + '\n')
        wr(data + '/nvlink_e.txt', '\n'.join(e_lines) + '\n')
        # NVML / CUDA 模拟数据（反映“真实”硬件；h20-fake 场景下与被篡改的 nvidia-smi 不同）
        conf = []
        remote = [NVSW_BDFS[0]] * 5 + [NVSW_BDFS[1]] * 4 + [NVSW_BDFS[2]] * 4 + [NVSW_BDFS[3]] * 5
        for i, bdf in enumerate(GPU_BDFS):
            active = hw['links'] - (1 if (deg and i == 3) else 0)
            conf.append('|'.join([bdf, '0x%04x10de' % hw['devid'], '0x%04x10de' % hw['subsys'], hw['name'],
                                  str(hw['mem']), str(hw['cores']), str(hw['bus']), str(hw['maxw']),
                                  str(hw['maxsm']), str(hw['maxmem']), hw['cc'], str(hw['links']), str(active),
                                  serials[i], uuids[i], ','.join(remote[:hw['links']])]))
        wr(out + '/nvml.conf', '\n'.join(conf) + '\n')
    dimm = []
    for k in range(32):
        dimm.append('Handle 0x%04X, DMI type 17, 92 bytes\nMemory Device\n\tSize: 64 GB\n\tForm Factor: DIMM\n'
                    '\tLocator: P%d-DIMM%s%d\n\tType: DDR5\n\tSpeed: 4800 MT/s\n\tManufacturer: Samsung\n'
                    '\tSerial Number: 8000%04d\n\tPart Number: M321R8GA0BB0-CQKZJ    \n\tRank: 2\n'
                    '\tConfigured Memory Speed: 4400 MT/s\n' % (0x26 + k, k // 16, 'ABCDEFGH'[(k % 16) // 2], k % 2, k))
    wr(data + '/dmidecode17.txt', '# dmidecode 3.3\nGetting SMBIOS data from sysfs.\n\n' + '\n'.join(dimm))
    disks = ['NAME="nvme%dn1" MODEL="SAMSUNG MZQL23T8HCLS-00A07" SIZE="3840755982336" TYPE="disk" TRAN="nvme" ROTA="0"'
             % k for k in range(8)]
    disks.append('NAME="nvme8n1" MODEL="SAMSUNG MZ1L2960HCJR-00A07" SIZE="960197124096" TYPE="disk" TRAN="nvme" ROTA="0"')
    disks.append('NAME="loop0" MODEL="" SIZE="67108864" TYPE="loop" TRAN="" ROTA="0"')
    wr(data + '/lsblk.txt', '\n'.join(disks) + '\n')
    lspci = ['%s "Ethernet controller [0200]" "Intel Corporation [8086]" "I350 Gigabit Network Connection [1521]" '
             '-r01 "Super Micro Computer Inc [15d9]" "Device [1521]"' % b for b in MGMT_BDFS]
    if scenario == 'pciids':
        lspci += ['%s "3D controller [0302]" "NVIDIA Corporation [10de]" "AD102 [GeForce RTX 4090] [26ff]" -ra1 '
                  '"NVIDIA Corporation [10de]" "Device [167c]"' % b for b in GPU_BDFS]
    wr(data + '/lspci.txt', '\n'.join(lspci) + '\n')
    wr(data + '/fm.txt', 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n' if pcie else
       'LoadState=loaded\nActiveState=%s\nSubState=%s\n' % (('inactive', 'dead') if deg else ('active', 'running')))
    if not pcie:
        wr(data + '/fm_pkg.txt', 'ii  nvidia-fabricmanager-550 550.54.15-1\n')
    smi_real = os.path.realpath(binp + '/nvidia-smi')
    wr(data + '/dpkg_verify.txt', ('??5??????   %s\n' % smi_real) if scenario in ('h20-fake', 'rtx4090d-fake', 'runfile') else '')
    wr(data + '/smi_pkg_ver.txt', '535.161.08-0ubuntu2.22.04.1' if scenario == 'runfile' else '550.54.15-0ubuntu0.22.04.1')
    dm = ['[    0.000000] Linux version 5.15.0-105-generic', '[   12.345678] nvidia: loading out-of-tree module']
    if deg:
        dm += ["[86400.123456] NVRM: Xid (PCI:0000:ba:00): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus.",
               "[90000.000001] NVRM: Xid (PCI:0000:5d:00): 13, pid=4321, name=python3, Graphics SM Warp Exception"]
    wr(data + '/dmesg.txt', '\n'.join(dm) + '\n')
    wr(data + '/ofed.txt', 'MLNX_OFED_LINUX-23.10-1.1.9.0:\n')

    for name, body in FAKE_BODY.items():
        if name == 'nvidia-smi' and not driver:
            continue
        wr(binp + '/' + name, FAKE_HEAD % (data, name) + body, 0o755)
    open(data + '/calls.log', 'w').close()


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(sys.argv[1], os.path.abspath(sys.argv[2]))
