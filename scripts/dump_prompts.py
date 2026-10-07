#!/usr/bin/env python3
"""导出当前所有 LLM 提示词的实际渲染结果，供人工审阅。

用法：
    python scripts/dump_prompts.py            # 输出到 logs/prompts/
    python scripts/dump_prompts.py --stdout   # 同时打印到终端

导出内容（与运行时完全一致的渲染路径）：
  dialogue_system.txt   应答模式的 System Prompt（人格+记忆+情绪+状态注入）
  dialogue_user.txt     对话历史 + 本轮用户输入的组装样例
  memory_extract.txt    记忆抽取（每轮后台调用）
  thought.txt           生命模块·念头生成
  micro_expression.txt  生命模块·微表达（当前走静态表，此为预留模板）
  proactive.txt         主动搭话
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipi.app import build_app
from pipi.domain.memory import RetrievedMemory

OUT = Path(__file__).resolve().parents[1] / "logs" / "prompts"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()

    app = build_app(sim=True)      # sim 模式：不起模型/网络，只取人格与配置
    out: dict[str, str] = {}
    now = app.clock.now()
    uid = app.cfg.get("user.default_user_id", "owner")

    pb = app.orchestrator.dialogue_expert.pb
    profile = app.orchestrator.get_profile(uid)
    state = app.orchestrator.get_state(uid)

    # 1) 应答模式：System + 历史 + 本轮输入
    memories = [
        RetrievedMemory(item=m, score=0.9)
        for m in app.repos.memory.list_live(uid, limit=3)
    ]
    system = pb.build_system(
        current_time="2026-10-07 14:00",
        profile=profile,
        memories=memories,
        emotion_state=state,
    )
    history = [
        {"role": "user", "content": "今天好累啊"},
        {"role": "assistant", "content": "哼，谁让你不好好休息的……要不要靠一会儿？"},
    ]
    messages = pb.build_messages(
        user_input="给我讲个笑话吧",
        history=history,
        current_time="2026-10-07 14:00",
        profile=profile,
        memories=memories,
        emotion_state=state,
    )
    out["dialogue_system"] = system
    out["dialogue_user"] = "\n\n".join(
        f"===== [{m['role']}] =====\n{m['content']}" for m in messages[1:]
    )

    # 2) 记忆抽取
    from pipi.prompts import render
    out["memory_extract"] = render(
        "memory_extract.jinja",
        user_text="对了，我下周五要去上海出差",
        reply_text="出差呀，记得带好东西，别累着了。",
    )

    # 3) 念头生成（生命模块）
    eng_state = ("活跃度3/10，疲劳2/10，情绪平静，"
                 "孤单6/10，无聊6/10，好奇4/10")
    out["thought"] = render(
        "thought.jinja",
        name=app.persona.name,
        persona_hint=f"{app.persona.name}人格（{app.persona.id}），语气傲娇但内心黏人",
        state_desc=eng_state,
        memory_flashback="主人说喜欢晚霞",
        need_hint="孤单，想找人说话",
    )

    # 4) 微表达
    out["micro_expression"] = render(
        "micro_expression.jinja",
        name=app.persona.name,
        persona_hint=f"{app.persona.name}人格（{app.persona.id}）",
        mood_desc="愉悦",
    )

    # 5) 主动搭话
    out["proactive"] = render(
        "proactive.jinja",
        event="主人回家了",
        owner_name=profile.nickname or "主人",
    ) if "{{" not in (Path(app.cfg.get("persona.personas_dir", "configs/personas"))).name else ""

    # proactive.jinja 的实际参数名以模板为准，直接读模板变量兜底
    try:
        import jinja2
        src = (Path(__file__).resolve().parents[1] / "src/pipi/prompts/proactive.jinja") \
            .read_text(encoding="utf-8")
        vars_hint = [v.strip() for v in src.split("{{")[1:] if "}}" in v]
        vars_hint = [v.split("}}")[0].strip().split("|")[0] for v in vars_hint]
        ctx = {v: {"event": "主人回家了", "owner_name": profile.nickname or "主人",
                   "name": app.persona.name}.get(v, f"<{v}>") for v in vars_hint}
        out["proactive"] = render("proactive.jinja", **ctx)
    except Exception as exc:
        out["proactive"] = f"(渲染失败：{exc})"

    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in out.items():
        path = OUT / f"{name}.txt"
        path.write_text(text, encoding="utf-8")
        print(f"  {path}  ({len(text)} 字)")
        if args.stdout:
            print("=" * 60)
            print(text)
    print(f"\n完成。共 {len(out)} 份提示词 → {OUT}")
    app.close()


if __name__ == "__main__":
    main()
