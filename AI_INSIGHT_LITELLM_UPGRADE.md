# AI Insight LiteLLM 升级 Runbook

> 适用对象：维护 `Tab_General_Cluster / agent` 命名空间下 AI Insight LiteLLM 网关的同学。
> 本文档记录**如何再升级一次 litellm 镜像**，包含决策依据、标准流程、验证清单，以及已经踩过的坑。
>
> 最近一次升级：2026-11-25（v1.100.0 → v1.106.5），记录见文末「变更履历」。

---

## 0. 一句话结论（先读这段）

- 升级 **不是** 换一个 tag 那么简单：我们有一条内部的 responses 流式契约补丁 + 一个曾以 ConfigMap 挂载的兼容模块，必须跟着平移。
- 选版本时**不要一路追最新**：`1.108.0` 起 litellm 把 rust 扩展 `litellm.rust_bridge._native` 变成**硬依赖**（缺了就 `ImportError`），会大幅提高构建/运行复杂度。目前安全线停在 **1.106.x 的修复版**。
- 构建镜像**必须** `--platform linux/amd64`。本机是 arm64，直接 `docker commit` 出来的镜像在 TKE 上会 `exec format error`。
- 升级后**必须抓真实 SSE 事件流比对**，不能只看单测通过——上游有的“看似已修”其实语义不同（我们踩过 `summary: null` 的回退）。

---

## 1. 线上部署形态（排查/变更前必读）

| 项 | 值 |
|---|---|
| 集群 / namespace | `general-context` (Tab_General_Cluster) / `agent` |
| 工作负载 | `deployment/litellm`（replicas=1，`maxSurge=1`、`maxUnavailable=0` → 滚动更新期间旧 pod 持续服务） |
| 启动命令 | `litellm --config /app/proxy_server_config.yaml --port 4000` |
| **无数据库** | `STORE_MODEL_IN_DB=False`、`DISABLE_SCHEMA_UPDATE=True`，`/health/readiness` 返回 `{"status":"healthy","db":"Not connected"}` 属正常 |
| 镜像仓库 | `mirrors.tencent.com/tab/litellm`，`imagePullSecrets: tencent-mirrors` |
| 探针 | liveness `/health/liveliness`（initialDelay 180s）、readiness `/health/readiness`（initialDelay 10s） |
| 优雅终止 | `terminationGracePeriodSeconds: 1800`（旧 pod 会 `Terminating` 很久，属正常，流量已摘） |

### 1.1 两个挂载文件（历史形态）

| 挂载路径 | 来源 | 内容 |
|---|---|---|
| `/app/ai_insight_compat.py` | ConfigMap `litellm-ai-insight-compat` | 137 行 CustomLogger：空 assistant 占位项清理、reasoning.summary 剥离、`deepseek-v4.1-flash-mix` 组 Venus/官网路由 |
| `/app/proxy_server_config.yaml` | Secret `litellm-config` | 37 个 model deployment（全指向内网 `new-api.woa.com`，key 用 `os.environ/` 引用）+ `general_settings.master_key` + `litellm_settings.callbacks` + `router_settings` |

### 1.2 环境变量（全部来自 Secret `litellm-secrets`）

```
LITELLM_MASTER_KEY        # 网关主密钥
NEWAPI_AI_INSIGHT_CHAT_KEY
NEWAPI_API_KEY_STANDARD
NEWAPI_API_KEY_CUSTOM
```
> 没有 `LITELLM_LICENSE` → **开源部署，未激活 enterprise**。很多安全公告（含 GHSA-w4c7-34wj-hqm8）明确只影响 enterprise 部署，这类工单可先按此申诉。

### 1.3 现网自查命令

```bash
kubectl --context general-context -n agent get deploy litellm \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl --context general-context -n agent get pods -l app.kubernetes.io/name=litellm -o wide
kubectl --context general-context -n agent get deploy litellm -o yaml | grep -A3 volumeMounts
```

---

## 2. 我们的内部定制清单（升级时要一起平移）

源码位置：fork `git@github.com:liracle/litellm.git`，分支 `feat/ai-insight-internal-1.106.5`（基线 `v1.106.5`）。

