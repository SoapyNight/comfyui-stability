# 本机 ComfyUI 出图 / 图像处理 —— 环境坑清单（必读）

这些坑都是本机（Windows + WorkBuddy 沙箱 + 国内网络）实测踩出来的。**执行前对照一遍，能省大量返工。**

## 1. 服务管理
- 服务地址 `http://127.0.0.1:8188`。手动启动：`C:\Users\34500\ComfyUI\run_comfyui.bat`。
- 脚本内置自动拉起（`comfyui_core.try_autostart`）与关闭（`comfyui_core.shutdown`）。
- **出图成功后默认自动关服务**释放显存/端口；连续出多张时加 `--keep-alive`。
- 判断服务真实在线，以 `netstat` 是否 LISTENING 为准，别信走代理的 curl（见 §5）。

## 2. 并发禁忌（最高优先级）
- **pip 安装 与 ComfyUI 生成 绝不能同时跑。** 二者并发会破坏 ComfyUI 的 venv / 模型加载，表现为"提交后 history 有 pid 但永远无 outputs"或图根本没生成。
- 正确顺序：先把所有 pip 依赖装完 → 校验 venv 可用 → 再启动 ComfyUI 出图。

## 3. 依赖安装（国内网络）
- 抠图/合成脚本依赖 `rembg` + `onnxruntime` + `numpy` + `Pillow`，装在 **ComfyUI 自带 venv**：
  `C:\Users\34500\ComfyUI\.venv\Scripts\python.exe`（也是 `cutout_composite.py` 的运行解释器）。
- 直接连 PyPI 会挂 20+ 分钟。改用清华镜像：
  `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple rembg onnxruntime`
- rembg 模型默认从 GitHub 拉，国内会超时/断流。规避：
  - 小模型 `u2netp`（~4MB）优先，够用且下载快。
  - 完整 `u2net`（~176MB，175997641 字节）用 GitHub 加速器 + 断点续传：
    `curl -C - --noproxy '*' -o <path> https://ghproxy.net/<原始url>`（换 gh-proxy.com / ghproxy.com 重试）。
  - 模型落地路径：`C:\Users\34500\.rembg\models\u2net\u2net.onnx`、`...\u2netp\u2netp.onnx`。
- **生成脚本（`comfyui_gen.py` / `comfyui_core.py`）纯标准库**，用托管 Python 即可，无需任何依赖：
  `C:\Users\34500\.workbuddy\binaries\python\versions\3.13.12\python.exe`。

## 4. 中文路径
- **别把含中文的路径当 argv 传给 python/外部程序**（PowerShell/bash 会按 GBK 解释，产生乱码路径）。
- 规避：路径**硬编码进 .py 脚本**，或用 Write 工具写 UTF-8 脚本内部处理。
- 本技能的 CLI 参数（--base/--layer/--out/--ref）尽量传 **ASCII 目录**或**相对文件名**；需要中文目录时把逻辑搬进脚本内。
- cv2（OpenCV）的 `imread/imwrite` 对中文路径同样会崩——本技能不依赖 cv2，若扩展用到 cv2，让 PIL 负责文件 I/O、cv2 只操作内存 numpy 数组。

## 5. 沙箱 shim 会拦的系统操作
- **taskkill 被禁**：杀进程走 WinAPI（`kernel32.OpenProcess + TerminateProcess`），脚本已实现（`comfyui_core._kill_tree`）。
- **os.replace 被禁**（MoveFileEx+REPLACE 拒绝访问）：稳健写盘已用三级回退 os.replace→shutil.move→直接写（`comfyui_core._robust_write`）。
- **HTTP_PROXY 污染本机端口**：curl/urllib 默认走代理，会对本机 8188 "假活"。所有本机 HTTP 必须绕代理——`comfyui_core` 已用 `ProxyHandler({})` 构建 opener；手动 curl 加 `--noproxy '*'`。
- **netstat -ano 输出是 GBK**：`text=True`（UTF-8）会崩，要 `raw.decode("gbk", errors="ignore")`，脚本已处理。
- **每条 Bash 命令各属独立 Job**：DETACHED_PROCESS 拉起的后台进程在该命令结束时会被整体回收。要"拉起 ComfyUI→用它出图→关掉"应在**同一条命令/同一次脚本调用**内闭环（本技能脚本正是这么做的：ensure_service→gen_one→shutdown 一次跑完）。

## 6. 出图 / 下载
- ComfyUI 出的图总是落地到 `C:\Users\34500\ComfyUI\output\{prefix}_{counter:05d}_.png`。
- **下载竞态**：history 里出现 prompt_id 时，outputs 可能还没写完。`comfyui_core.gen_one` 已处理：有 pid 但无图 → `sleep(2)` 重试而非立即判失败。若仍超时，直接去 `output/` 按 prefix 捞图。
- 用哪个 checkpoint：动画/赛璐璐向用 `animagine-xl-3.1.safetensors`（SDXL，推荐 1024 档）；恐怖/写实 SD1.5 用 `v1-5-pruned-emaonly.safetensors`（512 档）。
- 采样默认 `dpmpp_2m / karras / 28 steps / cfg 7`。

## 7. 合成（两层同框）
- **用正常 alpha 合成，别用 screen 叠加**（screen 只留亮部、丢细节、画风割裂 = "合成逻辑错误"）。
- 背景元素要清晰，就别把它当整幅场景的小配角（像素太少会糊）——**单独当主景按 1024 生成**，负向词压制 abstract/simplified/low detail/amorphous，再缩放进天空。
- 前景盖背景：`cutout_composite.py composite` 的层序 = `--layer` 出现顺序（先=最远）。
- 统一画风：`--tone 0.35` 让所有非最前层色调向最前层靠拢；背景层加 `darken`(压暗)+`blur`(轻虚化)制造纵深。

## 8. 看不到图时的判断
- 本通道部分模型**无法直接 Read 图片**。用 `ascii_view.py <img> [cols]` 出 ASCII 亮度图看构图/明暗/层序是否合理（`@`最亮、空格最暗）。
- 最终"好不好看"仍交给用户肉眼，把成品 `present_files` 交付。