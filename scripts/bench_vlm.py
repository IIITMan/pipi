# -*- coding: utf-8 -*-
"""云端视觉识别（VLM）基准：速度分布 + 超时率。

与 orchestrator._vlm_describe 完全同链路（同 prompt / 6s 超时），
每次从摄像头抓真实帧，编码 JPEG 后请求，统计耗时分布。

用法：
  python scripts/bench_vlm.py                     # 用当前配置（vision.vlm_model，缺省用主模型）
  python scripts/bench_vlm.py ep-2026xxxxxxx      # 指定 endpoint/模型
  python scripts/bench_vlm.py ep-xxxx 10 8.0      # 指定次数与超时秒
"""
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pipi.infra import Config                       # noqa: E402
from pipi.providers.llm.openai_compat import OpenAICompatLLM  # noqa: E402
from pipi.hardware.sim import OpenCVCamera          # noqa: E402

SYSTEM = ("你是机器人皮皮的眼睛。仔细观察画面，用中文描述："
          "有什么人/物品、衣着颜色、动作、表情，以及这个人在"
          "做什么、可能想干什么。100 字以内，不要寒暄。")


async def main() -> None:
    model_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0

    cfg = Config.default()
    base = cfg.get("llm.base_url", "")
    key = cfg.get("llm.api_key", "")
    # 与 orchestrator 一致：未显式指定且 vision.vlm_model 为空时用主 LLM
    model = (model_arg or cfg.get("vision.vlm_model", "")
             or cfg.get("llm.model", ""))
    if not (base and key and model):
        print(f"[缺配置] base_url={bool(base)} api_key={bool(key)} "
              f"model={model!r}")
        return
    print(f"目标模型: {model} ｜ 次数 {n} ｜ 超时 {timeout:.0f}s\n")
    # 与 orchestrator 一致：关思考（混合推理模型不关会截断/空响应）
    llm = OpenAICompatLLM(base_url=base, api_key=key, model=model,
                          name="vlm-bench", timeout_s=30,
                          extra_body={"thinking": {"type": "disabled"}})

    import base64
    import cv2

    cam = OpenCVCamera(index=0, width=640, height=480)
    await cam.open()
    if cam._cap is None or not cam._cap.isOpened():
        print("[摄像头不可用] 测试中止")
        return

    results = []
    for i in range(1, n + 1):
        frame = await cam.read()
        if frame is None:
            print(f"  #{i:02d} 抓帧失败（跳过）")
            continue
        small = cv2.resize(frame, (480, 360))
        ok, buf = cv2.imencode(".jpg", small,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        b64 = "data:image/jpeg;base64," + base64.b64encode(buf).decode()
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "描述你现在看到的画面"},
                {"type": "image_url", "image_url": {"url": b64}},
            ]},
        ]
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                llm.chat(messages, temperature=0.3, max_tokens=200),
                timeout=timeout)
            dt = time.perf_counter() - t0
            text = (getattr(resp, "text", "") or "").strip()
            results.append((dt, "ok" if text else "empty"))
            print(f"  #{i:02d} {dt:5.2f}s  {'ok' if text else '空响应'}"
                  f"  {text[:30]}")
        except asyncio.TimeoutError:
            results.append((time.perf_counter() - t0, "timeout"))
            print(f"  #{i:02d} >{timeout:.0f}s  超时")
        except Exception as exc:
            results.append((time.perf_counter() - t0, "error"))
            print(f"  #{i:02d} 异常 {type(exc).__name__}: {str(exc)[:80]}")
        await asyncio.sleep(1.0)
    await cam.close()

    ok = [r[0] for r in results if r[1] == "ok"]
    n_to = sum(1 for r in results if r[1] == "timeout")
    n_em = sum(1 for r in results if r[1] == "empty")
    print(f"\n========== 统计（{model}）==========")
    print(f"成功 {len(ok)}/{len(results)} ｜ 超时 {n_to} ｜ 空响应 {n_em}")
    if ok:
        ds = sorted(ok)
        print(f"耗时：均值 {statistics.mean(ds):.2f}s ｜ "
              f"中位 {statistics.median(ds):.2f}s ｜ 最快 {ds[0]:.2f}s ｜ "
              f"最慢 {ds[-1]:.2f}s")
    if n_to:
        print(f"超时率 {n_to / max(len(results), 1) * 100:.0f}% —— "
              f"对话中这些轮次走本地视觉降级")


if __name__ == "__main__":
    asyncio.run(main())
