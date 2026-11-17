# gpu-check — GPU 服务器只读检测与真伪核验

单文件脚本 `gpu_check.py`，只用 Python 3.6+ 标准库，不需要 pip 安装任何东西。在 GPU 服务器上运行，它会：

- **收集硬件配置**：整机、CPU、内存、存储、GPU、NVLink 拓扑、IB / RDMA 网卡，在终端输出表格报告；
- **核验 GPU 真伪**：按芯片熔丝决定的设备 ID 识别型号，再与驱动、NVML、nvidia-smi 报告的名称和规格指纹交叉比对。能识别 H20 / H800 冒充 H100、RTX 4090 D 冒充 4090、RTX PRO 6000D 冒充 RTX PRO 6000、矿卡冒充计算卡，以及 nvidia-smi 被篡改等情况；
- **检查健康状况**：NVLink 掉链、IB 端口、PCIe 降速、Xid、ECC、行重映射等。

型号库覆盖 Pascal 到 Blackwell 的 235 个设备 ID，包括数据中心卡、专业卡、GeForce 和矿卡，见「支持的型号」。

**检测全程只读，不影响线上业务**：不创建 CUDA 上下文、不占显存、不跑任何负载或带宽测试、不重置计数器、不改任何配置。详见下面的「对线上业务的影响」一节。

> 本项目原名 `h100-check`，v1.3.0 及以前的脚本叫 `h100_check.py`。旧的 GitHub 地址会自动跳转到新仓库，旧的一键命令、中转地址和环境变量名照常可用。

## 一键运行

在 GPU 服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash
```

不加参数时默认按 **H100 SXM5** 核验，也就是国内常说的「NVLink 版」（HGX 8 卡 / 4 卡机型），PCIe 版和 NVL 版会判为「形态不符」。

其它机器用 `--expect` 指定期望型号，参数接在 `bash -s --` 后面，`-o` 可以把报告另存一份：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash -s -- --expect 4090 -o /tmp/gpu_report.txt
curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash -s -- --expect pro6000d
curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash -s -- --expect h100-pcie
```

不确定型号时，用 `--expect any` 只识别型号，并检查各数据源是否一致、规格是否吻合：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/main/run.sh | sudo bash -s -- --expect any
```

`run.sh` 只做三件事：找到 Python 3.6+，把 `gpu_check.py` 下载到内存并检查是否完整，再交给 Python 执行。除了 `-o` 指定的报告文件，不在磁盘上留任何东西。不加 `sudo` 也能跑，但会缺少内存条型号、网卡序列号和料号，以及 dmesg 里的 Xid 记录。

要固定版本、不受仓库后续更新影响，就直接运行打了标签的版本：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/gpu-check/v1.4.0/gpu_check.py | sudo python3 -
```

### 服务器访问不了 GitHub 时

国内机房经常连不上 `raw.githubusercontent.com`，有三种办法：

1. **用中转站**（推荐，见下面「自建中转」一节）。在 GPU 服务器上：

   ```bash
   curl -fsSL http://<中转站>/gpu/run.sh | sudo bash
   ```

