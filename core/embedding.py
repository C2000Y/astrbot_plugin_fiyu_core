"""Provider capabilities and validation for ordered text embeddings."""

from dataclasses import dataclass
import math

from .util import conservative_tokens, dump, ident


DASHSCOPE_V4_DIMENSIONS = (64, 128, 256, 512, 768, 1024, 1536, 2048)


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    key: str
    max_texts: int
    max_text_tokens: int
    max_batch_tokens: int
    dimensions: tuple[int, ...]
    expected_dimension: int
    input_format: str

    def fingerprint(self, dimension):
        return ident(f"{self.key}:{dimension}", 32) if dimension else ""

    def fits(self, text):
        tokens = input_size(text)
        return bool(text.strip()) and all(
            not limit or tokens <= limit for limit in (self.max_text_tokens, self.max_batch_tokens)
        )


def input_size(text):
    # Packing leaves a margin without depending on a specific provider tokenizer.
    return max(len(text.encode("utf-8")), conservative_tokens(text))


def embedding_profile(provider, metadata, overrides):
    known = metadata["model"].casefold().rsplit("/", 1)[-1] == "text-embedding-v4"
    limits = overrides.get(provider, {})
    single = limits.get("max_text_tokens", 0)
    declared = metadata.get("dimension", 0)
    expected = limits.get("expected_dimension", 0) or declared
    if declared and expected != declared:
        raise ValueError("Embedding 预期维度与 Provider 配置不一致")
    if known:
        if expected and expected not in DASHSCOPE_V4_DIMENSIONS:
            raise ValueError("text-embedding-v4 不支持配置的维度")
        single = min(single or 8192, 8192)
    data = {
        "provider": provider,
        "max_texts": min(limits.get("max_texts", 10), 10) if known else limits.get("max_texts", 10),
        "max_text_tokens": single,
        "max_batch_tokens": limits.get("max_batch_tokens", 0),
        "dimensions": DASHSCOPE_V4_DIMENSIONS if known else (),
        "expected_dimension": expected,
        "input_format": metadata.get("input_format", "texts"),
    }
    key = ident(dump(["title-full-or-gist-v1", metadata["identity"], data]), 32)
    return EmbeddingProfile(key=key, **data)


def batches(profile, texts):
    start = 0
    size = 0
    for index, text in enumerate(texts):
        tokens = input_size(text)
        if not profile.fits(text):
            raise ValueError("Embedding 输入超出当前单条限制")
        if index - start == profile.max_texts or (
            profile.max_batch_tokens and size + tokens > profile.max_batch_tokens
        ):
            yield start, texts[start:index]
            start, size = index, 0
        size += tokens
    if start < len(texts):
        yield start, texts[start:]


class EmbeddingProtocolError(ValueError):
    pass


class EmbeddingProviderError(RuntimeError):
    def __init__(self, status_code, code):
        super().__init__("Embedding Provider 请求失败")
        self.status_code = int(status_code) if isinstance(status_code, int) else None
        self.code = code


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int | None = None


def ordered_vectors(indexed, count):
    vectors = {}
    for index, vector in indexed:
        if type(index) is not int or not 0 <= index < count or index in vectors:
            raise EmbeddingProtocolError("Embedding 响应位置无效")
        vectors[index] = vector
    if len(vectors) != count:
        raise EmbeddingProtocolError("Embedding 响应条数不匹配")
    return [vectors[i] for i in range(count)]


def validated_vectors(vectors, count, profile):
    if not isinstance(vectors, list) or not vectors or len(vectors) != count:
        raise EmbeddingProtocolError("Embedding 响应条数不匹配")
    size = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if (
        not 1 <= size <= 8192
        or profile.expected_dimension and size != profile.expected_dimension
        or profile.dimensions and size not in profile.dimensions
        or any(
            not isinstance(v, list) or len(v) != size
            or any(type(x) not in (int, float) or not -1e10 <= x <= 1e10 or not math.isfinite(x) for x in v)
            for v in vectors
        )
    ):
        raise EmbeddingProtocolError("Embedding 响应维度或数值无效")
    return size
