# -*- coding: utf-8 -*-
r"""ascii_view.py — 把图片降采样成 ASCII 亮度图，用于"看"图（本模型无法直接读图）。
用法: python ascii_view.py <img> [cols]"""
import sys
from PIL import Image

RAMP = " .:-=+*#%@"  # 暗->亮


def main():
    path = sys.argv[1]
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 56
    im = Image.open(path).convert("L")
    w, h = im.size
    # 行按 16:9-ish 字符宽高比补偿：字符比像素高约2倍，故 rows 用 cols * (h/w) / 2
    rows = max(8, int(cols * (h / w) * 0.5))
    im = im.resize((cols, rows))
    px = list(im.getdata())
    lines = []
    for r in range(rows):
        line = ""
        for c in range(cols):
            v = px[r * cols + c]
            line += RAMP[min(len(RAMP) - 1, int(v / 255 * len(RAMP)))]
        lines.append(line)
    print("SIZE %dx%d  ->  %dx%d ascii" % (w, h, cols, rows))
    for i, l in enumerate(lines):
        print("%3d|%s|" % (i, l))


if __name__ == "__main__":
    main()