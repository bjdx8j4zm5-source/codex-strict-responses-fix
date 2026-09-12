**Workaround (verified): deliver the trigger the way it was delivered before `e56e4922eb`**

Same setup as reported here (Windows, Codex Desktop, `wire_api = "responses"`, DeepSeek).
We hit both halves of the failure: the automation never completed a single run, and the
target thread became permanently unusable — every later user message returned the same
HTTP 400.

As @argszero traced above, the trigger **used to arrive as a user message**; since
`e56e4922eb` it arrives as a standalone `function_call_output` with no `call_id`. And as
@njwangcn pointed out, populating `call_id` alone is not a fix either — there is no
matching `function_call` to pair it with.

So rather than trying to make the item valid, add a local passthrough proxy that rewrites
it back into the old form before it reaches the provider:

```python
# every function_call_output without call_id -> plain user message with the same text
{"type": "function_call_output", "id": "fco_…", "name": "automation_update",
 "namespace": "codex_app", "output": "<heartbeat>…</heartbeat>"}
# becomes
{"type": "message", "role": "user",
 "content": [{"type": "input_text", "text": "<heartbeat>…</heartbeat>"}]}
```

Everything else — tool calls, reasoning items, streaming — is forwarded unchanged.

```toml
[model_providers.deepseek]
base_url = "http://127.0.0.1:19100/"   # local shim -> api.deepseek.com
wire_api = "responses"
```

Verified end to end:

| test | result |
| --- | --- |
| request containing an orphan `function_call_output`, sent directly to `api.deepseek.com` | 400 `missing field 'call_id'` |
| identical request through the shim | 200, 27 SSE events, clean `response.completed` |
| real `codex exec` run with the shim as `base_url` | 200, normal model reply |
| `GET /models` and a non-streaming `POST /responses` | 200 |

Outcome: the automation actually runs instead of dying on its first trigger, and no
malformed item is ever persisted — so the "permanently bricked thread" failure mode
disappears.

For threads that are **already** bricked, the same repo ships a repair script that removes
the orphan outputs from both the rollout JSONL and `thread_history_1.sqlite`
(`thread_items`), realigns `thread_history_projection_state`, and backs everything up
before touching it (dry-run by default). Restart Codex afterwards.

Code + write-up: https://github.com/bjdx8j4zm5-source/codex-strict-responses-fix

*Caveat: this is a client-side workaround, not an app-server fix. The proper fix is either
attaching a real `call_id` with a matching `function_call`, or going back to
user-message delivery for injected triggers.*

---

**中文版**

同样的环境(Windows + Codex 桌面版 + `wire_api = "responses"` + DeepSeek)。我们遇到了这个
问题的两半:自动化一次都没成功运行过,而且目标会话被永久搞坏——之后每条消息都返回同样的
HTTP 400。

正如 @argszero 上面定位的:触发内容**以前是以 user 消息下发的**;`e56e4922eb` 之后改成
独立的 `function_call_output`,且不带 `call_id`。而 @njwangcn 说得对:只补 `call_id` 也
不行——它没有配对的 `function_call`。

所以不去"修正"这条记录,而是加一个本地透传代理,把它改回旧格式再发给服务商:

```python
# 每个缺 call_id 的 function_call_output -> 内容相同的普通 user 消息
{"type": "function_call_output", "id": "fco_…", "name": "automation_update",
 "namespace": "codex_app", "output": "<heartbeat>…</heartbeat>"}
# 变成
{"type": "message", "role": "user",
 "content": [{"type": "input_text", "text": "<heartbeat>…</heartbeat>"}]}
```

其余内容(工具调用、reasoning、流式响应)原样透传。

```toml
[model_providers.deepseek]
base_url = "http://127.0.0.1:19100/"   # 本地代理 -> api.deepseek.com
wire_api = "responses"
```

端到端验证:

| 测试 | 结果 |
| --- | --- |
| 含孤儿 `function_call_output` 的请求,直连 `api.deepseek.com` | 400 `missing field 'call_id'` |
| 同一请求走代理 | 200,27 个 SSE 事件,正常 `response.completed` |
| 真实 `codex exec` 以代理为 `base_url` | 200,模型正常回复 |
| `GET /models` 与非流式 `POST /responses` | 200 |

结果:自动化能真正跑起来(而不是在第一次触发就死掉),并且不会再写入坏记录,因此
"会话永久损坏"这个失败模式消失。

对于**已经坏掉**的会话,同一个仓库里有修复脚本:从 rollout JSONL 和
`thread_history_1.sqlite`(`thread_items`)里删除孤儿输出、校正
`thread_history_projection_state` 偏移,改动前自动备份(默认 dry-run),修完重启
Codex 即可。

代码与说明:https://github.com/bjdx8j4zm5-source/codex-strict-responses-fix

*说明:这是客户端绕过方案,不是 app-server 的修复。官方正确修法是:补上真实的 `call_id`
并配对 `function_call`,或让注入的触发内容改回 user 消息投递。*
