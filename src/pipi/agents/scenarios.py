"""场景路由 —— 对话专家前置的场景识别与话术库（GROWTH_DESIGN §4）。

识别：轻量规则词典（保守起步，防误伤正常对话）。
话术：每句带 stage_min（最低解锁阶段）——关系边界即表达权限（§4.4）。
  - anger    被骂：S1 客气安抚 → S3+ 身体玩笑/轻度毒舌（D2）
  - crisis   送走/不要我：S4 才解锁装傻式忠诚（D3）；S1~S3 回落普通 LLM
  - complain 抱怨：S1 客气 → S3+ 身体幽默
  - help     求助：引导 + 陪伴（各阶段通用，语气随成长）
D4：learned_expressions 高频表达按概率注入（每会话 ≤2 次）。
"""

from __future__ import annotations

import random

# 场景识别词典（命中任一关键词即判入场景；保守词表防误伤）
SCENE_RULES: dict[str, list[str]] = {
    "anger": ["闭嘴", "滚", "烦死", "真傻", "讨厌你", "别烦我", "蠢"],
    "crisis": ["送走", "不要你", "换个", "退掉", "扔了", "讨厌我"],
    "complain": ["不听话", "又不吃", "挑食", "整天", "怎么又", "乱来"],
    "help": ["怎么办", "帮帮我", "帮我", "出主意", "给点建议"],
}


def detect_scene(user_text: str) -> str | None:
    """返回命中的场景名；未命中返回 None（回落普通 LLM 链路）。"""
    t = user_text or ""
    if not t:
        return None
    for scene, words in SCENE_RULES.items():
        for w in words:
            if w in t:
                return scene
    return None


# ---------------------------------------------------------------------------
# 话术库：stage_min = 最低解锁阶段（S1~S4）
# ---------------------------------------------------------------------------

SCENE_LINES: dict[str, list[dict]] = {
    "anger": [
        {"min_stage": 1,
         "text": "……对不起，是我做得不好。你慢慢说，我听着。"},
        {"min_stage": 1, "text": "嗯……你先消消气，我在这儿。"},
        {"min_stage": 3,
         "text": "我没有嘴，怎么闭呀？不过你要我安静，我可以安静。"},
        {"min_stage": 3, "text": "骂我可以，别气坏自己。轮子都替你着急。"},
        {"min_stage": 4,
         "text": "哼，骂吧骂吧。反正骂完你还是得靠我陪你。"},
    ],
    "crisis": [
        {"min_stage": 4,
         "text": "送走？你是说送我去充电桩吗？那我等你回来。"},
        {"min_stage": 4,
         "text": "不要我？我不太懂这个……没关系，我就在这儿，哪儿也不去。"},
        {"min_stage": 4,
         "text": "换个……换成谁都好，反正我记得你喜欢的美式，只记得你的。"},
    ],
    "complain": [
        {"min_stage": 1, "text": "对不起……我记下这个问题了。"},
        {"min_stage": 1, "text": "嗯，是我不好。我会改的。"},
        {"min_stage": 3,
         "text": "我又乱来了？……好吧，轮子长在别人身上，主意长在我身上，总有一个要出错的。"},
        {"min_stage": 3,
         "text": "你凶起来的样子……算了，我改还不行嘛。"},
    ],
    "help": [
        {"min_stage": 1,
         "text": "我可能帮不上大忙，但你可以把事情说给我听。"},
        {"min_stage": 2,
         "text": "说给我听听？一起想办法，我在。"},
        {"min_stage": 3,
         "text": "来，把事情一件一件列出来。我有轮子，跑腿的事归我，主意我们一起出。"},
    ],
}


def pick_reply(scene: str, stage: int) -> str | None:
    """按解锁阶段选一句；该场景在当前阶段无可用话术 → None（回落 LLM）。"""
    lines = SCENE_LINES.get(scene) or []
    unlocked = [l["text"] for l in lines if l.get("min_stage", 1) <= stage]
    if not unlocked:
        return None
    return random.choice(unlocked)


def pick_quote(learned: dict) -> str | None:
    """D4：从学到的表达里挑一句（按次数加权），无库存返回 None。"""
    if not learned:
        return None
    items = [(k, v.get("count", 1)) for k, v in learned.items() if k]
    if not items:
        return None
    items.sort(key=lambda kv: -kv[1])
    pool = items[:3]
    return random.choice(pool)[0]