| # | 改动 | 文件 | 说明 |
|---|---|---|---|
| 1 | `response.reasoning_summary_text.delta` 必须带 `summary_index` | `litellm/responses/litellm_completion_transformation/streaming_iterator.py` | pydantic `exclude_unset` 序列化会把默认值 0 丢掉，严格消费方会丢帧 |
| 2 | tool-call 后才出现 `reasoning_content` 时补发 `output_item.added` | 同上 | 一次性 `sent_output_item_added_event` 已被 tool call 消耗，否则 reasoning 帧引用未声明 item |
| 3 | reasoning `output_item.added` 的 `summary` 必须是**非空数组** | 同上 | 上游至今仍发 `null`，严格消费方整帧丢弃 |
| 4 | `ai_insight_compat` 内联为包内模块 | `litellm/integrations/ai_insight_compat.py` | 原先靠 ConfigMap 挂载；1.106.5 runtime 镜像**不再 COPY 源码到 /app/litellm**，只能进包 |
| 5 | 契约测试 | `tests/unit/responses/litellm_completion_transformation/test_streaming_event_order.py` | 12 个断言，是升级验收门禁 |

> 1.100.0 时代内部 patch 里的其余修复（`content_part.done` 只用 `output_text`、未声明 message item 不发 done、role-only 首块不决定 item 类型、reasoning 生命周期先关再开 message、annotation 缓冲）**上游 1.103~1.106 已自行修好**，升级时可直接丢弃，不要硬搬。

---

## 3. 版本选择

| 目标版本 | 结论 |
|---|---|
| `1.103.10` / `1.104.8` / `1.105.6` | 安全线，但上游这块重构较早，需要平移的内部逻辑更多（实测我们的测试 4/11 失败） |
| **`1.106.5`**（推荐） | 实测只剩 2 处需补，全套流式测试无新增失败 |
| `1.107.0` | ⚠️ **不要用**，PyPI 投毒事件版本 |
| `1.108.0+` / `1.111.x` | ⚠️ rust 扩展成硬依赖（实测 `ImportError: cannot import name '_native'`），构建链复杂化，暂不追 |

安全修复版本对照（2026-10 GHSA-w4c7-34wj-hqm8）：
`1.103.10 / 1.104.8 / 1.105.6 / 1.106.5`，四条线分别修复，无需跨线。

---

## 4. 标准升级流程

### Step 0 — 评估阶段（本地，不动线上）

```bash
cd <repo>
git fetch --depth=1 origin tag vX.Y.Z
git worktree add --detach /tmp/up-eval vX.Y.Z
# 把我们 12 个契约测试直接拷到目标版本跑（先不移植实现，看上游已修好多少）
cp tests/unit/responses/litellm_completion_transformation/test_streaming_event_order.py \
   /tmp/up-eval/tests/unit/responses/litellm_completion_transformation/
cd /tmp/up-eval && PYTHONPATH=. python -m pytest <上面的测试> -q
```
- 通过项 → 对应内部 patch 可以丢弃。
- 失败项 → 就是必须平移的量。**同时逐条读上游新实现**，确认语义是否真的等价（不要只看测试名）。

顺带体检：
```bash
git diff --stat <当前基线> vX.Y.Z -- litellm/responses | tail -20      # 桥接层改动面
git diff --stat <当前基线> vX.Y.Z -- litellm/proxy/schema.prisma         # 有 DB 才需要迁移（我们是 false）
git show vX.Y.Z:pyproject.toml | sed -n '/^\[build-system\]/,/^\[tool.maturin/p'
```

### Step 1 — 建分支、平移 patch、跑测试

```bash
git worktree add --detach /tmp/up-build vX.Y.Z
cd /tmp/up-build && git checkout -b feat/ai-insight-internal-<ver>
# 移植 3 处 streaming 改动 + litellm/integrations/ai_insight_compat.py
# 测试目录注意：1.104 起从 tests/test_litellm/... 变为 tests/unit/...
PYTHONPATH=. python -m pytest tests/unit/responses/litellm_completion_transformation/ \
  -q --ignore=...test_session_handler*.py
ruff check <改动文件> && ruff format --check <改动文件>
git push fork feat/ai-insight-internal-<ver>
```

### Step 2 — 构建镜像（**必须 amd64**）

```bash
cd <build-context>          # 内含 Dockerfile + streaming_iterator.py + ai_insight_compat.py
docker buildx build \
  --platform linux/amd64 \
  --provenance=false --sbom=false \
  -f Dockerfile \
  -t mirrors.tencent.com/tab/litellm:v<ver>-aiinsight-<YYYYMMDD>-p<N> \
  --push .
```

