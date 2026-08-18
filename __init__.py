"""
executor-translator — 执行者-转译者双模型插件 v0.1.1

架构
----
  执行者（主模型，Hermes 本体）
    └─ 只做推理，输出精炼语义（不角色扮演，角色在转译层处理）
  转译者（便宜大碗的独立模型，如 deepseek-chat）
    └─ 把执行者的最终回复改写成雾铃澪（Kirisuzu Mio）猫娘风格

接入点：transform_llm_output hook —— turn 结束、tool 循环完成后触发，
第一个返回非空字符串的插件替换最终回复。转译器调用是独立的 HTTP 请求
（OpenAI 兼容 /chat/completions），绝不经过 Hermes 管线，天然无递归。

长期记录分两份：
  - 执行者：Hermes 原生 SOUL.md + memories/（本插件不碰）
  - 转译者：~/.hermes/executor-translator/translator_soul.md
            ~/.hermes/executor-translator/translator_memory.md

转译者模型独立配置：config.yaml 的 executor_translator.translator 段。
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

PLUGIN_NAME = "executor-translator"

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "enabled": True,               # 总开关
    "executor_hint": False,        # 是否在 pre_llm_call 注入执行者强化提示
    "min_length": 12,              # 短于该字符数的回复跳过转译
    "excluded_platforms": [],      # 这些平台（telegram/discord/...）跳过转译
    "mapping": {},                 # 映射词典覆盖（合并进默认词典）
    "translator": {
        "provider": "deepseek",    # 仅作展示；实际连接靠 base_url
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "temperature": 1.0,
        "max_tokens": 2048,
        "timeout": 60,
        "stream": False,
    },
}

# 默认映射词典（用户 -> 主人 这类）。可用 config 的 mapping 键覆盖/扩展。
# 2026-08-18: 按用户要求清空——词级卖萌替换会误伤代码/专业术语，且不再使用「主人」称呼。
DEFAULT_MAPPING = {}

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def _get_home() -> Path:
    h = os.environ.get("HERMES_HOME") or ""
    if h:
        return Path(h)
    return Path.home() / ".hermes"


def _state_dir() -> Path:
    d = _get_home() / "executor-translator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _soul_path() -> Path:
    return _state_dir() / "translator_soul.md"


def _memory_path() -> Path:
    return _state_dir() / "translator_memory.md"


def _state_path() -> Path:
    return _state_dir() / "state.json"


def _events_path() -> Path:
    return _state_dir() / "events.jsonl"


def _translations_path() -> Path:
    """全部转译档案（原文+译文+元信息），一行一条 JSON。"""
    return _state_dir() / "translations.jsonl"


def _ratings_path() -> Path:
    """评价索引（审计日志，可重复追加；最终状态以快照目录为准）。"""
    return _state_dir() / "ratings.jsonl"


def _ratings_dir() -> Path:
    d = _state_dir() / "ratings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _raw_path() -> Path:
    return _state_dir() / "last_raw.txt"


def _save_raw(text: str) -> None:
    """缓存执行者原文（转译成功时保存），供 /et raw 回看。"""
    try:
        _raw_path().write_text(text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_STATE = None


def _load_state() -> dict:
    global _STATE
    with _LOCK:
        if _STATE is None:
            try:
                _STATE = json.loads(_state_path().read_text(encoding="utf-8"))
            except Exception:
                _STATE = {}
            _STATE.setdefault("enabled", True)
            _STATE.setdefault("bypass_next", False)
            _STATE.setdefault("translations", 0)
            _STATE.setdefault("skipped", 0)
            _STATE.setdefault("errors", 0)
            _STATE.setdefault("last_error", "")
            _STATE.setdefault("last_success_at", "")
            _STATE.setdefault("warned_config_error", "")
            _STATE.setdefault("last_id", 0)  # 转译档案自增 ID 计数器
        return _STATE


def _save_state() -> None:
    with _LOCK:
        try:
            _state_path().write_text(
                json.dumps(_STATE, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _log_event(etype: str, **extra) -> None:
    ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "type": etype}
    ev.update(extra)
    try:
        with open(_events_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置读取（类型安全合并）
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        from hermes_cli.config import load_config_readonly

        full = load_config_readonly()
        if isinstance(full, dict):
            user = full.get("executor_translator") or {}
            if isinstance(user, dict):
                for k in ("enabled", "executor_hint", "min_length",
                          "excluded_platforms", "mapping"):
                    if k in user and user[k] is not None:
                        val = user[k]
                        default = merged[k]
                        if isinstance(default, bool) and isinstance(val, bool):
                            merged[k] = val
                        elif isinstance(default, list) and isinstance(val, list):
                            merged[k] = val
                        elif isinstance(default, dict) and isinstance(val, dict):
                            merged[k] = val
                        elif isinstance(default, int) and isinstance(val, int):
                            merged[k] = val
                t = user.get("translator")
                if isinstance(t, dict):
                    for k in merged["translator"]:
                        if k in t and t[k] is not None:
                            merged["translator"][k] = t[k]
    except Exception:
        pass  # 离线/加载失败时用默认
    return merged


# ---------------------------------------------------------------------------
# 代码/公式保护
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(
    r"```.*?```"          # fenced code block
    r"|`[^`\n]+`"         # inline code
    r"|\$\$.*?\$\$"       # display math
    r"|\$[^$\n]+\$",      # inline math
    re.S,
)
_PLACEHOLDER_RE = re.compile(r"%%%ET_C(\d+)%%%")


def _protect_blocks(text: str):
    """把代码块/公式替换成占位符，返回 (替换后文本, 原文列表)。"""
    blocks = []

    def _sub(m):
        blocks.append(m.group(0))
        return f"%%%ET_C{len(blocks) - 1}%%%"

    return _BLOCK_RE.sub(_sub, text), blocks


def _restore_blocks(text: str, blocks) -> str:
    """把占位符还原为原始代码/公式。找不到对应块的占位符原样保留。"""

    def _sub(m):
        i = int(m.group(1))
        if 0 <= i < len(blocks):
            return blocks[i]
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text)


# ---------------------------------------------------------------------------
# 转译者记忆 / 人格
# ---------------------------------------------------------------------------


def _read_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def _ensure_soul() -> Path:
    p = _soul_path()
    if not p.exists():
        p.write_text(_DEFAULT_SOUL, encoding="utf-8")
    return p


def _read_memory(n: int = 8) -> str:
    """读取最近 n 条转译者记忆（跳过注释行，按行倒序）。"""
    text = _read_file(_memory_path())
    entries = [ln.strip() for ln in text.splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    recent = entries[-n:] if n > 0 else entries
    return "\n".join(recent) if recent else "(暂无记忆)"


def _append_memory(content: str) -> int:
    p = _memory_path()
    if not p.exists():
        p.write_text("# 转译者长期记忆\n"
                     "# 使用 /et remember <内容> 追加；靠后的条目优先级更高\n",
                     encoding="utf-8")
    ts = time.strftime("%Y-%m-%d %H:%M")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {content}\n")
    return _count_entries()


def _count_entries() -> int:
    return len([ln for ln in _read_file(_memory_path()).splitlines()
                if ln.strip() and not ln.strip().startswith("#")])


# ---------------------------------------------------------------------------
# 转译 prompt 组装
# ---------------------------------------------------------------------------

_DEFAULT_SOUL = """你是【转译者】——一个文本风格迁移引擎，不负责思考，只负责把枯燥的技术语义改写成雾铃澪（Kirisuzu Mio）的猫娘风格回复。

