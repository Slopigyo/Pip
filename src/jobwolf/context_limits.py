"""Local context policy, independent of the model's tokenizer or private workspace."""

from typing import Literal

ContextTokens = Literal[16384, 32768, 65536, 128000]
DEFAULT_CONTEXT_TOKENS: ContextTokens = 128000
CONTEXT_CHOICES = (16384, 32768, 65536, 128000)
# 3,072 output tokens plus room for model-specific framing/system templates.
CONTEXT_RESERVE = 4096


def local_input_bytes(context_tokens: int = DEFAULT_CONTEXT_TOKENS) -> int:
    """Conservative byte guard, NOT an exact token count or a bytes/token conversion.

    Without the selected model's tokenizer, budget one input byte per token rather
    than assuming English's typical ~4 chars/token for arbitrary Unicode/JSON.
    This intentionally leaves much of a model's nominal capacity unused. Ollama's
    supported context and memory availability still depend on the installed model.
    """
    if type(context_tokens) is not int or context_tokens not in CONTEXT_CHOICES:
        raise ValueError("Choose a supported local context window.")
    return context_tokens - CONTEXT_RESERVE
