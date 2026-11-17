# h100_check — H100 GPU 服务器只读检测

单文件脚本 `h100_check.py`，只用 Python 3.6+ 标准库，不需要 pip 安装任何东西。它在 GPU 服务器上收集硬件配置，在终端输出表格报告，重点核验 GPU 是否为真 H100（能识别 H20 / H800 冒充）。

**检测全程只读，不影响线上业务**：不创建 CUDA 上下文、不占显存、不跑任何负载或带宽测试、不重置计数器、不改任何配置。详见下面的「对线上业务的影响」一节。

## 一键运行

在 GPU 服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/h100-check/main/run.sh | sudo bash
```

带参数时接在 `bash -s --` 后面。比如要求必须是 SXM5 版，并把报告另存一份：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/h100-check/main/run.sh | sudo bash -s -- --expect h100-sxm -o /tmp/h100_report.txt
```

`run.sh` 只做三件事：找到 Python 3.6+，把 `h100_check.py` 下载到内存并检查是否完整，再交给 Python 执行。除了 `-o` 指定的报告文件，不在磁盘上留任何东西。不加 `sudo` 也能跑，但会缺少内存条型号、网卡序列号和料号，以及 dmesg 里的 Xid 记录。

要固定版本、不受仓库后续更新影响，就直接运行打了标签的版本：

```bash
curl -fsSL https://raw.githubusercontent.com/rivia7/h100-check/v1.0.0/h100_check.py | sudo python3 -
```

### 服务器访问不了 GitHub 时

国内机房经常连不上 `raw.githubusercontent.com`。可以在能上网的机器上下载一次，再通过 ssh 管道执行。目标机不需要外网，也不会留下文件：

```bash
curl -fsSLO https://raw.githubusercontent.com/rivia7/h100-check/main/h100_check.py
ssh gpu-node 'sudo python3 - --no-color' < h100_check.py > gpu-node.txt

# 批量检查，按退出码汇总
for h in node01 node02 node03; do
  ssh "$h" 'sudo python3 - --no-color' < h100_check.py > "report-$h.txt"; echo "$h rc=$?"
done
```

也可以把 `run.sh` 和 `h100_check.py` 放到内网 HTTP 服务器上，用 `H100_CHECK_URL` 指定脚本地址：

```bash
curl -fsSL http://<内网地址>/run.sh | sudo env H100_CHECK_URL=http://<内网地址>/h100_check.py bash
```

## 参数

| 参数 | 说明 |
|---|---|
| `-o FILE` | 把报告另存一份到文件（不含颜色码） |
| `--expect h100` | 默认值。H100 的任何形态（SXM5 / PCIe / NVL）都算通过 |
| `--expect h100-sxm` | 必须是 SXM5 版，PCIe/NVL 版判为「形态不符」。也可用 `h100-pcie`、`h100-nvl` |
| `--expect any` | 不限型号，只检查各数据源是否一致、规格是否与识别出的型号吻合（比如用来检查 H20 机器本身是否正常） |
| `--expect-gpus N` | 期望的 GPU 张数。HGX 8 卡机型（4 颗 NVSwitch）会自动按 8 张检查 |
| `--cuda` | 额外通过 CUDA 驱动 API 读取 SM 数、显存位宽等属性，多一路交叉验证。只调用 `cuInit` 和属性查询，不创建上下文、不占显存。默认关闭 |
| `--no-color` | 关闭颜色（输出被重定向时会自动关闭） |

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 通过：GPU 确认为真 H100，没有健康或配置问题 |
| 1 | 有告警：GPU 是真的，但有健康/配置问题（NVLink 掉链、IB 端口 down、Xid、ECC 等）；或真伪暂时无法确认（如驱动未加载） |
| 2 | 不通过：GPU 不是 H100（H20/H800 等），或发现数据被篡改的证据 |
| 3 | 运行错误（非 Linux、找不到 Python 等） |

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
4. **H100 真伪核验**：每张卡逐项比对硬件 ID 和规格指纹，另有工具链完整性检查（见下节）
5. **NVLink 拓扑**：`nvidia-smi topo -m` 互联矩阵（含 CPU 亲和）、每卡链路激活数/速率/错误计数/未激活链路、通过 NVML 读到的各卡到每颗 NVSwitch 的链路分布、NVSwitch 数量、Fabric Manager 状态
6. **IB / RDMA 网卡**：型号、料号(PN)、序列号、固件、PSID、PCIe 当前/能力速率、端口状态/速率/LID、链路错误计数，以及每张 GPU 最近的 RDMA 网卡（GPUDirect RDMA 亲和）
7. **其它网卡**：管理网卡等
8. **GPU 运行状态快照**：温度、功耗、利用率、显存占用、进程数、ECC、行重映射、降频原因、Xid
9. **检测结论**：按类别汇总，列出「需关注」项，给出总体结论

