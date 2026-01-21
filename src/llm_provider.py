from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple


def _env(name: str) -> str:
    v = os.getenv(name)
    return str(v).strip() if v is not None else ""


def _provider() -> str:
    return (_env("LLM_PROVIDER") or "openai").lower()


def _as_float(s: str, default: float) -> float:
    try:
        return float(s)
    except Exception:
        return float(default)


def _as_int(s: str, default: int) -> int:
    try:
        return int(s)
    except Exception:
        return int(default)


@dataclass(frozen=True)
class ProviderIds:
    provider: str
    chat_id: str
    embedding_id: str


def build_chat_llm(*, model: Optional[str] = None, **kwargs: Any):
    """
    LangChainのチャットモデルを構築する（OpenAI/Azure/Gemini/Claude）。

    - OpenAI:
      - env: OPENAI_API_KEY, OPENAI_MODEL(任意), MODEL(任意)
    - Azure OpenAI:
      - env: LLM_PROVIDER=azure, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_VERSION
      - env: AZURE_OPENAI_DEPLOYMENT_NAME(任意; 未指定時は model へフォールバック)
    - Gemini (AI Studio):
      - env: LLM_PROVIDER=gemini, GOOGLE_API_KEY
      - env: GEMINI_MODEL(任意)
    - Claude (Anthropic):
      - env: LLM_PROVIDER=claude, ANTHROPIC_API_KEY
      - env: CLAUDE_MODEL(任意; デフォルト claude-haiku-4-5-20250514)
    """
    provider = _provider()

    # 共通: timeout / retry（未指定なら安全側の既定値を入れる）
    if "timeout" not in kwargs:
        kwargs["timeout"] = _as_float(_env("LLM_TIMEOUT") or "180", 180.0)
    if "max_retries" not in kwargs:
        kwargs["max_retries"] = _as_int(_env("LLM_MAX_RETRIES") or "2", 2)

    # NOTE: 本リポジトリでは temperature は基本0運用。既存コードも temperature を渡していない。
    # temperature = _as_float(_env("TEMPERATURE") or "0", 0.0)

    if provider in {"azure", "azureopenai", "azure_openai"}:
        from langchain_openai import AzureChatOpenAI

        endpoint = _env("AZURE_OPENAI_ENDPOINT")
        api_key = _env("AZURE_OPENAI_API_KEY")
        api_version = _env("AZURE_OPENAI_API_VERSION") or _env("OPENAI_API_VERSION")
        deployment = (
            (str(model).strip() if model else "")
            or _env("AZURE_OPENAI_DEPLOYMENT_NAME")
            or _env("AZURE_OPENAI_DEPLOYMENT")
            or _env("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
        )
        if not endpoint or not api_key:
            raise RuntimeError("Azure OpenAI を使うには AZURE_OPENAI_ENDPOINT と AZURE_OPENAI_API_KEY が必要です。")
        if not api_version:
            raise RuntimeError("Azure OpenAI を使うには AZURE_OPENAI_API_VERSION（または OPENAI_API_VERSION）が必要です。")
        if not deployment:
            raise RuntimeError("Azure OpenAI を使うには AZURE_OPENAI_DEPLOYMENT_NAME（または model 指定）が必要です。")
        return AzureChatOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            azure_deployment=deployment,
            **kwargs,
        )

    if provider in {"gemini", "google", "google_genai"}:
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = _env("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Gemini(AI Studio) を使うには GOOGLE_API_KEY が必要です。")
        model_name = (str(model).strip() if model else "") or _env("GEMINI_MODEL") or "gemini-2.0-flash"
        # ChatGoogleGenerativeAI は google_api_key を受け取れる
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, **kwargs)

    if provider in {"anthropic", "claude"}:
        from langchain_anthropic import ChatAnthropic

        api_key = _env("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("Claude を使うには ANTHROPIC_API_KEY が必要です。")
        model_name = (str(model).strip() if model else "") or _env("CLAUDE_MODEL") or "claude-haiku-4-5-20250514"
        return ChatAnthropic(model=model_name, api_key=api_key, **kwargs)

    # OpenAI (default)
    from langchain_openai import ChatOpenAI

    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI を使うには OPENAI_API_KEY が必要です。")
    model_name = (str(model).strip() if model else "") or _env("OPENAI_MODEL") or _env("MODEL") or "gpt-5-mini"
    return ChatOpenAI(model=model_name, api_key=api_key, **kwargs)


def build_embeddings(*, model: Optional[str] = None, **kwargs: Any):
    """
    LangChainのEmbeddingを構築する（OpenAI/Azure/Gemini/Claude）。

    - OpenAI:
      - env: OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL(任意)
    - Azure OpenAI:
      - env: LLM_PROVIDER=azure, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_VERSION
      - env: AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME(任意; 未指定時は model へフォールバック)
    - Gemini (AI Studio):
      - env: LLM_PROVIDER=gemini, GOOGLE_API_KEY
      - default model: text-embedding-004
    - Claude (Anthropic):
      - Voyage AI Embedding を使用（Anthropic 推奨）
      - env: VOYAGE_API_KEY（必須）, VOYAGE_EMBEDDING_MODEL(任意; デフォルト voyage-3-large)
    """
    provider = _provider()

    if provider in {"azure", "azureopenai", "azure_openai"}:
        from langchain_openai import AzureOpenAIEmbeddings

        endpoint = _env("AZURE_OPENAI_ENDPOINT")
        api_key = _env("AZURE_OPENAI_API_KEY")
        api_version = _env("AZURE_OPENAI_API_VERSION") or _env("OPENAI_API_VERSION")
        deployment = (
            (str(model).strip() if model else "")
            or _env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME")
            or _env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
        )
        if not endpoint or not api_key:
            raise RuntimeError("Azure OpenAI embeddings を使うには AZURE_OPENAI_ENDPOINT と AZURE_OPENAI_API_KEY が必要です。")
        if not api_version:
            raise RuntimeError("Azure OpenAI embeddings を使うには AZURE_OPENAI_API_VERSION（または OPENAI_API_VERSION）が必要です。")
        if not deployment:
            raise RuntimeError("Azure OpenAI embeddings を使うには AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME（または model 指定）が必要です。")
        return AzureOpenAIEmbeddings(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            azure_deployment=deployment,
            **kwargs,
        )

    if provider in {"gemini", "google", "google_genai"}:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        api_key = _env("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Gemini(AI Studio) embeddings を使うには GOOGLE_API_KEY が必要です。")
        model_name = (str(model).strip() if model else "") or _env("GEMINI_EMBEDDING_MODEL") or "text-embedding-004"
        return GoogleGenerativeAIEmbeddings(model=model_name, google_api_key=api_key, **kwargs)

    if provider in {"anthropic", "claude"}:
        # Voyage AI Embedding を使用（Anthropic 推奨）
        from langchain_voyageai import VoyageAIEmbeddings

        api_key = _env("VOYAGE_API_KEY")
        if not api_key:
            raise RuntimeError("Claude の Embedding には VOYAGE_API_KEY が必要です。")
        model_name = (str(model).strip() if model else "") or _env("VOYAGE_EMBEDDING_MODEL") or "voyage-3-large"
        return VoyageAIEmbeddings(model=model_name, voyage_api_key=api_key, **kwargs)

    # OpenAI (default)
    from langchain_openai import OpenAIEmbeddings

    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI embeddings を使うには OPENAI_API_KEY が必要です。")
    model_name = (str(model).strip() if model else "") or _env("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-large"
    return OpenAIEmbeddings(model=model_name, api_key=api_key, **kwargs)


def provider_ids(*, chat_model: Optional[str] = None, embedding_model: Optional[str] = None) -> ProviderIds:
    """
    キャッシュ識別などに使う、provider + modelの識別子を返す。
    """
    provider = _provider()
    if provider in {"azure", "azureopenai", "azure_openai"}:
        chat = (
            (chat_model or "").strip()
            or _env("AZURE_OPENAI_DEPLOYMENT_NAME")
            or _env("AZURE_OPENAI_DEPLOYMENT")
            or _env("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
            or "azure-chat"
        )
        emb = (embedding_model or "").strip() or _env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME") or _env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") or "azure-emb"
        return ProviderIds(provider="azure", chat_id=f"azure:{chat}", embedding_id=f"azure:{emb}")
    if provider in {"gemini", "google", "google_genai"}:
        chat = (chat_model or "").strip() or _env("GEMINI_MODEL") or "gemini-2.0-flash"
        emb = (embedding_model or "").strip() or _env("GEMINI_EMBEDDING_MODEL") or "text-embedding-004"
        return ProviderIds(provider="gemini", chat_id=f"gemini:{chat}", embedding_id=f"gemini:{emb}")
    if provider in {"anthropic", "claude"}:
        chat = (chat_model or "").strip() or _env("CLAUDE_MODEL") or "claude-haiku-4-5-20250514"
        # Voyage AI Embedding を使用
        emb = (embedding_model or "").strip() or _env("VOYAGE_EMBEDDING_MODEL") or "voyage-3-large"
        return ProviderIds(provider="claude", chat_id=f"claude:{chat}", embedding_id=f"voyage:{emb}")
    chat = (chat_model or "").strip() or _env("OPENAI_MODEL") or _env("MODEL") or "gpt-5-mini"
    emb = (embedding_model or "").strip() or _env("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-large"
    return ProviderIds(provider="openai", chat_id=f"openai:{chat}", embedding_id=f"openai:{emb}")

