"""
模型实例管理 — LlamaCpp / Ollama / OpenAI 单例。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from typing import Any, Dict, List, Optional

from env_loader import load_project_env

load_project_env()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
default_model_path = os.getenv(
    "DEFAULT_MODEL_PATH",
    os.path.join(_REPO_ROOT, "Models", "Phi-4-reasoning-plus-Q3_K_S.gguf"),
)

_llama_instance = None
_ollama_instance = None
_openai_instance = None


def get_dashscope_api_key() -> str:
    """优先 DASHSCOPE_API_KEY（官方 SDK 命名），兼容 OPENAI_API_KEY。"""
    return (
        os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()


def primary_model_name() -> str:
    return (
        os.getenv("OPENAI_MODEL")
        or os.getenv("DASHSCOPE_MODEL")
        or "qwen-plus"
    )


def is_primary_configured() -> bool:
    return bool(get_dashscope_api_key())


def local_model_path() -> str:
    return os.getenv("DEFAULT_MODEL_PATH", default_model_path)


def is_local_gbnf_ready() -> bool:
    path = local_model_path()
    try:
        return bool(path and os.path.isfile(path) and os.path.getsize(path) > 1024)
    except OSError:
        return False


def get_llama_instance(model_path: Optional[str] = None, **kwargs):
    global _llama_instance
    if _llama_instance is None:
        from langchain_community.llms import LlamaCpp

        path = model_path or default_model_path
        config = {
            "model_path": path,
            "temperature": float(os.getenv("LLAMA_TEMPERATURE", kwargs.get("temperature", 0.3))),
            "n_threads": int(os.getenv("LLAMA_N_THREADS", kwargs.get("n_threads", 8))),
            "n_gpu_layers": int(os.getenv("LLAMA_N_GPU_LAYERS", kwargs.get("n_gpu_layers", 80))),
            "n_ctx": int(os.getenv("LLAMA_N_CTX", kwargs.get("n_ctx", 8192))),
            "n_batch": int(os.getenv("LLAMA_N_BATCH", kwargs.get("n_batch", 256))),
            "max_tokens": int(os.getenv("LLAMA_MAX_TOKENS", kwargs.get("max_tokens", 768))),
            "verbose": kwargs.get("verbose", False),
            "stop": kwargs.get("stop", ["\nHuman:", "\nAI:", "\nhuman:", "\nai:"]),
        }
        _llama_instance = LlamaCpp(**config)
        logger.info("LlamaCpp initialized: %s", path)
    return _llama_instance


def get_ollama_instance(model_name: Optional[str] = None, **kwargs):
    global _ollama_instance
    if _ollama_instance is None:
        from langchain_community.llms import Ollama

        _ollama_instance = Ollama(
            model=model_name or os.getenv("OLLAMA_MODEL", "deepseek-r1:14b"),
            **kwargs,
        )
    return _ollama_instance


def get_openai_instance(**kwargs):
    global _openai_instance
    if _openai_instance is None:
        from openai import OpenAI
        import httpx

        api_key = get_dashscope_api_key()
        if not api_key:
            raise ValueError(
                "DASHSCOPE_API_KEY (or OPENAI_API_KEY) environment variable is not set"
            )
        total = float(os.getenv("OPENAI_TIMEOUT", "30"))
        # 独立 httpx，避免旧超时连接残留在池里把后续对话拖到 timeout
        http_client = httpx.Client(
            timeout=httpx.Timeout(total, connect=10.0, write=30.0, read=total),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
        _openai_instance = OpenAI(
            api_key=api_key,
            base_url=os.getenv(
                "OPENAI_BASE_URL",
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            ),
            max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "0")),
            http_client=http_client,
            **kwargs,
        )
        logger.info(
            "OpenAI-compatible client ready: model=%s base=%s timeout=%s",
            primary_model_name(),
            os.getenv("OPENAI_BASE_URL", ""),
            total,
        )
    return _openai_instance


def _message_content_to_text(content: Any) -> str:
    """兼容 DashScope：content 可能是 str，或 [{text: ...}] / [{type,text}]。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item and item["text"] is not None:
                    parts.append(str(item["text"]))
                elif "content" in item and item["content"] is not None:
                    parts.append(str(item["content"]))
            else:
                text = getattr(item, "text", None)
                if text is not None:
                    parts.append(str(text))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if content.get("text") is not None:
            return str(content["text"])
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def chat_completion(
    messages: List[Dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    **kwargs: Any,
) -> str:
    """OpenAI 兼容 chat.completions，返回 assistant 文本。"""
    client = get_openai_instance()
    params: Dict[str, Any] = {
        "model": model or primary_model_name(),
        "messages": messages,
    }
    if temperature is not None:
        params["temperature"] = temperature
    else:
        params["temperature"] = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    # 关掉部分 Qwen 思考链，缩短首字/总耗时
    extra_body = kwargs.pop("extra_body", None) or {}
    if os.getenv("QWEN_ENABLE_THINKING", "0").strip() not in ("1", "true", "yes"):
        extra_body.setdefault("enable_thinking", False)
    timeout = kwargs.pop("timeout", None)
    params.update(kwargs)
    create_kwargs = dict(params)
    if extra_body:
        create_kwargs["extra_body"] = extra_body
    if timeout is not None:
        create_kwargs["timeout"] = timeout
    try:
        resp = client.chat.completions.create(**create_kwargs)
    except Exception as e:
        # 超时/连接错误后丢掉单例，下次换新连接池
        err = str(e).lower()
        if "timeout" in err or "timed out" in err or "connection" in err:
            logger.warning("chat_completion failed (%s); resetting OpenAI client", e)
            destroy_singleton()
        raise
    choice = resp.choices[0].message
    content = getattr(choice, "content", None)
    return _message_content_to_text(content)


def destroy_singleton() -> None:
    global _llama_instance, _ollama_instance, _openai_instance
    if _llama_instance is not None:
        try:
            client = getattr(_llama_instance, "client", None)
            if client:
                client.close()
        except Exception:
            pass
        _llama_instance = None
    _ollama_instance = None
    if _openai_instance is not None:
        try:
            http = getattr(_openai_instance, "_client", None)
            # openai SDK: close underlying httpx
            close = getattr(_openai_instance, "close", None)
            if callable(close):
                close()
            elif http is not None and hasattr(http, "close"):
                http.close()
        except Exception:
            pass
        _openai_instance = None


atexit.register(destroy_singleton)