## 真伪判定原理

结论来自几层互相独立的数据源，彼此必须一致：

1. **硬件层**：PCI 设备 ID 由 GPU 芯片熔丝决定，改名字、贴标签、改 nvidia-smi 都改不了。脚本既读内核缓存的 ID，也直接读 PCI 配置空间，两者对比。
2. **驱动层**：`/proc/driver/nvidia` 里的型号、NVML 直连读到的型号和 PCI ID（绕过 nvidia-smi 程序，直接调 `libnvidia-ml.so`）、nvidia-smi 的输出，三者都必须和硬件 ID 对得上；nvidia-smi 和 NVML 报的显存、功耗墙、频率、序列号也要一致。
3. **规格指纹**：SM 数、显存位宽、显存容量、最大功耗墙、最大频率、NVLink 链路数、计算能力（9.0）、GPU 部件号前缀，与该型号的标准值比对。
4. **工具链**：nvidia-smi 是否为原厂 ELF、包管理器校验和是否一致、驱动各组件版本是否一致、`/etc/ld.so.preload` 和 `LD_PRELOAD` 劫持。

关键项（硬件 ID、型号名、SM 数、显存位宽、显存容量、架构、nvidia-smi 与 NVML 一致性）只要有一项不符，就判「不通过」。功耗墙、频率、部件号这类非关键项有偏差时只告警，不影响真伪结论。

常见设备 ID（来源：pci.ids 与 NVIDIA 驱动 README 的支持列表）：

| 设备 ID | 型号 | 说明 |
|---|---|---|
| `0x2330` | H100 SXM5 80GB | HGX H100 8 卡机最常见 |
| `0x2331` | H100 PCIe 80GB | |
| `0x2321` | H100 NVL 94GB | |
| `0x2337` / `0x2338` / `0x2339` | H100 SXM5 64GB / 96GB / 94GB | |
| `0x2329` | **H20 96GB** | 中国特供版，算力大幅阉割 |
| `0x2328` / `0x232c` | **H20B / H20-3e** | 同上 |
| `0x2324` / `0x2322` / `0x233a` | **H800** SXM5 / PCIe / NVL | 特供版，NVLink 只有 8 条 |
| `0x2335` / `0x233b` | H200 SXM / NVL | 比 H100 更高规格，判为「非 H100」但不算降级 |

## 局限

- **内核级伪造防不住**：如果有人改了内核（例如修改开源内核模块，或在 PCI 层伪造设备 ID），所有软件读到的数据都可能是假的。脚本会显示内核模块的 srcversion 和 taint 标志，可与同版本的正常机器比对。
- **不做算力实测**：要确认实际算力，需要在维护窗口对空闲 GPU 单独压测（比如跑 BF16 GEMM：H100 SXM5 稠密 BF16 理论约 989 TFLOPS，H20 约 148 TFLOPS）。这会占用 GPU，所以脚本有意不做。
- 规格表里 H20、H800 的部分数值（SM 数、功耗等）以硬件 ID 判定为主，规格只作辅助参考。
- 脚本目前只在模拟环境里测试过（见下节），还没在真实 H100 机器上跑过。首次使用建议先在一台确定是正品的机器上跑一遍，作为基线对照。

## 离线测试

`tests/` 里是不需要 GPU 的回归测试：`make_fixture.py` 生成模拟的 `/sys`、`/proc` 和一组假命令，`mocknv.c` 编译成模拟的 NVML/CUDA 动态库，`run_tests.sh` 逐个场景运行并检查：

1. 退出码是否符合预期；
2. 报告里是否包含关键结论；
3. 是否只调用了白名单里的只读命令（每次调用都会记录下来比对）；
4. stderr 是否为空。

```bash
bash tests/run_tests.sh                      # 默认输出到 $TMPDIR/h100_check_tests
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
| `h100pcie` | 8 张 H100 PCIe → 通过；加 `--expect h100-sxm` → 形态不符 |
| `h20 --expect any` | 只检查一致性 → 通过 |

另有 3 项测试覆盖 `run.sh`：模拟 `curl … | bash` 正常执行；下载不完整时拒绝执行；误把 `h100_check.py` 直接管道给 bash 时给出正确用法提示。
