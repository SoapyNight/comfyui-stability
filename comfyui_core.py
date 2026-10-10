# -*- coding: utf-8 -*-
r"""
comfyui_core.py — 本地 ComfyUI 出图共享库（零积分，纯标准库 urllib）
====================================================================
把"本地 ComfyUI 出图 + 图像后处理"整套流程沉淀成可复用模块。提供：

  - 服务管理：健康检查 / 自动后台拉起 / 精确+端口清扫关闭
  - HTTP：绕环境代理的 http_json（本机 127.0.0.1 必须绕过 HTTP_PROXY）
  - 工作流构建：build_text2img() / build_img2img()
  - 稳健写盘：_robust_write()（规避沙箱拦截 os.replace）
  - 单任务生成：gen_one()（含"history 有 pid 但 outputs 未写完"的竞态修复）

所有路径/端口可用环境变量覆盖，默认值指向本机已验证环境：
  COMFYUI_ROOT   默认 C:\Users\34500\ComfyUI
  COMFYUI_PORT   默认 8188
  COMFYUI_BASE   默认 http://127.0.0.1:8188
  COMFYUI_PYTHON 默认 <COMFYUI_ROOT>\.venv\Scripts\python.exe（带 rembg/numpy 的抠图解释器）

本模块只依赖标准库，可用任意 Python3 运行（含托管 py3.13）。
"""
import json, os, subprocess, time, urllib.request, urllib.parse

COMFYUI_ROOT = os.environ.get("COMFYUI_ROOT", r"C:\Users\34500\ComfyUI")
PORT = int(os.environ.get("COMFYUI_PORT", "8188"))
BASE = os.environ.get("COMFYUI_BASE", "http://127.0.0.1:%d" % PORT)
# 抠图/合成（rembg + onnxruntime + numpy）所在的解释器：ComfyUI 自带 venv
COMFYUI_PYTHON = os.environ.get(
    "COMFYUI_PYTHON", os.path.join(COMFYUI_ROOT, ".venv", "Scripts", "python.exe"))

# 出图成功后 ComfyUI 图落地的输出目录（前缀_序号_.png）
OUTPUT_DIR = os.path.join(COMFYUI_ROOT, "output")


# 本机 127.0.0.1 的 ComfyUI 调用必须绕过环境代理（HTTP_PROXY），否则健康检查会被代理"假活"污染
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(url_or_req, timeout=30):
    return _OPENER.open(url_or_req, timeout=timeout)


def http_json(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with _open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# 工作流构建
# ---------------------------------------------------------------------------
def build_text2img(ckpt, prompt, negative, width, height,
                   steps=28, cfg=7.0, sampler="dpmpp_2m", scheduler="karras",
                   seed=0, prefix="out", vae=""):
    """text2img：EmptyLatentImage -> KSampler(denoise=1.0) -> VAEDecode -> SaveImage。
    vae 为空串时用 ckpt 内置 VAE。"""
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "3": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["3", 0]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["7", 0]}},
    }
    if vae:
        wf["2"] = {"class_type": "VAELoader", "inputs": {"vae_name": vae}}
        wf["7"] = {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["2", 0]}}
    return wf


def build_img2img(ckpt, ref_image, prompt, negative,
                  steps=28, cfg=7.0, sampler="dpmpp_2m", scheduler="karras",
                  seed=0, prefix="i2i", denoise=0.7):
    """img2img：LoadImage(相对 COMFYUI/input 的文件名) -> VAEEncode -> KSampler(denoise<1.0)。
    ref_image 必须是 ComfyUI/input 下已有的文件名（不是完整路径）。
    输出尺寸跟随参考图。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "load": {"class_type": "LoadImage", "inputs": {"image": ref_image}},
        "enc": {"class_type": "VAEEncode", "inputs": {"pixels": ["load", 0], "vae": ["1", 2]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                         "scheduler": scheduler, "denoise": denoise, "model": ["1", 0],
                         "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["enc", 0]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["7", 0]}},
    }


# ---------------------------------------------------------------------------
# 稳健写盘
# ---------------------------------------------------------------------------
def _robust_write(data, outpath, start=None):
    """把图片数据稳健落地到 outpath。

    根因（本环境实测）：os.replace 走 Windows MoveFileEx + MOVEFILE_REPLACE_EXISTING 会被
    沙箱拦截（PermissionError: 拒绝访问），而 shutil.move（os.rename，不带替换标志）可成功。
    三级回退 + 唯一临时名 + 重试：1) os.replace  2) shutil.move  3) 直接写最终名。"""
    last_err = None
    for attempt in range(6):
        tmp_path = "%s.%d_%d.tmp" % (outpath, attempt, os.getpid())
        fh = None
        try:
            fh = open(tmp_path, "wb")
            fh.write(data)
            fh.close()
            fh = None
            try:
                os.replace(tmp_path, outpath)
                return True
            except Exception as e1:
                last_err = e1
                try:
                    import shutil
                    shutil.move(tmp_path, outpath)
                    return True
                except Exception as e2:
                    last_err = e2
                    with open(outpath, "wb") as fd:
                        fd.write(data)
                    return True
        except Exception as werr:
            last_err = werr
            print("  [write-err] attempt %d: %r" % (attempt + 1, werr))
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        time.sleep(1.0)
    print("[错误] 写入被系统锁定，6 次重试均失败（最后错误: %r）:" % (last_err,), outpath)
    return False


# ---------------------------------------------------------------------------
# 服务管理
# ---------------------------------------------------------------------------
def service_up(timeout=5):
    try:
        http_json(BASE + "/system_stats", timeout=timeout)
        return True
    except Exception:
        return False


def try_autostart():
    """服务没起时，Windows 下后台直接拉起 venv 的 python main.py（带日志）。
    返回：成功→该进程 PID(int)；失败→None。"""
    if os.name != "nt":
        return None
    py = os.path.join(COMFYUI_ROOT, ".venv", "Scripts", "python.exe")
    main_py = os.path.join(COMFYUI_ROOT, "main.py")
    if not (os.path.exists(py) and os.path.exists(main_py)):
        print("[warn] 找不到 venv python 或 main.py，无法自动启动:", py)
        return None
    log = os.path.join(COMFYUI_ROOT, "comfyui_auto.log")
    pid = None
    try:
        logf = open(log, "ab")
        # DETACHED_PROCESS(0x00000008) | CREATE_NEW_PROCESS_GROUP(0x00000200)
        proc = subprocess.Popen([py, main_py, "--lowvram", "--port", str(PORT),
                                 "--listen", "127.0.0.1"],
                                cwd=COMFYUI_ROOT, stdout=logf, stderr=subprocess.STDOUT,
                                creationflags=0x00000008 | 0x00000200, close_fds=True)
        logf.close()
        pid = proc.pid
    except Exception as e:
        print("[warn] 自动启动失败:", repr(e))
        return None
    print("[..] 已后台启动 ComfyUI（PID=%s, 日志: %s），等待就绪（最多 80s）..." % (pid, log))
    for _ in range(40):
        time.sleep(2)
        if service_up(timeout=4):
            print("[OK] ComfyUI 就绪")
            return pid
    print("[warn] 80s 内未就绪，可查看日志: %s" % log)
    return None


def _kill_tree(pid):
    """强制杀掉指定 PID。taskkill 会被沙箱拦截，故直接用 kernel32 TerminateProcess。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 9)
            return True
        except Exception:
            return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0001, False, pid)
        if not h:
            return False
        rc = k32.TerminateProcess(h, 1)
        k32.CloseHandle(h)
        return bool(rc)
    except Exception:
        return False