2. **ssh 管道**：在能上网的机器上下载一次，再通过 ssh 管道执行。目标机不需要外网，也不会留下文件：

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/rivia7/gpu-check/main/gpu_check.py
   ssh gpu-node 'sudo python3 - --no-color' < gpu_check.py > gpu-node.txt

   # 批量检查，按退出码汇总
   for h in node01 node02 node03; do
     ssh "$h" 'sudo python3 - --no-color' < gpu_check.py > "report-$h.txt"; echo "$h rc=$?"
   done
   ```

3. **内网 HTTP 服务器**：把 `gpu_check.py` 放在 `<地址>/main/gpu_check.py`，用 `GPU_CHECK_BASE` 指定根地址（或用 `GPU_CHECK_URL` 直接指定脚本的完整地址）：

   ```bash
   curl -fsSL http://<内网地址>/main/run.sh | sudo env GPU_CHECK_BASE=http://<内网地址> bash
   ```

## 参数

| 参数 | 说明 |
|---|---|
| `-o FILE` | 把报告另存一份到文件（不含颜色码） |
| `--expect h100-sxm` | 默认值。必须是 H100 SXM5，即「NVLink 版」（HGX 机型），PCIe 版和 NVL 版判为「形态不符」。`h100-nvlink` 是同义写法 |
| `--expect h100` | H100 的任何形态（SXM5 / PCIe / NVL）都算通过 |
| `--expect h100-pcie` / `h100-nvl` | 必须是 PCIe 版 / NVL 版。注意 H100 NVL（设备 ID `0x2321`）是用 NVLink 桥连接的 PCIe 双卡，不是上面说的「NVLink 版」。形态后缀对其它型号同样适用（如 `a100-sxm`） |
| `--expect <型号>` | 期望其它型号，如 `h20`、`a100`、`a800`、`l40s`、`l20`、`4090`、`4090d`、`5090`、`pro6000`、`pro6000d`（也可写 `6000d`）、`a6000`、`6000ada`、`1080ti`、`p6000`、`t1000`、`cmp170hx`。大小写、空格、`rtx` / `gtx` 前缀都可省略 |
| `--expect any` | 不限型号，只检查各数据源是否一致、规格是否与识别出的型号吻合（比如用来检查 H20 机器本身是否正常） |
| `--expect-gpus N` | 期望的 GPU 张数。HGX 8 卡机型（4 颗 NVSwitch）会自动按 8 张检查 |
| `--cuda` | 额外通过 CUDA 驱动 API 读取 SM 数、显存位宽等属性，多一路交叉验证。只调用 `cuInit` 和属性查询，不创建上下文、不占显存。默认关闭 |
| `--no-color` | 关闭颜色（输出被重定向时会自动关闭） |

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 通过：GPU 确认为期望的型号，没有健康或配置问题 |
| 1 | 有告警：GPU 是真的，但有健康/配置问题（NVLink 掉链、IB 端口 down、Xid、ECC、改装显存等）；或真伪暂时无法确认（如驱动未加载） |
| 2 | 不通过：GPU 不是期望的型号（如期望 H100 实为 H20、期望 4090 实为 4090 D）或形态不符（默认期望 SXM5，实为 PCIe 版），或发现数据被篡改的证据 |
| 3 | 运行错误（非 Linux、找不到 Python、`--expect` 型号无法识别等） |

## 对线上业务的影响

| 会做 | 不会做 |
|---|---|
| 读 `/sys`、`/proc` 下的文件（PCI 配置空间头部、IB 端口状态和计数器、网卡 VPD） | 不创建 CUDA 上下文、不分配显存、不发起任何 GPU 计算 |
| 执行只读查询：`nvidia-smi -q -x`、`nvidia-smi topo -m`、`nvidia-smi nvlink -s`、`nvidia-smi nvlink -e` | 不跑 NCCL、`ib_write_bw`、`nvbandwidth`、DCGM 诊断等压测或带宽测试 |
| 通过 NVML 直连读取设备信息（只用 `nvmlInit`、`nvmlShutdown` 和 `nvmlDeviceGet*` 查询函数） | 不重置 NVLink/IB 错误计数（不用 `nvlink -r/-re`、`perfquery -R`） |
| `dmidecode -t 17`、`lspci -D -mm -nn`、`lsblk`、`lscpu`、`dmesg`/`journalctl -k`、`systemctl show`、`ofed_info -s`、`dpkg --verify`/`rpm -V`（只比对校验和） | 不改时钟、功耗墙、ECC、MIG、持久模式、计算模式，不动 Fabric Manager 等服务 |
| 以 `nice 19` + `ionice idle` 低优先级运行，每条外部命令都有超时（最长 120 秒） | 除 `-o` 指定的报告文件外，不写任何文件；只读取 `dmesg`，不清空内核日志缓冲区（不用 `-c`） |

`--cuda` 打开后会多调用一次 `cuInit`：它只初始化本进程的驱动接口，不在 GPU 上创建上下文，也不占显存。正常检测不需要打开它。

## 报告内容

1. **系统概览**：机型、序列号、BIOS、OS/内核、NVIDIA 驱动（区分开源/闭源内核模块）、CUDA 版本、GSP 固件、Fabric Manager、OFED
2. **CPU / 内存 / 存储**：CPU 型号与路数、NUMA、内存条型号/速率/槽位、NVMe 盘
3. **GPU 清单**：型号、显存、功耗墙、最大频率、PCIe 能力、NUMA、VBIOS、序列号、模块号、板卡料号、InfoROM、UUID、持久模式/MIG/ECC
4. **真伪核验**：每张卡逐项比对硬件 ID 和规格指纹，另有工具链完整性检查（见下节）
5. **NVLink 拓扑**：`nvidia-smi topo -m` 互联矩阵（含 CPU 亲和）、每卡链路激活数/速率/错误计数/未激活链路、通过 NVML 读到的各卡到每颗 NVSwitch 的链路分布、NVSwitch 数量、Fabric Manager 状态
6. **IB / RDMA 网卡**：型号、料号(PN)、序列号、固件、PSID、PCIe 当前/能力速率、端口状态/速率/LID、链路错误计数，以及每张 GPU 最近的 RDMA 网卡（GPUDirect RDMA 亲和）
7. **其它网卡**：管理网卡等
8. **GPU 运行状态快照**：温度、功耗、利用率、显存占用、进程数、ECC、行重映射、降频原因、Xid
9. **检测结论**：按类别汇总，列出「需关注」项，给出总体结论

## 真伪判定原理

结论来自几层互相独立的数据源，彼此必须一致：

1. **硬件层**：PCI 设备 ID 由 GPU 芯片熔丝决定，改名字、贴标签、改 nvidia-smi 都改不了。脚本既读内核缓存的 ID，也直接读 PCI 配置空间，两者对比。
2. **驱动层**：`/proc/driver/nvidia` 里的型号、NVML 直连读到的型号和 PCI ID（绕过 nvidia-smi 程序，直接调 `libnvidia-ml.so`）、nvidia-smi 的输出，三者都必须和硬件 ID 对得上；nvidia-smi 和 NVML 报的显存、功耗墙、频率、序列号也要一致。
3. **规格指纹**：SM 数（NVML 核心数 ÷ 每 SM 核心数）、显存位宽、显存容量、最大功耗墙、最大频率、NVLink 链路数、架构与计算能力、GPU 部件号前缀，与该型号的标准值比对。
4. **工具链**：nvidia-smi 是否为原厂 ELF、包管理器校验和是否一致、驱动各组件版本是否一致、`/etc/ld.so.preload` 和 `LD_PRELOAD` 劫持。

关键项（硬件 ID、型号名、SM 数、显存位宽、显存容量、架构、nvidia-smi 与 NVML 一致性）有矛盾时判「不通过」或「待确认」：H100 SXM5/PCIe 这类参考值可信度高的型号直接判不通过，其它型号先判待确认。功耗墙、频率、部件号这类非关键项有偏差时只告警，不影响真伪结论。

常见设备 ID（来源：pci.ids 与 NVIDIA 驱动 README 的支持列表）：

| 设备 ID | 型号 | 说明 |
|---|---|---|
| `0x2330` | H100 SXM5 80GB | 即「NVLink 版」，HGX H100 8 卡机最常见；默认按它核验 |
| `0x2331` | H100 PCIe 80GB | |
| `0x2321` | H100 NVL 94GB | 用 NVLink 桥连接的 PCIe 双卡，不是 SXM5 的「NVLink 版」 |
| `0x2337` / `0x2338` / `0x2339` | H100 SXM5 64GB / 96GB / 94GB | |
| `0x2329` | **H20 96GB** | 中国特供版，算力大幅阉割 |
| `0x2328` / `0x232c` | **H20B / H20-3e** | 同上 |
| `0x2324` / `0x2322` / `0x233a` | **H800** SXM5 / PCIe / NVL | 特供版，NVLink 只有 8 条 |
| `0x2335` / `0x233b` | H200 SXM / NVL | 比 H100 更高规格，判为「非 H100」但不算降级 |
| `0x20b2` / `0x20f3` | A100 SXM4 80GB / **A800** SXM4 80GB | A800 的 NVLink 为 8 条（A100 为 12 条） |
| `0x26b9` / `0x26ba` | L40S / **L20** | L20 为特供版 |
| `0x2684` / `0x2685` | RTX 4090 / **RTX 4090 D** | 4090 D 为特供版，114 个 SM（4090 为 128 个） |
| `0x2b85` / `0x2b87` / `0x2b8c` | RTX 5090 / **5090 D** / **5090 D V2** | 5090 D V2 只有 24GB、384-bit |
| `0x2bb1` / `0x2bb4` / `0x2bb5` | RTX PRO 6000 Blackwell 工作站版 / Max-Q / 服务器版 | 188 个 SM、512-bit、96GB |
| `0x2bb9` | **RTX PRO 6000D** 服务器版 | 特供版，驱动里叫「NVIDIA RTX 6000D」；156 个 SM、448-bit、84GB |
| `0x20c2` | **CMP 170HX** | 矿卡，和 A100 同为 GA100 核心，但只有 70 个 SM（A100 为 108 个）、8GB 显存 |

## 支持的型号

内置型号库（`GPU_DB`）按设备 ID 收录了 235 个 ID，覆盖 Pascal 到 Blackwell 的台式机和服务器 GPU。每个 ID 的官方名称都与 NVIDIA 驱动的支持列表核对过，规格的来源见下一节：

- **数据中心**：H100 / H800 / H20 / H200 / GH200，B200 / B100 / GB200 / B300 / GB300，A100 / A800 / A30 / A40 / A10 / A10G / A10M / A2 / A16，L40S / L40 / L20 / L4 / L2，V100 / V100S / T4 / P100 / P40 / P4
- **专业卡**：RTX PRO 6000 / 6000D / 5500 / 5000 / 4500 / 4000 / 2000 Blackwell，RTX 6000 / 5880 / 5000 / 4500 / 4000 / 2000 Ada，RTX A6000 / A5500 / A5000 / A4500 / A4000 / A2000 / A1000 / A400，Quadro RTX 8000 / 6000 / 5000 / 4000，T1000 / T600 / T400，Quadro GV100，Quadro P6000 / P5000 / P4000 / P2200 / P2000 / P1000 / P620 / P600 / P400
- **消费级**：GeForce RTX 5090 / 5090 D / 5090 D V2 / 5080 / 5070 Ti / 5070 SUPER / 5070 / 5060 Ti / 5060 / 5050，RTX 4090 / 4090 D / 4080 SUPER / 4080 / 4070 Ti SUPER / 4070 Ti / 4070 SUPER / 4070 / 4060 Ti / 4060，RTX 3090 Ti / 3090 / 3080 Ti / 3080 / 3070 Ti / 3070 / 3060 Ti / 3060 / 3050，RTX 2080 Ti / 2080 SUPER / 2080 / 2070 SUPER / 2070 / 2060 SUPER / 2060，GTX 1660 Ti / 1660 SUPER / 1660 / 1650 SUPER / 1650 / 1630，GTX 1080 Ti / 1080 / 1070 Ti / 1070 / 1060 / 1050 Ti / 1050，GT 1030 / 1010，TITAN RTX / TITAN V / TITAN Xp / TITAN X (Pascal)
- **矿卡**：CMP 170HX / 90HX / 70HX / 50HX / 40HX / 30HX，P104-100 / P104-101 / P106-100 / P106-090。这些卡没有显示输出、PCIe 带宽受限，期望别的型号时，报告会注明是「挖矿专用卡」

型号库里没有的设备 ID，会退回用本机的 pci.ids（`lspci`）识别，再与驱动、NVML、nvidia-smi 报告的名称核对，报告里会注明「按本机 pci.ids 识别」，可信度低于内置型号。
本机 pci.ids 也不认识时（系统自带的 pci.ids 往往比新卡旧），只能参考驱动报告的名称：名称明确是别的型号，判「不是期望型号」；名称与期望相符，也只能判「待确认」，因为驱动名称可能被篡改。遇到这种情况请反馈设备 ID，补进型号库。

不同类型的卡，检查侧重不同：

- **消费级卡**的功耗墙和最大频率随板卡厂商不同，不做比对；ECC 默认关闭属正常，不告警；不支持 GPUDirect RDMA，不提示 `nvidia_peermem`。
- **改装大显存**（如 48GB 的 RTX 4090、22GB 的 RTX 2080 Ti）：显存明显大于官方规格时，结论为「真 RTX 4090(改装48G显存)」并给出告警。芯片是真的，但板卡不是原厂设计，要留意稳定性和保修。
- **数据中心卡**关闭 ECC 时告警。GDDR 显存开 ECC 会占掉 1/16 容量，两种容量都按标准处理。

## 型号库的来源与核实

网上找过现成的 GPU 数据库，没有能直接用的。真伪核验要把「熔丝设备 ID」和「SM 数、显存位宽、显存容量、功耗墙」对应起来，现有项目要么只有设备 ID 和名称，要么只有规格、没有设备 ID：

| 项目 | 内容 | 缺什么 |
|---|---|---|
| NVIDIA 驱动 README 的[支持列表](https://download.nvidia.com/XFree86/Linux-x86_64/620.70.06/README/supportedchips.html) | 官方的设备 ID 与名称 | 没有规格 |
| [pciutils/pciids](https://github.com/pciutils/pciids) | 设备 ID、名称和芯片代号，社区维护 | 没有规格 |
| [chaneyzorn/nvidia-gpu-hwdata](https://github.com/chaneyzorn/nvidia-gpu-hwdata) | 从驱动支持列表整理的设备 ID 与名称 | 没有规格，2024 年后没有更新 |
| [dbgpu](https://github.com/painebenjamin/dbgpu)（数据来自 TechPowerUp） | 1400 多款 NVIDIA GPU 的详细规格 | 没有设备 ID，个别数值有误（见下表） |
| [RightNow-AI/RightNow-GPU-Database](https://github.com/RightNow-AI/RightNow-GPU-Database) | 同样来自 TechPowerUp，JSON 格式 | 没有设备 ID |
| [voidful/gpu-info-api](https://github.com/voidful/gpu-info-api) | 从维基百科抓取的规格表 | 没有设备 ID，字段不统一 |
| [gmasse/gpu-specs](https://github.com/gmasse/gpu-specs)、Hugging Face 上的 [Jr23xd23/gpu-database](https://huggingface.co/datasets/Jr23xd23/gpu-database) | 面向 AI 选型的规格表 | 没有设备 ID |

所以型号库是把这几类数据拼起来的，规则如下：

- **设备 ID 和名称**：以 NVIDIA 驱动的支持列表为准；驱动没收录的 ID 取自 pci.ids。
- **规格**：取自 NVIDIA 数据表和产品页，再与 TechPowerUp（dbgpu）、维基百科交叉核对。有两个来源一致，或有官方的明确数值，才写入；拿不准的留空，这类型号只按设备 ID 识别，不比对规格。
- **范围**：只收 Pascal 及以后的台式机和服务器 GPU。笔记本和嵌入式 GPU、工程板卡（如 PG506-232）、资料不足的新品（GB10、Rubin）暂不收录，遇到时按 pci.ids 和驱动名称识别。

2026-11-17 的核对结果：

- 驱动 620.70.06 和 580.178.04 的支持列表里，型号库 ID 对应的 229 个官方名称全部识别正确；驱动没收录的 27 个 ID 与 pci.ids 的名称一致。
- 规格与 TechPowerUp 比对，163 项一致，6 项不一致。查证后都是 TechPowerUp 的数值有误：

| 型号 | TechPowerUp | 型号库（依据） |
|---|---|---|
| V100 SXM2 16GB / 32GB | 250W | 300W（NVIDIA V100 数据表） |
| V100 PCIe 16GB | 300W | 250W（NVIDIA V100 数据表） |
| A10G | 72 个 SM、150W | 80 个 SM、300W（NVIDIA/AWS 的 A10G 数据表：80 个 RT 核心，每个 SM 一个） |
| L40S | 300W | 350W（NVIDIA L40S 产品页） |
| L4 | 60 个 SM | 58 个 SM（7424 个 CUDA 核心 ÷ 128） |

- 只有一个来源的规格不写入。比如 TechPowerUp 给 RTX PRO 6000D 工作站版和 Max-Q 版填的是 RTX PRO 6000 的数值，型号库只收录了从真实机器核实过的服务器版规格。

维护者可以随时复核（需要联网，GPU 服务器上用不到）：

```bash
python3 -I tools/verify_db.py            # 核对官方名称，列出官方列表里有、型号库还没有的 ID
python3 -I tools/verify_db.py --dbgpu    # 另外与 TechPowerUp 数据比对规格
```

## 局限

- **内核级伪造防不住**：如果有人改了内核（例如修改开源内核模块，或在 PCI 层伪造设备 ID），所有软件读到的数据都可能是假的。脚本会显示内核模块的 srcversion 和 taint 标志，可与同版本的正常机器比对。
- **不做算力实测**：要确认实际算力，需要在维护窗口对空闲 GPU 单独压测（比如跑 BF16 GEMM：H100 SXM5 稠密 BF16 理论约 989 TFLOPS，H20 约 148 TFLOPS）。这会占用 GPU，所以脚本有意不做。
- H20 系列等特供型号没有官方公开的完整规格，判定以硬件 ID 为主，规格只作辅助参考。型号库只写入有可靠来源的规格（见「型号库的来源与核实」），非关键项有偏差时只告警。
- H100 的检测目前只在模拟环境里测试过（见下节），还没在真实 H100 机器上跑过；RTX 4090、RTX PRO 6000D 的测试场景取自真实机器的报告。首次使用建议先在一台确定是正品的机器上跑一遍，作为基线对照。

## 自建中转

`relay/` 目录是一个 Docker Compose 服务，部署在一台能访问 GitHub 的服务器上，给访问不了 GitHub 的 GPU 服务器提供一键运行入口：

- `sync` 容器每 10 分钟从 GitHub 同步一次 main 分支和所有版本标签。它走 `codeload.github.com` 和 `api.github.com`，不依赖常被墙的 `raw.githubusercontent.com`，只发布末行完整的脚本。
- `web` 容器（nginx）对外提供 `/gpu/` 下的文件。改名前的旧路径 `/h100/` 内容相同，继续可用。
- 两个容器共用同一个 nginx 镜像。

`gpu_check.py` 与 GitHub 上的文件逐字节一致（v1.3.0 及以前的版本目录里是 `h100_check.py`）。只有 `run.sh` 被改写过：默认下载源改成中转站，标签目录里的 `run.sh` 默认运行同一版本。

部署步骤：

```bash
scp -r relay/ <中转服务器>:/opt/gpu-relay
ssh <中转服务器>
cd /opt/gpu-relay
cat > .env <<'EOF'
PUBLIC_BASE=http://<中转服务器公网 IP 或域名>/gpu
NGINX_IMAGE=nginx:1.27-alpine   # Docker Hub 拉不到时换成镜像站，如 mirror.ccs.tencentyun.com/library/nginx:1.27-alpine
EOF
docker compose up -d
curl http://127.0.0.1/gpu/      # 使用说明、可用版本、最近同步时间
```

云服务器还要在控制台的防火墙/安全组里放行 TCP 80。部署后，GPU 服务器上这样运行：

```bash
curl -fsSL http://<中转站>/gpu/run.sh | sudo bash                        # 最新版（main）
curl -fsSL http://<中转站>/gpu/run.sh | sudo bash -s -- --expect any      # 带参数
curl -fsSL http://<中转站>/gpu/v1.4.0/run.sh | sudo bash                 # 指定版本
```

中转站走 HTTP 明文。链路不可信时，可以拿 `<版本>/SHA256SUMS` 里 `gpu_check.py` 的哈希与 GitHub 上的文件比对。

## 离线测试

`tests/` 里是不需要 GPU 的回归测试：`make_fixture.py` 生成模拟的 `/sys`、`/proc` 和一组假命令，`mocknv.c` 编译成模拟的 NVML/CUDA 动态库，`run_tests.sh` 逐个场景运行并检查：

1. 退出码是否符合预期；
2. 报告里是否包含关键结论（以及不该出现的内容没有出现）；
3. 是否只调用了白名单里的只读命令（每次调用都会记录下来比对）；
4. stderr 是否为空。

```bash
bash tests/run_tests.sh                      # 默认输出到 $TMPDIR/gpu_check_tests
PYTHON=python3.9 bash tests/run_tests.sh     # 指定解释器
```

| 场景 | 期望结果 |
|---|---|
| `h100` | 正常 HGX H100 SXM5 8 卡 → 通过 |
| `h20-fake` | 硬件是 H20，nvidia-smi 被篡改成显示 H100 → 不通过（硬件 ID、PCI ID、显存、包校验多处露馅） |
| `h20` / `h800` | 诚实的 H20 / H800 机器 → 不通过 |
| `degraded` | 真 H100，但 NVLink 掉链、IB 端口 down、PCIe 降速、FM 未运行、Xid 79、行重映射待处理、ECC 错误 → 通过，有告警 |
| `nodriver` | 真 H100 硬件，但驱动未加载 → 待确认 |
| `runfile` | 真 H100，驱动用 .run 覆盖安装在旧版 deb 包之上 → 通过，有告警（不误判为篡改） |
| `h100 --expect h100-nvlink` | 同义写法 → 通过 |
| `h100pcie` | 8 张 H100 PCIe：默认（期望 SXM5）→ 形态不符，并提示加 `--expect h100-pcie`；加 `--expect h100` 或 `h100-pcie` → 通过 |
| `h20 --expect any` | 只检查一致性 → 通过 |
| `rtx4090` | 8 张 RTX 4090（取自真实机器）：默认按 H100 核验 → 不通过，并提示加 `--expect 4090`；`--expect any` / `--expect 4090` → 通过，不报 ECC 关闭、peermem |
| `rtx4090d --expect 4090` | 诚实的 4090 D 机器 → 不通过（非 RTX 4090） |
| `rtx4090d-fake --expect 4090` | 硬件是 4090 D，nvidia-smi 被篡改成显示 4090 → 不通过 |
| `rtx4090-48g --expect 4090` | 改装 48GB 显存的 4090 → 通过，有告警（改装大显存） |
| `pciids --expect any` | 设备 ID 不在型号库里，按本机 pci.ids 识别 → 通过，并注明识别来源 |
| `rtx6000d` | 8 张 RTX PRO 6000D 服务器版（取自真实机器，驱动名「RTX 6000D」）：`--expect pro6000d` / `6000d` → 通过；`--expect pro6000` → 不通过（特供版冒充 RTX PRO 6000）；默认 → 不通过（非 H100） |
| `unknownid` | 设备 ID 型号库和 pci.ids 都不认识：默认 → 驱动名称是别的型号，不通过；`--expect 6000d` → 名称相符，待确认 |
| `cmp170hx` | 8 张 CMP 170HX 矿卡（GA100 核心）：`--expect a100` → 不通过，注明是挖矿专用卡；`--expect any` → 通过，识别为 CMP 170HX（70 个 SM） |

此外还有 5 项测试：`tests/test_keys.py` 检查型号名称规则，驱动的官方名称、`--expect` 的各种写法都要识别成正确的型号，型号库每条记录都要自洽；另外 4 项覆盖 `run.sh`：模拟 `curl … | bash` 正常执行；改名前的环境变量名 `H100_CHECK_URL` 仍然有效；下载不完整时拒绝执行；误把 `gpu_check.py` 直接管道给 bash 时给出正确用法提示。