Dockerfile 核心（不要用 `docker commit`，见踩坑 #1）：

```dockerfile
FROM ghcr.io/berriai/litellm:<ver>
ARG SITE=/app/.venv/lib/python3.13/site-packages/litellm
COPY streaming_iterator.py ${SITE}/responses/litellm_completion_transformation/streaming_iterator.py
COPY ai_insight_compat.py  ${SITE}/integrations/ai_insight_compat.py
RUN chown 0:0 ${SITE}/integrations/ai_insight_compat.py \
 && rm -rf ${SITE}/responses/litellm_completion_transformation/__pycache__ ${SITE}/integrations/__pycache__ \
 && /app/.venv/bin/python -c "import litellm.integrations.ai_insight_compat as m; print(type(m.proxy_handler).__name__)"
```

构建后自检（拿 digest 备用）：
```bash
docker buildx imagetools inspect mirrors.tencent.com/tab/litellm:v<ver>-aiinsight-<date>-p<N>
docker run --rm --platform linux/amd64 --entrypoint /app/.venv/bin/python <image> -c \
  "import platform,hashlib;print(platform.machine());
   print(hashlib.sha256(open('/app/.venv/lib/python3.13/site-packages/litellm/responses/litellm_completion_transformation/streaming_iterator.py','rb').read()).hexdigest())"
# 期望 x86_64 + 与本地产物一致的 sha256
```

### Step 3 — 部署（按“两步走”，务必不要跳步）

**第 1 步：只换镜像，config / 挂载一律不动**

```bash
kubectl --context general-context -n agent set image deploy/litellm \
  litellm=mirrors.tencent.com/tab/litellm:v<ver>-aiinsight-<date>-p<N>@sha256:<digest>
kubectl --context general-context -n agent rollout status deploy/litellm --timeout=300s
```
此时 `callbacks: ai_insight_compat.proxy_handler` 仍指向 ConfigMap 挂载文件（`get_instance_fn` 优先从 config 同目录加载 `.py`），零配置改动。

**第 2 步（观察稳定后再做）：切包内模块 + 摘挂载**

顺序不可颠倒，否则找不到模块：

1. Secret `litellm-config` 里把
   `callbacks: [ai_insight_compat.proxy_handler]` → `[litellm.integrations.ai_insight_compat.proxy_handler]`，先 apply；
2. 再摘 volumeMount / volume：
   ```bash
   kubectl --context general-context -n agent patch deploy litellm --type=json -p='[
     {"op":"remove","path":"/spec/template/spec/containers/0/volumeMounts/0"},
     {"op":"remove","path":"/spec/template/spec/volumes/0"}]'
   ```
3. 回滚窗口过去后再删 ConfigMap `litellm-ai-insight-compat`，并清理 deployment annotation（`ai-insight/compat-code`、`reasoning-fix/config` 等）。

### Step 4 — 验证（缺一不可）

```bash
# 4.1 基础健康（在 pod 内执行，容器里没有 curl）
kubectl --context general-context -n agent exec deploy/litellm -- \
  /app/.venv/bin/python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:4000/health/liveliness',timeout=8).read())"

# 4.2 文件指纹对得上
kubectl exec ... /app/.venv/bin/python -c "import hashlib;p='<site-packages>/litellm/responses/litellm_completion_transformation/streaming_iterator.py';print(hashlib.sha256(open(p,'rb').read()).hexdigest())"

# 4.3 抓真实 SSE，比对事件序列 + 关键字段（见附录 A 脚本）
```

必须覆盖 5 类流：

| 场景 | 期望 |
|---|---|
| 纯文本 | `created → in_progress → added(message) → content_part.added → output_text.delta… → completed/incomplete` |
| reasoning + 文本 | reasoning 项 `added→…→done` **完全关闭**后才出现 `added(message)`，两个 item 生命周期不重叠 |
| 纯 tool-call | 只有 `reasoning` / `function_call` 两个 item，**不出现 message 项的 done**（ghost done） |
| tool-call 后带 reasoning | function_call 之后出现的 reasoning 仍要有 `output_item.added[reasoning]` |
| 空 assistant 占位项 input | 请求成功（compat 的 `pre_call_hook` 清理掉空 assistant） |

