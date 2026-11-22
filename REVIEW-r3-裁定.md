# 第六轮裁定：E2 注入面

> 裁定人：助理。依据 coder 提出的可行性冲突 + 我的独立实测。

## 一、你是对的，我写错了

| 我任务书写的 | 实测真相 | 证据 |
|---|---|---|
| 用 `on_session_start` 注入系统提示 | **纯观察者，返回值被 ignore** | `plugins_dispatch.py:33/45` 把它列在 bounded 观察钩子组 |
| — | 唯一可写的注入面是 `register_system_prompt_section` | `hermes_cli/plugins_context.py:783` |

**顺带纠正你的一处引用**：`register_system_prompt_section` 在 `plugins_context.py:783`，不是 753。

## 二、而且你挖到了一个更严重的问题

我实测确认：

```
$ hermes plugins list | grep -i cockpit
│ Hermes Cockpit │ not enabled │ 0.1.0 │ 项目中枢 … │ user │
$ hermes plugins list | grep -i todo-panel
│ todo-panel     │ enabled     │ 0.1.0 │ 人机共用任务面… │ user │
```

**我们的插件是 not enabled 状态。** 这意味着：
- 后端 router 不会挂载
- hook 一条都不跑
- 系统提示注入不可能生效

⇒ 这比「注入面选错」严重得多：**E2 做对了也等于没做**，除非先让插件变成 enabled。

## 三、裁定

### Q1 注入面：换成 `register_system_prompt_section` ✅

按你的建议做。理由：
1. 它是宿主官方唯一承认的写 prompt 通道
2. 签名实测支持 `content: str | Callable`，callable 能拿到只读 session-info —— 正好够做「按会话判定是否有归属」
3. `max_chars` 参数天然满足 ≤80 字约束
4. ⚠️ **section id 全局唯一**，重复注册抛 `ValueError`，实现时用固定 id，不要动态生成

### Q2 要不要先验证它真能渲染？

**要，但顺序是：先让插件 enabled，再验证渲染。**

理由：现在是 `not enabled`，直接验 `register_system_prompt_section` 会得到「注册了但没生效」的假阴性，
你会误判成「这条路也不通」而去换第三条路 —— 那是把自己逼进死角。

**本轮任务顺序改为：**

1. **先补 `plugins/hermes-cockpit/__init__.py::register(ctx)`**（插件被 enabled 的前提）
   - 里面只做一件事：转调适配层去 `register_system_prompt_section`
   - `register()` 本身是宿主规定的入口函数，放 `__init__.py` 是正确的
   - 但**内部所有宿主调用必须转调 `host_adapter.py`**，别把业务逻辑写进 `__init__.py`
   - `check_boundary.py` 若把 `register` 判成越界，就加一条明确豁免（入口函数不算），
     **但 `on_session_start` / `register_hook` 的禁令保留**
2. **让插件 enabled 并实测**：`hermes plugins enable hermes-cockpit`（或等价配置写法），
   再 `hermes plugins list` 确认变成 `enabled`
3. **验证注入真的渲染**：用 `hermes -z` 起会话，让它原样输出系统提示里与本插件相关的段落；
   拿不到就说明没生效，回来报告，**不要自己换路**
4. 最后才写 E2 判定逻辑（归属 / 开关 / 80 字 / fail-open）与测试

### Q3 E3 不受影响

跑题提醒是纯行为层（`core/drift.py` + skill 文本），不走 hook，按原任务书做。

## 四、硬要求（新增）

| # | 要求 |
|---|---|
| 1 | section id 固定为 `hermes-cockpit.project`，不可每次生成新 id |
| 2 | `register()` 里不得实现业务逻辑，只转调适配层 |
| 3 | **未归项目时返回空串**（不是「当前项目：无」）—— 空串不污染 prompt |
| 4 | `reminders.enabled=false` 时同样返回空串 |
| 5 | fail-open：回调内任何异常返回空串，不让会话起不来 |
| 6 | **enabled 状态变更后必须重启 Hermes 才生效**，报告里要写清这一点 |

## 五、报告要贴的证据

1. `hermes plugins list | grep cockpit` 改前 / 改后两行
2. `hermes -z` 起会话后，系统提示里相关段落的**原文**（证明真渲染进去了）
3. 四条验证（pytest / __check / __render_check / check_boundary）各自独立裸跑的退出码

现在开始。先做第 1 步（补 `register`），做完立刻验 enabled，**不要一口气写完全部再验**。
