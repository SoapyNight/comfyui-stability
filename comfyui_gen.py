# -*- coding: utf-8 -*-
r"""
comfyui_gen.py — 本地 ComfyUI 一键出图 CLI（零积分，纯标准库）
============================================================
用法（用任意 Python3，推荐托管 py3.13；脚本无重依赖）：

  # text2img 出单张（出完默认自动关服务释放显存）
  python comfyui_gen.py --mode text2img --prompt "..." --negative "..." \
      --width 1024 --height 1024 --ckpt animagine-xl-3.1.safetensors --seed 7

  # text2img 多种子批量（同 prompt 出 4 张挑一张）
  python comfyui_gen.py --mode text2img --prompt "..." --seeds 121 122 123 124 \
      --prefix float_char --out "C:\...\out"

  # img2img（ref 给完整路径会自动拷入 ComfyUI\input\；denoise 越低越贴近参考图）
  python comfyui_gen.py --mode img2img --ref "C:\...\base.png" --prompt "..." \
      --negative "..." --denoise 0.7 --seed 7

参数说明：
  --mode        text2img | img2img
  --prompt/-p   正向提示词
  --negative    负向提示词（默认空）
  --ckpt        checkpoint 文件名（默认 animagine-xl-3.1.safetensors）
  --vae         独立 VAE 文件名（留空用 ckpt 内置 VAE）
  --width/-W    text2img 宽（默认 1024）
  --height/-H   text2img 高（默认 1024）
  --ref         img2img 参考图（文件名或完整路径）
  --denoise     img2img 重绘强度 0~1（默认 0.7）
  --steps       采样步数（默认 28）
  --cfg         引导强度（默认 7.0）
  --sampler     采样器（默认 dpmpp_2m）
  --scheduler   调度器（默认 karras）
  --seed        单种子
  --seeds       多种子（空格分隔，批量出图）
  --prefix      输出文件名前缀（默认 comfy）
  --out         输出目录（默认当前目录）
  --keep-alive  出完不关服务（连续多张时省重启）
  --no-check    跳过出图前的稳定性体检（默认开启体检）
"""
import argparse, os, shutil, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfyui_core as C
import stability  # 稳定性体检（VBS/HVCI/GPU/残留进程），见 stability.py

DEFAULT_CKPT = os.environ.get("COMFYUI_CKPT", "animagine-xl-3.1.safetensors")


def _prepare_ref(ref):
    r"""img2img 的 ref：若给的是完整路径，拷入 COMFYUI\input\ 并返回文件名；否则原样返回文件名。"""
    if os.path.isabs(ref) or (os.sep in ref) or (os.altsep and os.altsep in ref):
        name = os.path.basename(ref)
        if os.path.exists(ref):
            dst = os.path.join(C.COMFYUI_ROOT, "input", name)
            try:
                shutil.copyfile(ref, dst)
                print("[..] 已把参考图拷入 input/: %s" % name)
            except Exception as e:
                print("[warn] 拷贝参考图失败（%r），假定已在 input/ 下" % (e,))
            return name
        print("[warn] 参考图不存在: %s" % ref)
    return ref


def main():
    ap = argparse.ArgumentParser(description="本地 ComfyUI 一键出图（零积分）")
    ap.add_argument("--mode", choices=["text2img", "img2img"], default="text2img")
    ap.add_argument("--prompt", "-p", default="")
    ap.add_argument("--negative", default="")
    ap.add_argument("--ckpt", default=DEFAULT_CKPT)
    ap.add_argument("--vae", default="")
    ap.add_argument("--width", "-W", type=int, default=1024)
    ap.add_argument("--height", "-H", type=int, default=1024)
    ap.add_argument("--ref", default="")
    ap.add_argument("--denoise", type=float, default=0.7)
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--cfg", type=float, default=7.0)
    ap.add_argument("--sampler", default="dpmpp_2m")
    ap.add_argument("--scheduler", default="karras")
    ap.add_argument("--seed", type=int, default=int(time.time()) % (2**32))
    ap.add_argument("--seeds", type=int, nargs="*", default=None)
    ap.add_argument("--prefix", default="comfy")
    ap.add_argument("--out", default=os.getcwd())
    ap.add_argument("--keep-alive", action="store_true")
    ap.add_argument("--no-check", action="store_true",
                    help="跳过出图前的稳定性体检（VBS/HVCI/GPU/残留进程）")
    a = ap.parse_args()

    if not a.prompt:
        print("[错误] 需要 --prompt"); sys.exit(1)
    if a.mode == "img2img" and not a.ref:
        print("[错误] img2img 需要 --ref"); sys.exit(1)

    # img2img：完整路径→拷入 COMFYUI\input 并转文件名
    if a.mode == "img2img":
        a.ref = _prepare_ref(a.ref)

    seeds = a.seeds if a.seeds else [a.seed]
    os.makedirs(a.out, exist_ok=True)

    # 出图前稳定性体检（VBS/HVCI/GPU 显存温度/残留进程清理；--no-check 跳过）
    if not a.no_check:
        stability.preflight(fast=True)

    # 注入稳定环境变量（本进程及后续 try_autostart 拉起的服务都会继承）
    os.environ.update(stability.stable_env())

    server_pid = C.ensure_service()
    if server_pid is None and not C.service_up():
        sys.exit(1)
    print("[OK] ComfyUI 在线:", C.BASE)
    # 提高进程优先级，采样时系统不易卡顿
    if server_pid:
        stability.raise_priority(server_pid)

    results = []
    for i, s in enumerate(seeds):
        suffix = "" if len(seeds) == 1 else "_%d" % s
        out = os.path.join(a.out, "%s%s.png" % (a.prefix, suffix))
        if a.mode == "text2img":
            wf = C.build_text2img(a.ckpt, a.prompt, a.negative, a.width, a.height,
                                  steps=a.steps, cfg=a.cfg, sampler=a.sampler,
                                  scheduler=a.scheduler, seed=s, prefix=a.prefix + suffix,
                                  vae=a.vae)
        else:
            wf = C.build_img2img(a.ckpt, a.ref, a.prompt, a.negative,
                                 steps=a.steps, cfg=a.cfg, sampler=a.sampler,
                                 scheduler=a.scheduler, seed=s, prefix=a.prefix + suffix,
                                 denoise=a.denoise)
        try:
            ok = C.gen_one(wf, out, "%s seed=%d" % (a.mode, s))
        except Exception as e:
            print("[异常] seed=%d: %r" % (s, e)); ok = False
        results.append((s, out, ok))

    if a.keep_alive:
        print("[保留] 按 --keep-alive 保留 ComfyUI 服务")
    else:
        C.shutdown(known_pid=server_pid)

    print("==== 结果 ====")
    for s, out, ok in results:
        print("%s  seed=%d  %s" % ("OK " if ok else "FAIL", s, out))
    sys.exit(0 if all(r[2] for r in results) else 2)


if __name__ == "__main__":
    main()