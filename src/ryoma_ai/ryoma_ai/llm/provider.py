"""Model construction through LangChain's maintained native registries.

Provider identifiers use LangChain's ``provider:model`` format. Existing
Ryoma identifiers for the same integrations remain aliases. Passing an
already constructed BaseChatModel to ChatAgent/SqlAgent remains unchanged.
Jupyter's notebook-only provider discovery is not needed by the agent library.
"""

from typing import Dict, Optional

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

# These names are already published by RyomaLab's provider choices. Only
# normalize the provider prefix; Bedrock model IDs themselves contain colons.
_PROVIDER_ALIASES = {
    "openai-chat": "openai",
    "bedrock-chat": "bedrock",
    "azure-chat-openai": "azure_openai",
    "nvidia-chat": "nvidia",
    "gemini": "google_genai",
    "huggingface_hub": "huggingface",
    "togetherai": "together",
}


def load_model_provider(
    model_id: str,
    model_type: Optional[str] = "chat",
    model_parameters: Optional[Dict] = None,
) -> BaseChatModel | Embeddings:
    params = dict(model_parameters or {})
    provider, separator, model = model_id.partition(":")
    if separator and provider in _PROVIDER_ALIASES:
        model_id = f"{_PROVIDER_ALIASES[provider]}:{model}"
    if model_type == "chat":
        from langchain.chat_models import init_chat_model

        return init_chat_model(model_id, **params)
    if model_type == "embedding":
        from langchain.embeddings import init_embeddings

        return init_embeddings(model_id, **params)
    raise ValueError(f"Unsupported model type: {model_type!r}; use chat or embedding")
