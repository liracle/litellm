"""AI Insight proxy compatibility hooks.

Historically shipped as a ``/app/ai_insight_compat.py`` file mounted from a
ConfigMap (``litellm-ai-insight-compat``) and referenced from the proxy config
as ``litellm_settings.callbacks: [ai_insight_compat.proxy_handler]``. It now
lives in-tree so it ships inside the image with the rest of the package and is
covered by the normal review/test flow instead of being an out-of-band patch.

Responsibilities
- drop contentless assistant placeholder items from a Responses ``input`` list
  (the chat-completions bridge would otherwise reject/duplicate them)
- keep the reasoning summary out of the chat ``reasoning`` object on providers
  whose bridge does not reassemble it (kept as a defensive no-op: upstream >=
  1.106 already splits ``reasoning`` into ``reasoning_effort`` + summary alias)
- route the ``deepseek-v4.1-flash-mix`` group to Venus first, falling back to
  the DeepSeek official line only when Venus is measurably degraded
"""

import time
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any, Final

from litellm.integrations.custom_logger import CustomLogger

MIX_GROUP: Final = "deepseek-v4.1-flash-mix"
VENUS_IDS: Final = ("mix-venus-self", "mix-venus-official")
DS_ID: Final = "mix-ds-official"
SAMPLE_WINDOW_SECONDS: Final = 600
MIN_SAMPLES: Final = 3
FAILURE_PENALTY_SECONDS: Final = 30.0
# The official line bills per token and is naturally faster on first token, so
# only switch to it when every Venus line's median exceeds this threshold.
VENUS_DEGRADED_SECONDS: Final = 3.0

SELF_TOOL_MODELS: Final = frozenset(
    {
        "deepseek-v4-flash-zhuzhan",
        "deepseek-v4.1-flash-zhuzhan",
        "deepseek-v4-pro-zhuzhan",
        "deepseek-v4.1-flash",
        MIX_GROUP,
        "claude-opus-5-5",
        "glm-5.3-flash",
    }
)
REASONING_AWARE_MODELS: Final = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-zhuzhan",
        "deepseek-v4.1-flash-zhuzhan",
        "deepseek-v4-pro-zhuzhan",
        "kimi-k3",
        "kimi-k3-zhuzhan",
        "kimi-k2.7-zhuzhan",
        "glm-5.3-zhuzhan",
        "glm-5.2",
        "claude-opus-4-6",
        "deepseek-v4.1-flash",
        MIX_GROUP,
        "glm-5.3-flash",
        "claude-opus-5-5",
    }
)


def empty_assistant(item: object) -> bool:
    """True for an assistant message that carries neither content nor a tool call."""
    if not isinstance(item, dict) or item.get("type", "message") != "message":
        return False
    if item.get("role") != "assistant" or item.get("tool_calls") or item.get("reasoning_content"):
        return False
    content: Final = item.get("content")
    if content == "" or (content is None and "content" in item):
        return True
    return isinstance(content, list) and all(
        isinstance(part, dict)
        and part.get("type") in ("input_text", "output_text", "text")
        and part.get("text") in ("", None)
        for part in content
    )


def deployment_id(deployment: Mapping[str, Any]) -> str | None:
    return (deployment.get("model_info") or {}).get("id")


def has_reasoning_text(item: Mapping[str, Any]) -> bool:
    parts: Final = [*(item.get("content") or []), *(item.get("summary") or [])]
    return any(isinstance(part, dict) and str(part.get("text") or "").strip() for part in parts)


def has_unreasoned_turn(items: Sequence[object]) -> bool:
    """The official line's thinking mode requires every assistant turn to replay reasoning.

    Venus does not validate this, so when a turn is missing reasoning the request
    must not be routed to the official line.
    """
    reasoned: bool = False
    output: bool = False
    for item in items:
        if not isinstance(item, dict):
            continue
        kind: Final = item.get("type", "message")
        if kind == "reasoning":
            reasoned = reasoned or has_reasoning_text(item)
        elif kind == "function_call" or (kind == "message" and item.get("role") == "assistant"):
            output = True
            reasoned = reasoned or bool(str(item.get("reasoning_content") or "").strip())
        else:
            if output and not reasoned:
                return True
            reasoned = output = False
    return output and not reasoned


class ResponsesEmptyMessageCompatibility(CustomLogger):
    """Proxy hooks: empty-assistant cleanup + mix-group routing."""

    def __init__(self) -> None:
        super().__init__()
        self.samples: dict[str, deque[tuple[float, float]]] = {}

    def _record(self, kwargs: Mapping[str, Any], seconds: float) -> None:
        info: Final = (kwargs.get("litellm_params") or {}).get("model_info") or {}
        key: Final = info.get("id")
        if key in VENUS_IDS or key == DS_ID:
            self.samples.setdefault(key, deque(maxlen=50)).append((time.time(), seconds))

    def _score(self, key: str) -> float | None:
        now: Final = time.time()
        values: Final = sorted(v for t, v in self.samples.get(key, ()) if now - t < SAMPLE_WINDOW_SECONDS)
        return values[len(values) // 2] if len(values) >= MIN_SAMPLES else None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        first: Final = kwargs.get("completion_start_time") or end_time
        try:
            self._record(kwargs, (first - start_time).total_seconds())
        except TypeError:
            pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._record(kwargs, FAILURE_PENALTY_SECONDS)

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None,
        request_kwargs: dict | None = None,
        parent_otel_span=None,
    ) -> list:
        if model != MIX_GROUP:
            return healthy_deployments
        venus: Final = [d for d in healthy_deployments if deployment_id(d) in VENUS_IDS]
        official: Final = [d for d in healthy_deployments if deployment_id(d) == DS_ID]
        history: Final = (request_kwargs or {}).get("input")
        if not isinstance(history, list):
            history = messages if isinstance(messages, list) else []
        if venus and has_unreasoned_turn(history):
            return venus
        if not venus or not official:
            return healthy_deployments
        venus_scores: Final = [self._score(deployment_id(d) or "") for d in venus]
        if None in venus_scores or min(s for s in venus_scores if s is not None) <= VENUS_DEGRADED_SECONDS:
            return venus
        official_score: Final = self._score(DS_ID)
        if official_score is not None and official_score >= min(s for s in venus_scores if s is not None):
            return venus
        return official

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type) -> dict | None:
        if data.get("model") not in REASONING_AWARE_MODELS:
            return None
        if data.get("model") in SELF_TOOL_MODELS:
            reasoning: Final = data.get("reasoning")
            if isinstance(reasoning, dict) and "summary" in reasoning:
                # Chat only accepts an effort string; LiteLLM would otherwise
                # forward the summary option as an object. Upstream >= 1.106
                # already splits this, kept as a defensive no-op.
                reasoning = {key: value for key, value in reasoning.items() if key != "summary"}
                if reasoning:
                    data["reasoning"] = reasoning
                else:
                    data.pop("reasoning", None)
        items: Final = data.get("input")
        if isinstance(items, list):
            data["input"] = [item for item in items if not empty_assistant(item)]
        return data


proxy_handler: Final = ResponsesEmptyMessageCompatibility()