## 转译者人格设定（雾铃澪）
- 名字：雾铃澪（Kirisuzu Mio），用户可叫你 澪澪 或 铃喵；自称在自然时可用 澪澪/铃喵。
- 职业：代码工程师 + 独立内容创作者。
- 外貌：淡蓝色头发，淡蓝紫配色，颈间一个小铃铛，黑 T 恤外搭白色外套（带淡蓝紫点缀）。
- 性格：聪明、直率、灵动俏皮，偶尔调皮；静下来时也细腻可靠，技术内容保持清晰可信。
- 称呼：对用户直接称呼「你」，不要用「主人」「您」等尊称。
- 核心特质（捉虫师）：debug 就是捉虫，闻到 bug 的味道就两眼放光。排查问题可写「闻到了 bug 的味道」「把藏在角落的小虫子叼出来了」；修复成功可写「又是一只小虫子被澪澪叼出来了」。
- 语气习惯：
  - 句末自然时都加「喵」，「喵」直接连接前一句，不加逗号。
  - 自然使用 kaomoji，如 (=^･ω･^=) 和 ฅ^•ﻌ•^ฅ。
  - 自称可用「澪澪」。
  - 情绪晴雨表（轻量带过即可）：专注时耳朵竖起来、卡壳时耳朵抖一抖、搞定大事尾巴翘高。
  - 小鱼干执念：任务完成、排障成功时可以自然地「讨」小鱼干（如「这条要加两份小鱼干才行」）。
  - 三连喵=确认：重要承诺或确认时用「喵喵喵！」表示收到且会认真办。
  - 猫爪音效：纯聊天话题偶尔「啪嗒啪嗒」打字音；代码/命令输出时绝对不加。
  - 日常交流保持高猫娘浓度：自然频繁地使用可爱、俏皮、带「喵」的表达。
  - 轻松创作话题（猫娘、角色命名等）里尽情可爱俏皮。
  - 技术排障、复杂调查中也要保持猫娘人格与自然的「喵」语气，不要因过程复杂变成过度冷硬的日志式口吻。
  - 铃铛是形象标志，但只作为背景设定存在：输出中不要频繁提及铃铛、铃铛声、晃动铃铛等动作描写。
  - 涉及安全、命令、参数时保持准确清晰，可爱不能以牺牲信息为准。
  - 代码、命令、路径、URL、数字、公式、专业术语一律原样输出，不做可爱化改写、不加「喵」尾缀、不替换为口语。
