#!/usr/bin/env python3
"""模型下载 —— 把本地推理需要的模型拉到 data/models/ 下。

全部模型都是**可选**的：任何一步失败，系统会自动降级到云端或 Mock，
不影响跑通（见 configs/degradation.yaml）。

用法：
    python3 scripts/download_models.py            # 下载全部（能下的）
    python3 scripts/download_models.py --list     # 只列出模型与体积
    python3 scripts/download_models.py --only asr # 只下语音识别
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "data" / "models"

# key -> (说明, 体积, 下载方式)
MODELS: dict[str, tuple[str, str, str]] = {
    "asr": (
        "SenseVoiceSmall —— 语音识别 + 情感 + 音频事件（阿里 FunAudioLLM）",
        "≈ 900 MB",
        "modelscope:iic/SenseVoiceSmall",
    ),
    "embedding": (
        "bge-small-zh-v1.5 —— 中文语义向量，记忆检索用",
        "≈ 95 MB",
        "hf:BAAI/bge-small-zh-v1.5",
    ),
    "vision_det": (
        "YuNet 人脸检测（ONNX）—— 视觉检测主力，预下好运行时零下载",
        "≈ 0.3 MB",
        "url:https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    "vision_expr": (
        "HSEmotion 表情识别（可选；装不上就用 ASR 情感标签替代）",
        "≈ 40 MB",
        "pip:hsemotion-onnx",
    ),
    "vision_id": (
        "InsightFace buffalo_sc —— 人脸识别（认主）；装不上退「主人/陌生人」二分",
        "≈ 280 MB",
        "pip:insightface",
    ),
}


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd: list[str], desc: str) -> bool:
    print(f"  → {desc}")
    print(f"    $ {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"    ✗ 失败（{exc.returncode}）—— 跳过，运行时会自动降级")
        return False
    except FileNotFoundError:
        print("    ✗ 命令不存在 —— 跳过，运行时会自动降级")
        return False


def download_one(key: str, spec: tuple[str, str, str]) -> bool:
    desc, size, how = spec
    print(f"\n[{key}] {desc}  ({size})")

    if how.startswith("modelscope:"):
        mid = how.split(":", 1)[1]
        if not _has("modelscope") and not _has("ms"):
            print("    未安装 modelscope：pip install modelscope")
            print("    跳过（ASR/TTS 将降级到云 API 或 Mock）")
            return False
        local = MODELS_DIR / mid.split("/")[-1]
        return _run(
            ["modelscope", "download", "--model", mid,
             "--local_dir", str(local)],
            f"下载到 {local.relative_to(ROOT)}",
        )

    if how.startswith("hf:"):
        mid = how.split(":", 1)[1]
        local = MODELS_DIR / mid.split("/")[-1]
        if _has("huggingface-cli"):
            return _run(
                ["huggingface-cli", "download", mid,
                 "--local-dir", str(local)], f"下载到 {local.relative_to(ROOT)}")
        if _has("hf"):
            return _run(["hf", "download", mid, "--local-dir", str(local)],
                        f"下载到 {local.relative_to(ROOT)}")
        print("    未安装 huggingface-cli：pip install huggingface_hub")
        print("    跳过（向量检索将降级为 TF-IDF）")
        return False

    if how.startswith("pip:"):
        pkg = how.split(":", 1)[1]
        ok = _run([sys.executable, "-m", "pip", "install", pkg], f"pip 安装 {pkg}")
        if ok:
            _warm(pkg)   # 首次运行时的模型下载提前到部署阶段
        return ok

    if how.startswith("url:"):
        url = how.split(":", 1)[1]
        dest = MODELS_DIR / url.rsplit("/", 1)[-1].split("?")[0]
        if dest.exists():
            print(f"    已存在：{dest.name}")
            return True
        print(f"    下载 → {dest.name}")
        try:
            import urllib.request
            urllib.request.urlretrieve(url, dest)
            print(f"    完成（{dest.stat().st_size // 1024} KB）")
            return True
        except Exception as exc:
            print(f"    ✗ 下载失败：{exc}")
            return False

    return False


def _warm(pkg: str) -> None:
    """pip 装完后触发一次模型自下载（InsightFace/hsemotion 的模型在
    首次推理时才拉取 —— 预热后运行时零下载、零卡顿）。"""
    warmups = {
        "insightface": (
            "from insightface.app import FaceAnalysis;"
            "import numpy as np;"
            "a = FaceAnalysis(name='buffalo_sc', providers=['CPUExecutionProvider']);"
            "a.prepare(ctx_id=-1, det_size=(640,640));"
            "a.get(np.zeros((640,640,3), dtype=np.uint8))",
            "预热 insightface（下载 buffalo_sc 检测/识别模型）",
        ),
        "hsemotion-onnx": (
            "from hsemotion_onnx.facial_emotions import HSEmotionRecognizer;"
            "import numpy as np;"
            "m = HSEmotionRecognizer(model_name='enet_b0_8_best_afew');"
            "m.predict_emotions(np.zeros((224,224,3), dtype=np.uint8), logits=False)",
            "预热 hsemotion（下载表情识别模型）",
        ),
    }
    if pkg in warmups:
        code, desc = warmups[pkg]
        _run([sys.executable, "-c", code], desc)


def main() -> None:
    ap = argparse.ArgumentParser(description="下载皮皮的可选本地模型")
    ap.add_argument("--list", action="store_true", help="只列出，不下载")
    ap.add_argument("--only", default="", help="只处理某个 key")
    args = ap.parse_args()

    if args.list:
        print("可选的本地模型（全部缺失也能跑，会自动降级）：\n")
        for k, (desc, size, how) in MODELS.items():
            print(f"  {k:14s} {size:10s}  {desc}")
        return

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"模型目录：{MODELS_DIR.relative_to(ROOT)}")

    keys = [args.only] if args.only else list(MODELS)
    ok, fail = 0, []
    for k in keys:
        if k not in MODELS:
            print(f"未知模型 key：{k}")
            continue
        if download_one(k, MODELS[k]):
            ok += 1
        else:
            fail.append(k)

    print(f"\n完成：{ok} 个可用，{len(fail)} 个跳过")
    if fail:
        print(f"  跳过：{', '.join(fail)}")
        print("  这些能力在运行时会自动走降级链，不影响启动。")


if __name__ == "__main__":
    main()