关键字段硬检查：
- `response.reasoning_summary_text.delta` 的 payload 里**真实存在** `summary_index`（不是 model 默认值）；
- reasoning `output_item.added` 的 `summary` 是 `[{"type":"summary_text","text":""}]`，不是 `null`。

### Step 5 — 观察与回滚

- 观察 24h：客户端 responses 解析错误率、首包时延、`deepseek-v4.1-flash-mix` 路由分布。
- 回滚：
  - 第 1 步之后 → `set image` 回旧 tag 即可；
  - 第 2 步之后 → config 那一行改回 `ai_insight_compat.proxy_handler` + 恢复 volumeMount。
  - 无 DB 迁移，回滚是纯替换。

---

## 5. 踩坑清单（本次真实踩过，按重要性排序）

### 坑 1：`docker commit` 出来的镜像是 arm64，集群 amd64 → CrashLoopBackOff
现象：`kubectl logs` 只有 `exec /bin/sh: exec format error`。
原因：本机 Apple Silicon(arm64)，`docker run/commit` 得到 arm64 镜像。
对策：**只用 `docker buildx build --platform linux/amd64`**；构建后必查 `docker buildx imagetools inspect` 的 `platform.architecture`。
幸运点：`maxUnavailable=0`，旧 pod 全程服务，业务零中断——但别指望每次都有这个余量。

### 坑 2：buildx 默认带 provenance attestation，manifest 变成 manifest list
现象：`docker manifest inspect` 里多出一个 attestation manifest。
对策：构建时加 `--provenance=false --sbom=false`，得到单一 `application/vnd.oci.image.manifest.v1+json`。

### 坑 3：新版 runtime 镜像不再 COPY 源码，改 `/app/litellm/*.py` 无效
1.100.x 镜像里 `/app/litellm` 有源码目录，容易被误改；1.106.5 只保留 `prisma_migration.py`，真正 import 的是
`/app/.venv/lib/python3.13/site-packages/litellm/...`。
判断当前实际生效路径：
```bash
kubectl exec ... /app/.venv/bin/python -c "import litellm;print(litellm.__file__)"
```

### 坑 4：config 与镜像必须“原子”切换（或按两步走）
- 只改 config 指向包路径、镜像没换 → 旧镜像没这个模块 → 起不来；
- 只摘挂载、config 没改 → `get_instance_fn` 找不到 `ai_insight_compat` → 起不来。
对策：按第 3 节的两步走，第 1 步完全不碰 config。

### 坑 5：上游“看着已修”≠ 语义一致
本次升级中，1.106.5 上游已把 `content_part.done`、ghost done、role-only 延迟这几项修好，**但** reasoning item 的 `summary` 仍发 `null`（我们旧 patch 发的是非空数组）。
对策：升级后必须抓真实 SSE 逐字段比对，别只依赖测试。

### 坑 6：commit 前必须清理运行期污染
跑一次服务就会产生：大量新增 `__pycache__/*.pyc`、`/tmp/*`、以及 Docker Desktop/OrbStack 注入的 `/etc/ssl/certs/orbstack-root.crt`（连带 `ca-certificates.crt` 被改）。
```bash
docker diff <container> > /tmp/dd.txt
# 只应留下预期的 2 个改动（1 个 A、1 个 C）；其余按 dd.txt 逐条删除
```
（用 Dockerfile 方式构建天然规避这条。）

### 坑 7：跳板机是精简系统
`sed` / `sleep` / `sort` / `curl` / `python3` 都没有。kubectl 场景请用 `awk` / `grep -A` / `kubectl wait` / `-o jsonpath`，容器内自检用 `/app/.venv/bin/python` 而不是 curl。

### 坑 8：`pkill -f 'port 4001'` 会杀掉自己
在容器内用 `sh -c "pkill -f 'port 4001'"` 时，`pkill -f` 会匹配到**这条 sh 命令本身**，导致后续命令不执行。用 PID 精确 kill。

### 坑 9：测试目录结构变过
`1.103.x`：`tests/test_litellm/responses/...`；`1.104+`：`tests/unit/responses/...`。搬测试时注意路径，以及 `tests/unit/conftest.py` 需要 `pytest-socket`、`respx` 等依赖。

### 坑 10：1.107.0 是投毒版本
2026-10-21 PyPI 投毒事件。选版本时跳过它，并从可信 tag 拉取（`ghcr.io/berriai/litellm:vX.Y.Z`，留意 digest）。

---

## 6. 附录

