# -*- coding: utf-8 -*-
r"""
stability.py — ComfyUI 稳定性体检与守护（从 _optimize_comfyui.py 并入技能）
===========================================================================
背景：本机（联想 Y9000P IAX10，RTX 5060 8GB）跑 ComfyUI 满载时曾出现
HYPERVISOR_ERROR (0x20001) / CLOCK_WATCHDOG_TIMEOUT (0x101) 死机，
根因是 VBS/HVCI 虚拟化安全层，已关闭缓解。本模块把"每次出图前的
系统体检 + 运行时保护"沉淀成可复用库，供 comfyui_gen.py 出图前自动调用：

  - check_vbs_hvci()   确认 VBS/HVCI 仍关闭（缓解生效），异常时提示
  - check_gpu()        GPU 显存/温度检查，超阈值告警
  - check_bsod_since() 最近蓝屏记录（PowerShell 查 Event 1001）
  - clean_stale()      清理残留 ComfyUI 进程（服务在线时默认跳过）
  - stable_env()       推荐的稳定环境变量（PYTORCH_CUDA_ALLOC_CONF / NO_PROXY）
  - raise_priority()   把 ComfyUI 进程提到 High 优先级
  - preflight()        出图前一键体检（fast 模式只跑轻量项）

纯标准库 + 只读注册表；不杀非 ComfyUI 进程、不做破坏性操作。
"""
import ctypes
import os
import subprocess
import time

import comfyui_core as C

VRAM_BUDGET_MB = 7600   # 8GB 卡留 500MB 余量
TEMP_WARN_C = 87        # GPU 温度告警线


# ---------------------------------------------------------------------------
# 注册表只读
# ---------------------------------------------------------------------------
def _reg_get(path, name):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, name)
        winreg.CloseKey(key)
        return val
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 体检项
# ---------------------------------------------------------------------------
def check_vbs_hvci():
    """确认 VBS/HVCI 仍处于关闭（死机缓解项）。开启时警告。"""
    hvci = _reg_get(r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
                    r"\HypervisorEnforcedCodeIntegrity", "Enabled")
    vbs = _reg_get(r"SYSTEM\CurrentControlSet\Control\DeviceGuard",
                   "EnableVirtualizationBasedSecurity")
    if hvci == 0 and vbs == 0:
        print("[体检 OK] VBS/HVCI 已关闭（HYPERVISOR_ERROR 缓解生效）")
        return True
    print("[体检 警告] VBS/HVCI 状态异常: HVCI=%s VBS=%s" % (hvci, vbs))
    print("           满载死机风险回升。若非有意开启，请到 Windows 安全中心关闭内核隔离。")
    return False


def gpu_status():
    """读 GPU 显存占用/温度。返回 (used_mb, total_mb, temp_c) 或 None。"""
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=15)
        txt = raw.decode("gbk", errors="ignore").strip()
        used, total, temp = [int(x.strip()) for x in txt.split(",")]
        return used, total, temp
    except Exception:
        return None


def check_gpu():
    """GPU 体检：显存接近预算或温度过高时告警。"""
    st = gpu_status()
    if not st:
        print("[体检 跳过] nvidia-smi 不可用")
        return True
    used, total, temp = st
    print("[体检 GPU] 显存 %d/%d MB，温度 %d°C" % (used, total, temp))
    warn = False
    if used >= VRAM_BUDGET_MB:
        print("    [警告] 显存已接近上限（预算 %d MB），建议 --lowvram 或减少并发" % VRAM_BUDGET_MB)
        warn = True
    if temp >= TEMP_WARN_C:
        print("    [警告] GPU 温度偏高（>=%d°C），注意散热" % TEMP_WARN_C)
        warn = True
    return not warn


def check_bsod_since(days=7):
    """列出最近 N 天的 BugCheck 记录（Event 1001），无则报 OK。"""
    try:
        cmd = ("Get-WinEvent -FilterHashtable @{LogName='System'; Id=1001} -MaxEvents 5 "
               "-ErrorAction SilentlyContinue | "
               "ForEach-Object { \"$($_.TimeCreated) | $($_.Message.Substring(0,[Math]::Min(90,$_.Message.Length)))\" }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=30)
        out = r.stdout.decode("gbk", errors="ignore").strip()
        if out:
            print("[体检 注意] 最近有蓝屏记录：")
            for line in out.splitlines():
                print("    " + line)
            return False
        print("[体检 OK] 最近无蓝屏记录")
        return True
    except Exception:
        print("[体检 跳过] 无法查询蓝屏日志")
        return True


# ---------------------------------------------------------------------------
# 进程优化
# ---------------------------------------------------------------------------
def clean_stale(force=False):
    """清理残留 ComfyUI 进程。服务在线且未 force 时跳过（可能正在出图）。"""
    up = C.service_up(timeout=4)
    if up and not force:
        print("[清理 跳过] ComfyUI 在线（可能正在出图），不清理")
        return True
    pids = C._pids_on_port()
    if not pids and not up:
        print("[清理 OK] 无残留 ComfyUI 进程")
        return True
    print("[清理] 清扫端口 %d 上的进程: %s" % (C.PORT, ", ".join(sorted(pids))))
    return C.shutdown()


def stable_env():
    """返回对出图稳定性有益的环境变量 dict（供拉起服务时注入）。"""
    env = dict(os.environ)
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:256,expandable_segments:True")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def raise_priority(pid):
    """把 ComfyUI 进程提到 High 优先级（采样时系统不易卡顿）。失败静默。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0200, False, int(pid))  # PROCESS_SET_INFORMATION
        if not h:
            return False
        k32.SetPriorityClass(h, 0x80)  # HIGH_PRIORITY_CLASS
        k32.CloseHandle(h)
        print("[优化] ComfyUI (PID=%s) 优先级已提到 High" % pid)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 一键体检
# ---------------------------------------------------------------------------
def preflight(fast=False, clean=True):
    """出图前体检。fast=True 只跑轻量项（VBS + GPU，跳过蓝屏日志查询）。
    返回 True 表示可以放心出图；False 表示有告警（仍可出图，仅提示）。"""
    ok = True
    ok = check_vbs_hvci() and ok
    ok = check_gpu() and ok
    if not fast:
        ok = check_bsod_since() and ok
    if clean:
        clean_stale()
    return ok
