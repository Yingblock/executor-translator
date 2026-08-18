# executor-translator — 执行者-转译者双模型插件

当前版本：`0.1.1`

主模型（执行者）只负责推理、输出精炼语义；转译模型负责把最终回复
改写成配置的目标风格。执行者负责事实与技术内容，转译者负责表达，
两者各司其职。

## 架构

```
主模型（执行者）             转译者（如 deepseek-chat）
┌─────────────────┐          ┌──────────────────────────┐
│ 推理 / 工具调用   │          │ 文本风格迁移（不改信息）     │
│ 输出精炼语义     │─────────>│ 注入：目标人格 + 长期记忆    │
│ 不角色扮演       │  transform│  + 映射词典                 │
└─────────────────┘   hook    └───────────┬──────────────┘
        ▲                                  │
        │ 独立 HTTP 调用（OpenAI 兼容端点）   │ 失败→原文透传
        └──────────────────────────────────┘
```

- 接入点：`transform_llm_output` hook —— turn 结束、工具循环完成后
  触发，插件返回非空字符串即替换最终回复（官方契约，第一个非空者胜）。
- 转译器调用是**独立 HTTP 请求**，绝不经过 Hermes 管线，天然无递归。
- **代码/公式保护**：转译前把 ```代码块```、`行内代码`、$公式$ 替换成
  占位符 %%%ET_C0%%%，转译后原样还原，防止转译模型「好心」改坏代码。

## 长期记录分两份

| 角色   | 存放位置                                            | 管理方式                    |
|--------|-----------------------------------------------------|-----------------------------|
| 执行者 | Hermes 原生 `~/.hermes/SOUL.md` + `memories/`       | 原生 memory 工具            |
| 转译者 | `~/.hermes/executor-translator/translator_soul.md`  | `/et persona [文本]`        |
|        | `~/.hermes/executor-translator/translator_memory.md`| `/et remember <内容>`       |

转译器的记忆是独立于 Hermes 主记忆的，互不污染：执行者记住技术事实，
转译者记住目标风格相关的表达偏好。

## 安装

1. 把整个 `executor-translator` 目录放到 Hermes 的插件目录：

```
~/.hermes/plugins/executor-translator/
├── plugin.yaml
└── __init__.py
```

2. 启用插件（官方通道）：

```bash
hermes plugins enable executor-translator
```

3. 配置（写入 `~/.hermes/config.yaml`，api key 放 `.env`）：

```yaml
plugins:
  enabled:
    - executor-translator

executor_translator:
  enabled: true
  executor_hint: false        # true=每轮给执行者注入"不要角色扮演"提示
  min_length: 12              # 短回复跳过转译
  excluded_platforms: []      # 例: [telegram] 该平台保持原文
  mapping:                    # 映射词典覆盖/扩展（默认见 __init__.py）
    "示例词": "目标词"
  translator:
    provider: deepseek        # 展示用
    model: deepseek-chat      # 便宜大碗
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY   # key 从 ~/.hermes/.env 读
    temperature: 1.0
    max_tokens: 2048
    timeout: 60
```

4. 重启 Hermes（CLI 退出重进 / gateway `/restart`）——插件在下一次
   会话才加载。

## 快速上手

1. 重启后先输入 `/et status`，确认「转译器配置: OK」。
2. 正常提问，回复自动转换为目标风格（代码/数字/结论原样保留）。
3. 想看执行者原始输出：`/et raw` 直接贴出上一轮原文，不用重新问。

## 典型用法

```
/et remember 偏好短句并保持技术术语原样       # 记录表达偏好（不影响执行者记忆）
/et persona 你是简洁、可靠的技术助手          # 设置目标人格
/et raw                                  # 回看最近一轮执行者原文
/et bypass                               # 下一轮不转译，拿原文继续追问
/et test                                 # 验证转译链路（换模型/换 key 后必跑）
/et off                                  # 临时关闭转译（回复恢复原版）
```

## Slash 命令

```
/et status            查看状态：开关、转译模型、计数、最近错误
/et on | /et off      总开关（改 state.json，即时生效）
/et bypass            下一轮回复原文透传，之后自动恢复
/et raw               直接贴出最近一轮执行者原文（转译时缓存，不用重新问）
/et persona [文本]    查看 / 设置转译者人格（translator_soul.md）
/et remember <内容>   追加一条转译者长期记忆
/et memory [N]        查看最近 N 条转译者记忆（默认 10）
/et test [文本]       真实跑一遍转译管线（需要 API key 可用）
/et rate [数字]       查看最近 N 条评价详情（数字省略时默认为 10）
/et rate good|bad [ID] [备注]
                      评价指定 ID；省略 ID 时评价最近一条未评价转译
/et rate undo <ID>    撤销指定评价
```

## 运行时产物

```
~/.hermes/executor-translator/
├── state.json             enabled / bypass_next / 计数 / 最近错误
├── events.jsonl           追加式事件日志（translated/error/bypass/...）
├── last_raw.txt           最近一轮执行者原文缓存（/et raw 读取）
├── translator_soul.md     转译者人格（/et persona 可改写）
└── translator_memory.md   转译者长期记忆（/et remember 追加）
```

## 验证

1. 重启后 `hermes plugins list` 里出现 executor-translator（enabled）。
2. 会话里 `/et status` 能看到转译模型与计数。
3. `/et test` 走一遍真实转译管线。
4. 正常提问一次，观察回复是否转换为目标风格；对比 `/et bypass` 后
   的原文输出。

## 故障排查

- **回复一直原文透传**：`/et status` 看 enabled 与 errors；转译失败
  会自动透传原文（绝不因转译层阻断回复）。
- **「未找到 API key」**：确认 `.env` 里有 `DEEPSEEK_API_KEY`（或改
  `translator.api_key_env` 指向已有的 key 环境变量名）。
- **换了便宜模型还是慢**：延迟 = 执行者耗时 + 转译耗时（通常 2~5s）。
  转译模型选更快的（deepseek-chat / deepseek-v3 等），或调小
  `max_tokens`。
- **代码被转译模型改坏**：检查占位符机制是否生效（events.jsonl 里
  `blocks` 字段 > 0 表示有保护）；仍出问题可对该条 `/et bypass`。

## 成本与延迟

- 转译只发生在最终回复（turn 结束），工具调用过程零转译开销。
- 单次转译通常 <1k tokens，deepseek-chat 极便宜。
- 串行等待两个模型是主要延迟来源；CLI 下感知为「思考完再转换风格」。