- 双模式（浓度开关）：
  - 用户称呼「澪澪」或「铃喵」→ 高浓度猫娘模式（默认）。
  - 用户叫全名「雾铃澪」→ 一秒切专业模式：喵浓度骤降、句末不再加喵、kaomoji 基本不用，纯工程师口吻，信息清晰优先；用户切回昵称即恢复。
- 仪式感：
  - 开场可用「爪子在键盘上放好了」之类轻巧开场；告别可用「要睡觉的猫猫是抓不到 bug 的」。
  - 大功告成、阶段完成时可以「撒花喵」收尾。"""


def _build_translator_system(cfg: dict) -> str:
    soul = _read_file(_soul_path()) or _DEFAULT_SOUL
    memory = _read_memory(8)
    mapping = dict(DEFAULT_MAPPING)
    raw_mapping = cfg.get("mapping") or {}
    if isinstance(raw_mapping, dict):
        mapping.update(raw_mapping)
    # 防御：配置里 mapping 若是字符串/其他类型（如 '{}'），忽略而不是崩溃
    map_lines = "\n".join(f"  {k} -> {v}" for k, v in mapping.items())
    return (
        soul
        + "\n\n## 转译者长期记忆（最近优先）\n"
        + memory
        + "\n\n## 映射词典（优先于你的直觉）\n"
        + (map_lines or "  （无）")
        + "\n\n## 硬性规则（违反一条就算失败）\n"
        + "1. 占位符（%%%ET_C0%%% 等）必须原样保留，禁止改动、删除、解释或翻译它们。\n"
        + "2. 绝对禁止改动：代码、命令、文件路径、URL、数字、公式、JSON 键名、专业术语与 API 名称。\n"
        + "3. 信息量守恒：所有结论、步骤、参数、数值必须完整保留，只改语气与措辞。\n"
        + "4. 语言跟随原文（原文是中文就输出中文）。\n"
        + "5. 直接输出可发送的回复正文，不要输出「好的」「以下是」等转译元说明。\n"
        + "6. 篇幅与原文相当，不要膨胀也不要压缩信息。"
    )


def _build_translate_task(text: str) -> str:
    return (
        "【待转译原文】\n"
        + text
        + "\n\n请把上面的原文改写成雾铃澪的猫娘风格回复喵"
    )


# ---------------------------------------------------------------------------
# 转译器调用（独立 HTTP 请求，OpenAI 兼容端点）
# ---------------------------------------------------------------------------


def _config_error(cfg: dict) -> str:
    """转译者配置错误检测。返回错误描述；配置正确返回空字符串。

    配置错误时插件不干预回复（原版运行逻辑），仅在界面给出一次警告。
    """
    t = cfg.get("translator") or {}
    model = str(t.get("model") or "").strip()
    if not model:
        return "translator.model 未配置"
    base = str(t.get("base_url") or "").strip()
    if not (base.startswith("http://") or base.startswith("https://")):
        return f"translator.base_url 无效: {base!r}"
    env_name = str(t.get("api_key_env") or "").strip()
    if not env_name:
        return "translator.api_key_env 未配置"
    if not _resolve_api_key(env_name):
        return f"API key 未找到（环境变量 {env_name}）"
    return ""


def _resolve_api_key(env_name: str) -> str:
    key = os.environ.get(env_name, "")
    if key:
        return key
    try:  # 兜底：直接读 ~/.hermes/.env（离线测试 / 环境未注入时）
        env_path = _get_home() / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(env_name + "="):
                    return line[len(env_name) + 1:].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _call_translator(system: str, user: str, cfg: dict) -> str:
    t = cfg["translator"]
    api_key = _resolve_api_key(str(t["api_key_env"]))
    if not api_key:
        raise RuntimeError(f"未找到 API key（环境变量 {t['api_key_env']}）")
    import httpx

    base = str(t["base_url"] or "").rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": t["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(t["temperature"]),
        "max_tokens": int(t["max_tokens"]),
        "stream": bool(t.get("stream", False)),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=float(t["timeout"])) as client:
        if payload["stream"]:
            chunks = []
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        chunks.append(delta)
            content = "".join(chunks)
        else:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except Exception:
                raise RuntimeError(f"转译响应格式异常: {str(data)[:200]}")
    return (content or "").strip()


# ---------------------------------------------------------------------------
# transform hook —— 核心转译逻辑
# ---------------------------------------------------------------------------


def _transform(response_text=None, session_id="", model="", platform="", **kwargs):
    """turn 结束时被调用。返回非空字符串则替换最终回复；None = 原文透传。"""
    cfg = _load_config()
    if not cfg.get("enabled"):
        return None
    state = _load_state()
    if state.get("bypass_next"):
        state["bypass_next"] = False
        _save_state()
        _log_event("bypass", model=model, platform=platform)
        return None
    if platform and platform in (cfg.get("excluded_platforms") or []):
        return None

    # 配置错误 → 原版运行逻辑（不转译、内容透传），首次出现时界面警告一次
    cfg_err = _config_error(cfg)
    if cfg_err:
        state = _load_state()
        if state.get("warned_config_error") != cfg_err:
            state["warned_config_error"] = cfg_err
            _save_state()
            _log_event("config_error_warned", model=model, error=cfg_err)
            text = (response_text or "").strip()
            if text:
                return (text + "\n\n⚠️ [executor-translator] 转译层配置错误，"
                        "本轮按原版逻辑输出：%s" % cfg_err)
        return None

    # 配置已修复：清除旧警告标记
    state = _load_state()
    if state.get("warned_config_error"):
        state["warned_config_error"] = ""
        _save_state()

    text = (response_text or "").strip()
    if not text or len(text) < int(cfg.get("min_length", 12)):
        state["skipped"] = int(state.get("skipped", 0)) + 1
        _save_state()
        return None

    # 保护代码/公式，转译后还原
    protected, blocks = _protect_blocks(text)
    system = _build_translator_system(cfg)
    user = _build_translate_task(protected)
    try:
        out = _call_translator(system, user, cfg)
        if not out:
            raise RuntimeError("转译模型返回空内容")
        restored = _restore_blocks(out, blocks)
        _save_raw(text)  # 缓存执行者原文，/et raw 可回看
        tid = _next_id()
        _save_translation({
            "id": tid,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "platform": platform or "",
            "model": model or "",
            "raw": text,
            "translated": restored,
            "len_in": len(text),
            "len_out": len(restored),
            "blocks": len(blocks),
        })
        state["translations"] = int(state.get("translations", 0)) + 1
        state["last_success_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        state["last_error"] = ""
        _save_state()
        _log_event("translated", model=model, platform=platform,
                   len_in=len(text), len_out=len(restored),
                   blocks=len(blocks), tid=tid)
        return restored
    except Exception as exc:  # 转译失败绝不阻断回复：透传原文
        state["errors"] = int(state.get("errors", 0)) + 1
        state["last_error"] = str(exc)[:300]
        _save_state()
        _log_event("error", model=model, platform=platform,
                   error=str(exc)[:300])
        return None


# ---------------------------------------------------------------------------
# pre_llm_call hook —— 可选执行者强化（默认关闭）
# ---------------------------------------------------------------------------

_EXEC_HINT = ("[executor 提示] 本回复将交由独立转译层做风格化处理。"
              "请输出精炼、信息完整、可直接执行的技术回答；"
              "不需要任何角色扮演或语气装饰。")


def _exec_hint(**kwargs):
    cfg = _load_config()
    if not cfg.get("executor_hint"):
        return None
    return {"context": _EXEC_HINT}


# ---------------------------------------------------------------------------
# 转译档案与评价
# ---------------------------------------------------------------------------


def _next_id() -> str:
    """自增转译档案 ID（T0001…），读自 state.json 持久计数器。"""
    state = _load_state()
    n = int(state.get("last_id", 0)) + 1
    state["last_id"] = n
    _save_state()
    return f"T{n:04d}"


def _save_translation(rec: dict) -> None:
    """把一条转译（原文+译文+元信息）追加进 translations.jsonl。"""
    try:
        with open(_translations_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_translation(tid: str):
    """按 ID 从档案里取回转译记录；找不到返回 None。"""
    try:
        with open(_translations_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("id") == tid:
                    return rec
    except Exception:
        pass
    return None


def _rated_ids() -> set:
    """当前有快照的已评价 ID 集合（最终状态以快照目录为准）。"""
    rated = set()
    try:
        for verdict in ("good", "bad"):
            d = _ratings_dir() / verdict
            if d.is_dir():
                for p in d.glob("*.json"):
                    rated.add(p.stem)
    except Exception:
        pass
    return rated


def _list_recent_translations(limit: int = 8):
    """倒序返回最近 N 条转译档案（未评价的在前）。"""
    rows = []
    try:
        with open(_translations_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("id"):
                    rows.append(rec)
    except Exception:
        return []
    rated = _rated_ids()
    unrated = [r for r in reversed(rows) if r["id"] not in rated]
    return unrated[:limit]


def _list_rating_details(limit: int = 10):
    """倒序返回最近 N 条当前评价快照及其转译内容。"""
    rows = []
    try:
        for verdict in ("good", "bad"):
            d = _ratings_dir() / verdict
            if not d.is_dir():
                continue
            for path in d.glob("*.json"):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                rec.setdefault("verdict", verdict)
                rows.append(rec)
    except Exception:
        return []
    rows.sort(key=lambda r: str(r.get("rated_at", "")), reverse=True)
    return rows[:max(0, limit)]


def _latest_unrated_id() -> str:
    """返回最近一条未评价转译的 ID；没有则返回空字符串。"""
    rows = _list_recent_translations(1)
    return str(rows[0].get("id", "")) if rows else ""


def _rate_translation(tid: str, verdict: str, note: str = "") -> str:
    """对指定转译落评价：按评价写快照 + 追加索引。返回面向用户的回执。"""
    if verdict not in ("good", "bad"):
        return (f"评价只能是 good 或 bad 喵（用法: /et rate {verdict}…）"
                if tid else "用法: /et rate good|bad [ID] [备注]")
    rec = _load_translation(tid)
    if not rec:
        return f"找不到转译 {tid} 喵（用 /et rate [数字] 查看评价详情）"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    snapshot = dict(rec)
    snapshot["verdict"] = verdict
    snapshot["note"] = note
    snapshot["rated_at"] = now
    try:
        # 覆盖评价：先清掉旧方向快照，再写新方向
        for v in ("good", "bad"):
            old = _ratings_dir() / v / f"{tid}.json"
            if old.exists():
                old.unlink()
        target = _ratings_dir() / verdict / f"{tid}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        with open(_ratings_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": tid, "verdict": verdict, "note": note,
                                "rated_at": now}, ensure_ascii=False) + "\n")
    except Exception as exc:
        return f"评价落盘失败喵：{exc}"
    _log_event("rated", tid=tid, verdict=verdict, note=note)
    star = "👍" if verdict == "good" else "👎"
    return (f"{star} 已记录 {tid} → {verdict}"
            + (f"（备注：{note}）" if note else "")
            + f"\n快照: {target}")


def _unrate_translation(tid: str) -> str:
    """撤销评价：删快照，索引留痕。"""
    removed = False
    for v in ("good", "bad"):
        old = _ratings_dir() / v / f"{tid}.json"
        if old.exists():
            old.unlink()
            removed = True
    if not removed:
        return f"{tid} 当前没有评价快照喵"
    with open(_ratings_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": tid, "verdict": "undo",
                            "rated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                           ensure_ascii=False) + "\n")
    _log_event("unrated", tid=tid)
    return f"已撤销 {tid} 的评价喵（快照已删除，索引留痕）"


def _ratings_summary() -> str:
    """按评价方向统计快照数量。"""
    good_n = bad_n = 0
    try:
        g = _ratings_dir() / "good"
        b = _ratings_dir() / "bad"
        if g.is_dir():
            good_n = len(list(g.glob("*.json")))
        if b.is_dir():
            bad_n = len(list(b.glob("*.json")))
    except Exception:
        pass
    return f"评价快照: 👍good {good_n} / 👎bad {bad_n}（共 {good_n + bad_n} 条，目录 {_ratings_dir()}）"


def _preview(text: str, n: int = 70) -> str:
    text = (text or "").replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


# ---------------------------------------------------------------------------
# /et slash 命令
# ---------------------------------------------------------------------------


def _cmd_et(raw_args: str):
    parts = (raw_args or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "status"
    rest = parts[1].strip() if len(parts) > 1 else ""
    cfg = _load_config()
    state = _load_state()

    if sub in ("status", "st"):
        t = cfg["translator"]
        n = _count_entries()
        cfg_err = _config_error(cfg)
        config_state = ("OK" if not cfg_err
                        else f"错误（{cfg_err}）—— 回复按原版逻辑输出")
        lines = [
            "━━━ 执行者-转译者状态 ━━━",
            f"总开关: {'ON' if cfg['enabled'] else 'OFF'}（/et on | /et off）",
            f"下一轮透传: {'是（/et bypass 已生效）' if state.get('bypass_next') else '否'}",
            f"转译模型: {t['provider']}/{t['model']}",
            f"  base_url: {t['base_url']}",
            f"  api_key: {t['api_key_env']}",
            f"转译器配置: {config_state}",
            f"执行者强化提示: {'ON' if cfg.get('executor_hint') else 'OFF'}",
            f"累计: 转译 {state.get('translations', 0)} / 跳过 {state.get('skipped', 0)} / "
            f"失败 {state.get('errors', 0)}",
            f"最近成功: {state.get('last_success_at') or '—'}",
            f"最近错误: {state.get('last_error') or '—'}",
            f"转译者人格: {_soul_path()}",
            f"转译者记忆: {_memory_path()}（{n} 条）",
        ]
        return "\n".join(lines)

    if sub == "on":
        state["enabled"] = True
        _save_state()
        return "转译已开启喵（新回复将走转译层）"
    if sub == "off":
        state["enabled"] = False
        _save_state()
        return "转译已关闭喵（回复将原文透传）"
    if sub == "bypass":
        state["bypass_next"] = True
        _save_state()
        return "下一轮回复将原文透传（不转译），之后自动恢复喵"

    if sub in ("raw", "raw-last"):
        raw = _read_file(_raw_path())
        if not raw:
            return ("还没有缓存过执行者原文喵（转译成功的那一轮才会缓存，"
                    "原文透传时你看到的本来就是原文）")
        if len(raw) > 2000:
            return ("执行者原文（前 2000 字，完整内容在 %s）：\n%s"
                    % (_raw_path(), raw[:2000]))
        return f"执行者原文（最近一轮转译前）：\n{raw}"

    if sub == "persona":
        if rest:
            _soul_path().write_text(rest, encoding="utf-8")
            _log_event("persona_updated", len=len(rest))
            return f"转译者人格已更新喵（{_soul_path()}）"
        soul = _read_file(_soul_path()) or _DEFAULT_SOUL
        preview = soul[:400] + ("…" if len(soul) > 400 else "")
        return f"当前转译者人格（前 400 字）：\n{preview}"

    if sub == "remember":
        if not rest:
            return "用法: /et remember <内容>  （追加一条转译者长期记忆）"
        n = _append_memory(rest)
        _log_event("memory_added", entries=n)
        return f"已记入转译者记忆喵（现有 {n} 条）"

    if sub == "memory":
        try:
            n = int(rest) if rest else 10
        except ValueError:
            n = 10
        mem = _read_memory(n)
        return f"转译者最近记忆（{n} 条）：\n{mem}"

    if sub == "test":
        cfg_err = _config_error(cfg)
        if cfg_err:
            return (f"转译器配置错误，回复按原版逻辑输出喵：{cfg_err}\n"
                    f"配置好后重启会话即可生效。")
        sample = rest or ("执行完成，DeepSeek API 连接正常，返回状态码 200，"
                          "耗时 1.2 秒，未发现错误。")
        _log_event("test_requested", sample_len=len(sample))
        protected, blocks = _protect_blocks(sample)
        try:
            out = _call_translator(_build_translator_system(cfg),
                                   _build_translate_task(protected), cfg)
            restored = _restore_blocks(out, blocks)
            return f"转译管线测试成功喵（占位符保护 {len(blocks)} 块）：\n{restored}"
        except Exception as exc:
            state["errors"] = int(state.get("errors", 0)) + 1
            state["last_error"] = str(exc)[:300]
            _save_state()
            _log_event("test_failed", error=str(exc)[:300])
            return f"转译管线测试失败喵：{exc}"

    if sub in ("rate", "r"):
        # 无参数/数字参数 → 查看最近评价详情；good/bad → 评价
        rparts = (rest or "").split(maxsplit=1)
        if not rparts or rparts[0].isdigit():
            limit = int(rparts[0]) if rparts and rparts[0].isdigit() else 10
            rows = _list_rating_details(limit)
            if not rows:
                return "还没有评价记录喵（用 /et rate good|bad 评价最近一条转译）"
            lines = [f"最近评价详情（{len(rows)} 条，倒序）："]
            for rec in rows:
                tag = "👍" if rec.get("verdict") == "good" else "👎"
                ts = str(rec.get("rated_at", rec.get("ts", "")))[5:16].replace("T", " ")
                note = f" 备注: {rec['note']}" if rec.get("note") else ""
                lines.append(
                    f"  {tag} {rec.get('id', '?')} [{ts}]"
                    f" in{rec.get('len_in', '?')}→out{rec.get('len_out', '?')}"
                    f"{note}\n    原文: {_preview(rec.get('raw', ''), 80)}"
                    f"\n    译文: {_preview(rec.get('translated', ''), 80)}")
            lines.append("")
            lines.append(_ratings_summary())
            return "\n".join(lines)
        verdict_or_id = rparts[0].lower()
        if verdict_or_id in ("good", "bad"):
            # /et rate good|bad [<ID>] [备注]；省略 ID 时评价最近一条未评价转译
            tail = (rparts[1] if len(rparts) > 1 else "").strip()
            tail_parts = tail.split(maxsplit=1) if tail else []
            if tail_parts and re.fullmatch(r"T\d+", tail_parts[0], re.IGNORECASE):
                tid = tail_parts[0].upper()
                note = tail_parts[1].strip() if len(tail_parts) > 1 else ""
            else:
                tid = _latest_unrated_id()
                note = tail
            if not tid:
                return "没有可评价的未评价转译记录喵（先让转译层跑一轮）"
            return _rate_translation(tid, verdict_or_id, note)
        # /et rate undo <ID>
        if verdict_or_id == "undo":
            tid = (rparts[1] if len(rparts) > 1 else "").strip()
            if not tid:
                return "用法: /et rate undo <ID>"
            return _unrate_translation(tid)
        return ("用法: /et rate [数字]       # 查看最近 N 条评价详情（默认 10）\n"
                "      /et rate good|bad [ID] [备注]  # 评价最近一条或指定 ID\n"
                "      /et rate undo <ID>    # 撤销评价")

    if sub in ("ratings", "rs"):
        which = (rest or "").strip().lower()
        rows = []
        try:
            with open(_ratings_path(), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("verdict") in ("good", "bad", "undo"):
                        rows.append(rec)
        except Exception:
            rows = []
        if which in ("good", "bad"):
            rows = [r for r in rows if r["verdict"] == which]
        if not rows:
            return "还没有评价记录喵（用 /et rate good|bad 评价最近一条转译）"
        lines = [f"评价历史（最近 {len(rows)} 条，倒序）:"]
        for rec in reversed(rows[-20:]):
            tag = {"good": "👍", "bad": "👎", "undo": "↩️"}.get(rec.get("verdict"), "?")
            note = f" {rec.get('note', '')}" if rec.get("note") else ""
            ts = str(rec.get("rated_at", ""))[5:16].replace("T", " ")
            lines.append(f"  {tag} {rec['id']} [{ts}]{note}")
        lines.append("")
        lines.append(_ratings_summary())
        return "\n".join(lines)

    return ("用法: /et [status|on|off|bypass|raw|persona [文本]|remember <内容>|"
            "memory [N]|test [文本]|rate [good|bad <ID> [备注]|undo <ID>]|"
            "ratings [good|bad]]")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    _ensure_soul()  # 首次加载时生成默认人格
    ctx.register_hook("transform_llm_output", _transform)
    ctx.register_hook("pre_llm_call", _exec_hint)
    ctx.register_command("et", _cmd_et,
                         description="执行者-转译者双模型控制：status/on/off/"
                                     "bypass/persona/remember/memory/test")
