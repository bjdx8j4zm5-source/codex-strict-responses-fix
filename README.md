# codex-strict-responses-fix

Workaround for the Codex Desktop / `codex` CLI bug where injected automation
triggers are sent as a standalone `function_call_output` **without `call_id`**,
which strict third-party Responses providers (DeepSeek, Azure, Kimi, ...) reject with:

```
Failed to deserialize the JSON body into the target type: input: missing field `call_id`
```

Upstream reports: [openai/codex#41690](https://github.com/openai/codex/issues/41690),
[#44723](https://github.com/openai/codex/issues/44723),
[#44519](https://github.com/openai/codex/issues/44519),
[#42088](https://github.com/openai/codex/issues/42088),
[#42067](https://github.com/openai/codex/issues/42067),
[#44779](https://github.com/openai/codex/issues/44779).

## The problem, in one paragraph

Since commit `e56e4922eb` ("Support standalone tool outputs in `turn/start`", #41002,
first shipped in `rust-v0.151.0-alpha.4`), the app-server delivers automation
triggers (heartbeat / cron), thread-delegation input and `tool_search` follow-ups as
a standalone `function_call_output` item with `id`/`name`/`namespace` but **no
`call_id`** and no matching `function_call`. OpenAI's endpoint tolerates that form;
strict third-party implementations do not and reject the whole request with HTTP 400.
Because the bad item is persisted in the session history (and in
`$CODEX_HOME/thread_history_1.sqlite`), the thread stays broken: every later message
fails the same way.

Before that commit the same payload was delivered as an ordinary **user message**,
which every provider accepts.

## Fix A (recommended): local shim that restores the old delivery format

`responses-shim.py` is a tiny passthrough proxy. It sits between Codex and your
provider and rewrites every `function_call_output` that lacks a `call_id` into the
equivalent user message before forwarding. Everything else is proxied byte-for-byte
(streaming included), so the automation still receives its `<heartbeat>` payload —
it just arrives the way it used to before `e56e4922eb`.

```toml
# ~/.codex/config.toml
[model_providers.deepseek]
name = "deepseek"
base_url = "http://127.0.0.1:19100/"   # was https://api.deepseek.com/
wire_api = "responses"
experimental_bearer_token = "..."
```

```bash
python responses-shim.py                # listens on 127.0.0.1:19100
python responses-shim.py --port 19101 --upstream api.deepseek.com
```

Keep it alive with a scheduled task / systemd unit / launchd agent, and restart
Codex once so it picks up the new `base_url`.

### Verified

| test | result |
| --- | --- |
| request with an orphan `function_call_output`, sent directly to `api.deepseek.com` | `400 missing field 'call_id'` |
| same request through the shim | `200`, 27 SSE events, clean `response.completed` |
| real `codex exec` with the shim as `base_url` | `200`, model reply received |
| `GET /models`, non-streaming `POST /responses` | `200` |

## Fix B: repair a thread that is already broken

`repair_missing_call_id.py` scans every rollout JSONL plus
`thread_history_1.sqlite`, removes the orphan outputs and realigns
`thread_history_projection_state` so the history rebuild stays consistent.

```bash
python repair_missing_call_id.py           # dry run
python repair_missing_call_id.py --apply   # backup + repair, then restart Codex
```

Backups are written next to the script before anything is modified.

## Caveats

- The shim is on the request path: if it is down, Codex cannot reach the model.
  Keep the old `base_url` around so you can switch back instantly.
- It only rewrites items that are *invalid anyway*; valid call/output pairs, tool
  calls and streams are passed through untouched.
- This is a client-side workaround, not an upstream fix. The proper fix belongs in
  the app-server: either attach a real `call_id` + matching `function_call`, or
  deliver injected triggers as user messages again.

---

## 中文说明

**问题**:自 commit `e56e4922eb`(随 `rust-v0.151.0-alpha.4` 发布)起,Codex 把自动化
(heartbeat / cron)触发内容、线程委派、`tool_search` 后续请求,作为**不带 `call_id`
的独立 `function_call_output`** 注入请求。OpenAI 官方端点能容忍这种形式;DeepSeek、
Azure、Kimi 等严格实现会直接返回:

```
Failed to deserialize the JSON body into the target type: input: missing field `call_id`
```

因为这条坏记录会写进会话历史(以及 `$CODEX_HOME/thread_history_1.sqlite`),整个对话会
**永久失效**——之后发什么都报同样的错。而只补 `call_id` 也没用:它没有配对的
`function_call`。

**方案 A(推荐)**:`responses-shim.py` 是一个本地透传代理,把这种缺 `call_id` 的
`function_call_output` 在转发前改写成普通 user 消息(这正是该 commit 之前 Codex 的
投递方式),其余请求原样透传(含流式)。这样自动化能真正运行,也不会再写出坏记录。

```toml
[model_providers.deepseek]
base_url = "http://127.0.0.1:19100/"   # 本地代理 -> api.deepseek.com
wire_api = "responses"
```

**方案 B**:`repair_missing_call_id.py` 用于修复**已经坏掉**的会话——扫描全部 rollout
与 `thread_history_1.sqlite`,删除孤儿输出并校正 `thread_history_projection_state`
的偏移(默认 dry-run,`--apply` 才动手,改前自动备份)。

**已验证**:同一条请求直连 DeepSeek → 400;走代理 → 200、27 个 SSE 事件、正常
`response.completed`;真实 `codex exec` 走代理 → 200 并正常回复;`GET /models` 与
非流式请求均 200。

**注意**:这是客户端绕过,不是上游修复。官方正确修法是 app-server 里补上
`call_id` + 配对 `function_call`,或改回 user 消息投递。
