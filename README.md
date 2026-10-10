# comfyui-stability-pack

ComfyUI 稳定性优化与体检工具包 —— 针对 Windows 下跑 ComfyUI 满载时出现
`CLOCK_WATCHDOG_TIMEOUT (0x101)` / `HYPERVISOR_ERROR (0x20001)` 蓝屏死机的
排查成果沉淀。零积分、纯标准库、离线可用。

## 背景

本机环境：联想拯救者 Y9000P IAX10（83F4），RTX 5060 Laptop 8GB，Core Ultra 9 275HX。

**排查结论**：ComfyUI 推理（全核满载 + GPU 满载 + 功耗尖峰）是最狠的稳定性测试，
但它只是"扳机"。真正的病根是 **VBS/HVCI 虚拟化安全层**（HYPERVISOR_ERROR 蓝屏的
直接来源）。关闭内核隔离 + 更新驱动后缓解。本工具包把排查成果沉淀为
"每次出图前的自动体检 + 运行时守护"，防止风险因素悄悄回来。

## 文件清单

| 文件 | 作用 |
|------|------|
| `stability.py` | 稳定性体检库（VBS/HVCI 检查、GPU 显存/温度、蓝屏记录、残留进程清理、稳定环境变量、进程提优先级） |
| `comfyui_gen.py` | 一键出图 CLI（已并入出图前自动体检） |
| `comfyui_core.py` | 共享库：服务生命周期管理、HTTP、工作流构建、稳健写盘 |
| `cutout_composite.py` | rembg 抠图 + 多层 alpha 合成（可选） |
| `ascii_view.py` | 无视觉通道时用 ASCII 亮度图看图（可选） |

## 快速开始

### 方式一：出图即体检（推荐）

`comfyui_gen.py` 已内置出图前自动体检，用法不变：

```bash
python comfyui_gen.py --mode text2img --prompt "..." --seeds 1 2 3 4
```

出图前自动执行：
1. VBS/HVCI 状态检查（防误开后死机风险回升）
2. GPU 显存/温度检查（≥7600MB 或 ≥87°C 告警）
3. 残留进程清理（服务在线时自动跳过，不打断出图）
4. 注入稳定环境变量 + 提进程优先级

跳过体检：加 `--no-check`。

### 方式二：独立体检/守护

```bash
python stability.py                # 见下方"独立运行"
```

`stability.py` 主要 API：

```python
import stability

stability.preflight(fast=True)      # 出图前一键体检（VBS+GPU+残留清理）
stability.check_vbs_hvci()          # 确认 HYPERVISOR_ERROR 缓解仍生效
stability.gpu_status()              # (used_mb, total_mb, temp_c) 或 None
stability.clean_stale(force=False)  # 清残留进程；force=True 强制停在线服务
stability.raise_priority(pid)       # ComfyUI 进程提 High 优先级
stability.stable_env()              # 稳定环境变量 dict
```

## 环境变量与阈值

| 变量/常量 | 默认值 | 说明 |
|-----------|--------|------|
| `VRAM_BUDGET_MB` | 7600 | 8GB 卡留 500MB 余量，超过告警 |
| `TEMP_WARN_C` | 87 | GPU 温度告警线 |
| `COMFYUI_ROOT` | `C:\Users\34500\ComfyUI` | ComfyUI 安装目录 |
| `COMFYUI_PORT` | 8188 | 服务端口 |
| `PYTORCH_CUDA_ALLOC_CONF` | `max_split_size_mb:256,expandable_segments:True` | 减少显存碎片 OOM |
| `NO_PROXY` | `127.0.0.1,localhost` | 本机调用绕代理（防健康检查假活） |

## 一次性系统修复（已在本机完成，供参考）

蓝屏排查时的系统级修复，**需要管理员权限**：

1. **关闭内核隔离（核心修复）**：设置 → 隐私和安全性 → Windows 安全中心 →
   设备安全性 → 内核隔离 → 关闭"内存完整性"，重启。
   或管理员 PowerShell：
   ```powershell
   Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity" -Name "Enabled" -Value 0
   Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard" -Name "EnableVirtualizationBasedSecurity" -Value 0
   ```
2. **关闭快速启动**（防内核状态休眠保存导致设置未冷启动生效）：
   ```powershell
   Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name "HiberbootEnabled" -Value 0
   ```
3. **更新驱动**：NVIDIA（592.01 → 617.42）、Intel 芯片组 INF（10.1.20398.8776）、
   BIOS 保持最新（本机 Q7CN78WW 已最新）。

> 权衡：关闭内核隔离会略降系统对内核级攻击的防护。日常使用影响很小；
> 想恢复时反向操作即可（把上面注册表值改回 1）。

## 安全说明

- 纯标准库，无第三方依赖
- 注册表操作**只读**（写操作需用户手动完成，本工具不代写 HKLM）
- 只清理 ComfyUI 自己的残留进程，不碰其他进程
- 本机 HTTP 调用仅指向 `127.0.0.1:8188`，无外发流量

## 排查记录速查

| 症状 | 含义 | 处置 |
|------|------|------|
| `HYPERVISOR_ERROR (0x20001)` | VBS/HVCI 虚拟化层崩溃 | 关内核隔离（见上） |
| `CLOCK_WATCHDOG_TIMEOUT (0x101)` | CPU 核心无响应 | 升 BIOS/驱动、查降压、查温度 |
| 死机无 dump（Kernel-Power 41） | 硬挂死 | 看同日是否有 0x20001/0x101 前兆 |
| 出图显存爆 | 8GB 卡常态 | `--lowvram`、降分辨率、减少并发 |
| minidump 位置 | `C:\Windows\Minidump\` | WinDbg `!analyze -v` 分析 |
