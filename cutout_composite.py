# -*- coding: utf-8 -*-
r"""
cutout_composite.py — rembg 抠图 + 多层 alpha 合成（两层/多层同框）
===================================================================
关键约定（踩坑沉淀）：
  * 用【正常 alpha 合成】（不透明前景盖在实体背景上），不要用 screen 叠加——screen 只保留
    亮部、丢细节、两层画风割裂，是"合成逻辑错误"的常见来源。
  * 背景层做「色调向最前层靠拢 + 压暗 + 轻高斯虚化」制造前后纵深，统一画风。
  * 依赖 rembg + onnxruntime + numpy：用 ComfyUI 自带 venv 解释器运行（COMFYUI_PYTHON）。
  * 抠图结果缓存到 _cut_<name>，重复运行不重算。

运行解释器（本机）：
  "C:/Users/34500/ComfyUI/.venv/Scripts/python.exe" cutout_composite.py <子命令> ...

子命令 1：抠单图
  cutout --in a.png [--model u2net|u2netp] [--cache-dir D]
    → 产出 D/_cut_a.png（透明背景 RGBA）

子命令 2：多层合成
  composite --base sky.png --out final.png \
      --layer "back.png=1.05=0.5=0.16=0.10=0.7" \   # 文件=宽比例=中心x=中心y=压暗=虚化
      --layer "front.png=1.0" \
      [--tone 0.35] [--model u2net]
    层序 = --layer 出现顺序（先=最远背景，后=最前）。
    layer 规格：file[=wfrac[=xfrac[=yfrac[=darken[=blur]]]]
      wfrac  目标宽/画布宽（默认 1.0）
      xfrac  水平中心位置（0.5=居中，默认 0.5）
      yfrac  垂直中心位置（0.5=居中，默认 0.5；可为 0~1 之外实现出界）
      darken 压暗强度 0~1（默认 0）
      blur   高斯虚化半径 px（默认 0）
    --tone S：把所有"非最前层"的色调向"最前层"靠拢（强度 S，默认 0 关闭）。
"""
import argparse, os, sys


def _get_session(model):
    from rembg import new_session
    return new_session(model)


def _cut(session, src, cache_dir, model):
    out = os.path.join(cache_dir, "_cut_" + os.path.basename(src))
    if os.path.exists(out):
        from PIL import Image
        print("  [缓存] %s" % os.path.basename(src))
        return Image.open(out).convert("RGBA")
    from PIL import Image
    from rembg import remove as _remove
    o = _remove(Image.open(src).convert("RGB"), session=session)
    o.save(out)
    print("  [抠出] %s  -> %s" % (os.path.basename(src), out))
    return o


def cmd_cutout(a):
    cache_dir = a.cache_dir or os.path.dirname(os.path.abspath(a.in_))
    os.makedirs(cache_dir, exist_ok=True)
    sess = _get_session(a.model)
    _cut(sess, a.in_, cache_dir, a.model)
    print("DONE  %s" % os.path.join(cache_dir, "_cut_" + os.path.basename(a.in_)))


def _tone_match(dragon_rgba, char_rgba, strength):
    import numpy as np
    ch = np.asarray(char_rgba)
    ca = ch[..., 3]
    if (ca > 40).sum() < 50:
        return dragon_rgba
    cmean = ch[ca > 40][..., :3].astype(np.float32).mean(axis=0)
    arr = np.asarray(dragon_rgba).copy()
    m = arr[..., 3] > 40
    if m.sum() == 0:
        return dragon_rgba
    dmean = arr[m][..., :3].astype(np.float32).mean(axis=0)
    arr[..., :3] = np.clip(arr[..., :3].astype(np.float32) + (cmean - dmean) * strength, 0, 255).astype(np.uint8)
    from PIL import Image
    return Image.fromarray(arr)


def _place(canvas, layer, W, H, wfrac, xfrac, yfrac, darken, blur):
    from PIL import Image, ImageFilter, ImageEnhance
    lw, lh = layer.size
    target_w = max(1, int(W * wfrac))
    target_h = max(1, int(lh * target_w / lw))
    layer = layer.resize((target_w, target_h), Image.LANCZOS)
    if darken > 0:
        layer = ImageEnhance.Brightness(layer).enhance(1.0 - darken)
    if blur > 0:
        r, g, b, a = layer.split()
        rgb = Image.merge("RGB", (r, g, b)).filter(ImageFilter.GaussianBlur(blur))
        layer = Image.merge("RGBA", tuple(rgb.split()) + (a,))
    cx = int(xfrac * W - target_w / 2)
    cy = int(yfrac * H - target_h / 2)
    canvas.alpha_composite(layer, (cx, cy))


def _parse_layer(spec):
    """file[=wfrac[=xfrac[=yfrac[=darken[=blur]]]]"""
    parts = spec.split("=")
    f = parts[0]
    def fnum(i, d):
        try:
            return float(parts[i]) if i < len(parts) and parts[i] != "" else d
        except Exception:
            return d
    return (f, fnum(1, 1.0), fnum(2, 0.5), fnum(3, 0.5), fnum(4, 0.0), fnum(5, 0.0))


def cmd_composite(a):
    from PIL import Image
    cache_dir = a.cache_dir or os.path.dirname(os.path.abspath(a.out))
    os.makedirs(cache_dir, exist_ok=True)
    sess = _get_session(a.model)

    base = Image.open(a.base).convert("RGBA")
    W, H = base.size
    print("[..] 画布 %dx%d  层数=%d  tone=%.2f" % (W, H, len(a.layer), a.tone))

    layers = []
    for spec in a.layer:
        f, wfrac, xfrac, yfrac, darken, blur = _parse_layer(spec)
        src = f if os.path.isabs(f) else os.path.join(cache_dir, f)
        layers.append((src, wfrac, xfrac, yfrac, darken, blur))

    cut = [_cut(sess, s[0], cache_dir, a.model) for s in layers]

    # 色调统一：所有非最前层向最前层靠拢
    front = cut[-1]
    for i in range(len(cut) - 1):
        if a.tone > 0:
            cut[i] = _tone_match(cut[i], front, a.tone)

    # 从后到前叠放
    for (src, wfrac, xfrac, yfrac, darken, blur), layer in zip(layers, cut):
        _place(base, layer, W, H, wfrac, xfrac, yfrac, darken, blur)

    base.convert("RGB").save(a.out)
    print("DONE  %s" % a.out)


def main():
    ap = argparse.ArgumentParser(description="rembg 抠图 + 多层 alpha 合成")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c1 = sub.add_parser("cutout", help="抠单图")
    c1.add_argument("--in", dest="in_", required=True)
    c1.add_argument("--model", default=os.environ.get("REMBG_MODEL", "u2net"))
    c1.add_argument("--cache-dir", default="")
    c1.set_defaults(func=cmd_cutout)

    c2 = sub.add_parser("composite", help="多层合成")
    c2.add_argument("--base", required=True, help="底图（天空/背景）")
    c2.add_argument("--out", required=True, help="输出 PNG")
    c2.add_argument("--layer", action="append", required=True,
                    help="file[=wfrac[=xfrac[=yfrac[=darken[=blur]]]]，可多次，先=最远层")
    c2.add_argument("--tone", type=float, default=0.0, help="非最前层向最前层靠拢强度(0关闭)")
    c2.add_argument("--model", default=os.environ.get("REMBG_MODEL", "u2net"))
    c2.add_argument("--cache-dir", default="")
    c2.set_defaults(func=cmd_composite)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()