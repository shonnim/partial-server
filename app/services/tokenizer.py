"""Counting tokens the way the embedding model will.

The chunk budget is set for ``solar-embedding-2-passage``; the Qwen3
tokenizer is a close enough stand-in for how it tokenises Korean, and it
loads offline once the files are in the Hugging Face cache. The tokenizer is
built once per process — loading it is a couple of seconds, counting is
milliseconds.
"""

from functools import lru_cache

from tokenizers import Tokenizer

TOKENIZER_NAME = "Qwen/Qwen3-0.6B"


@lru_cache(maxsize=1)
def get_tokenizer() -> Tokenizer:
    return Tokenizer.from_pretrained(TOKENIZER_NAME)


def count_tokens(text: str) -> int:
    """How many tokens ``text`` is, without the special tokens a model adds."""
    if not text:
        return 0
    return len(get_tokenizer().encode(text, add_special_tokens=False).ids)