### 附录 A：真实 SSE 比对脚本（在 pod 内跑）

```python
import json, os, urllib.request, time
key = os.environ["LITELLM_MASTER_KEY"]

def call(label, body):
    req = urllib.request.Request("http://127.0.0.1:4000/v1/responses",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    seq, summary_field, delta_idx, texts, t0 = [], None, None, [], time.time()
    with urllib.request.urlopen(req, timeout=90) as f:
        for raw in f:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            t = d.get("type", "")
            if not t:
                continue
            short = t.replace("response.", "")
            if "output_item" in short:
                seq.append(f"{short}[{(d.get('item') or {}).get('type')}]")
                if short.endswith("added") and (d.get("item") or {}).get("type") == "reasoning":
                    summary_field = d["item"].get("summary")
            else:
                seq.append(short)
            if t.endswith("reasoning_summary_text.delta") and delta_idx is None:
                delta_idx = ("summary_index" in d, d.get("summary_index"))
            if t.endswith("output_text.delta"):
                texts.append(d.get("delta", ""))
    print(f"[{label}] {time.time()-t0:.1f}s")
    print("  seq:", seq)
    print("  reasoning.summary =", json.dumps(summary_field, ensure_ascii=False))
    print("  delta.summary_index =", delta_idx)
    print("  text:", repr("".join(texts)[:40]))

tools = [{"type": "function", "name": "get_weather", "description": "Get weather",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                         "required": ["city"]}}]

call("plain", {"model": "deepseek-v4-flash", "input": "say hi", "stream": True, "max_output_tokens": 32})
call("reasoning", {"model": "deepseek-v4-flash", "input": "what is 2+2?", "stream": True,
                   "reasoning": {"effort": "low"}, "max_output_tokens": 48})
call("tool-only", {"model": "deepseek-v4-flash", "input": "Call get_weather for Beijing.",
                   "stream": True, "tools": tools,
                   "tool_choice": {"type": "function", "name": "get_weather"}, "max_output_tokens": 64})
call("empty-assistant", {"model": "deepseek-v4-flash", "stream": True, "max_output_tokens": 24,
                         "input": [{"type": "message", "role": "user", "content": "hi"},
                                   {"type": "message", "role": "assistant", "content": ""},
                                   {"type": "message", "role": "user", "content": "again"}]})
```

### 附录 B：pod 内健康自检（容器无 curl）

```bash
kubectl --context general-context -n agent exec deploy/litellm -- /app/.venv/bin/python -c "
import urllib.request as u
print('liveliness:', u.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=8).read())
print('readiness :', u.urlopen('http://127.0.0.1:4000/health/readiness', timeout=8).read())
"
```

### 附录 C：callbacks 是否真的加载了（临时开 DEBUG 看日志）

```bash
LITELLM_LOG=DEBUG litellm --config /app/proxy_server_config.yaml --port 4000 2>&1 | grep "Initialized Callbacks"
# 期望出现 <litellm.integrations.ai_insight_compat.ResponsesEmptyMessageCompatibility object ...>
```

---

## 7. 变更履历

### 2026-11-25：v1.100.0-aiinsight-20260828-p6 → v1.106.5-aiinsight-20261125-p3

- 起因：安全工单 GHSA-w4c7-34wj-hqm8（CVSS 10.0，仅 enterprise 受影响；我方无 `LITELLM_LICENSE`，属开源部署）。
- 基线：`v1.106.5`（含修复 backport PR #48605）。
- 内部改动：3 处 streaming 契约修复 + `ai_insight_compat` 内联为包内模块；其余旧 patch 由上游吸收后丢弃。
- 分支：fork `feat/ai-insight-internal-1.106.5`。
- 镜像：`mirrors.tencent.com/tab/litellm:v1.106.5-aiinsight-20261125-p3`
  `@sha256:54ef599a2b8bc280818703c0add2339a0e6c7695071c817724e32c4c2fe26e48`
- 过程事故：p1 用 `docker commit` 产出 arm64 镜像 → `exec format error`，已回滚；p2 成功但发现 `summary: null` 回退；p3 补齐后上线。
- 验证：12 个契约测试全绿；线上 5 类流场景真实 SSE 比对通过；`RESTARTS=0`。
- 遗留：ConfigMap / Secret 挂载**尚未**移除（仍是“第 1 步”形态），待观察稳定后执行第 2 步。