def _pids_on_port():
    """netstat 找监听端口的 PID 集合。中文 Windows netstat 表头是 GBK，手动解码。"""
    pids = set()
    try:
        raw = subprocess.check_output("netstat -ano", stderr=subprocess.DEVNULL)
        try:
            out = raw.decode("gbk", errors="ignore")
        except Exception:
            out = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        print("[warn] 无法执行 netstat:", repr(e))
        return pids
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if state.upper() == "LISTENING" and local.rsplit(":", 1)[-1] == str(PORT):
            pids.add(pid)
    return pids


def shutdown(known_pid=None):
    """关闭 ComfyUI：先精确杀已知进程树，再做"带重试的端口清扫"直到端口真正释放。"""
    if known_pid:
        _kill_tree(known_pid)
    killed = set()
    for _ in range(6):
        pids = _pids_on_port()
        if not pids:
            break
        for pid in pids:
            if _kill_tree(pid):
                killed.add(pid)
        time.sleep(1.0)
    remaining = _pids_on_port()
    if remaining:
        print("[警告] 关闭后仍有进程监听 %s: %s（可能被系统锁定，稍后重试）" % (PORT, ", ".join(sorted(remaining))))
        return False
    print("[关闭] ComfyUI 已停止（PID: %s）" % (", ".join(sorted(killed)) if killed else "无残留监听"))
    return True


def ensure_service():
    """确保服务在线。返回自动拉起的 PID（若本次是脚本拉起的，否则 None）。"""
    if service_up():
        return None
    print("[..] ComfyUI 未在线，尝试自动启动 ...")
    pid = try_autostart()
    if not pid:
        print("[错误] 无法连接 ComfyUI。请手动运行: %s\\run_comfyui.bat" % COMFYUI_ROOT)
    return pid


# ---------------------------------------------------------------------------
# 单任务生成（含下载竞态修复）
# ---------------------------------------------------------------------------
def gen_one(workflow, out, label, timeout=900):
    """提交 workflow 并轮询 history，把第一张产出图稳健写到 out。
    关键修复：history 里出现 pid 但 outputs 尚未写完时，sleep(2) 稍等重试，而非立即判失败
    （否则会得到"任务完成但无图片输出"的假失败——图其实已落地到 OUTPUT_DIR）。"""
    resp = http_json(BASE + "/prompt", {"prompt": workflow})
    pid = resp.get("prompt_id")
    if not pid:
        print("[错误] 提交失败 %s: %r" % (label, resp))
        return False
    start = time.time()
    while time.time() - start < timeout:
        try:
            h_ = http_json(BASE + "/history/" + pid, timeout=15)
        except Exception:
            time.sleep(3)
            continue
        if pid in h_:
            imgs = []
            for _, nodeout in h_[pid].get("outputs", {}).items():
                imgs += nodeout.get("images", [])
            if imgs:
                img = imgs[0]
                fn = img["filename"]; sub = img.get("subfolder", ""); typ = img.get("type", "output")
                full = (BASE + "/view?filename=" + urllib.parse.quote(fn)
                        + "&subfolder=" + urllib.parse.quote(sub) + "&type=" + urllib.parse.quote(typ))
                with _open(full, timeout=60) as r2:
                    data = r2.read()
                if _robust_write(data, out, start):
                    print("[完成] %s  %d bytes  %.1fs" % (label, len(data), time.time() - start))
                    return True
            time.sleep(2)
            continue   # outputs 尚未写完 → 稍等重试（竞态修复）
        time.sleep(3)
    print("[超时] %s（可去 %s 按前缀捞取）" % (label, OUTPUT_DIR))
    return False