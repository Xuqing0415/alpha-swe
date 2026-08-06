"""嵌入器 —— 向量化抽象与实现。

优先级（auto）: sentence-transformers 本地模型 > OpenAI Embeddings API > sklearn TF-IDF。
TF-IDF 使用字符 n-gram，中英文与代码符号均无需分词即可向量化，且零外部下载。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import urllib.request
from abc import ABC, abstractmethod
from typing import List

import numpy as np

logger = logging.getLogger("alpha-swe.memory.embed")


class Embedder(ABC):
    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """返回 L2 归一化向量列表。"""


class TfidfEmbedder(Embedder):
    """字符 n-gram TF-IDF 向量化（零外部依赖）。"""

    def __init__(self, max_features: int = 8192, ngram_range=(2, 4)):
        from sklearn.feature_extraction.text import TfidfVectorizer

        self._fixed_dim = max_features
        self._vectorizer = TfidfVectorizer(
            max_features=max_features,
            ngram_range=ngram_range,
            analyzer="char_wb",
            norm="l2",
        )
        self._fitted = False

    @property
    def dim(self) -> int:
        return self._fixed_dim

    def fit(self, texts: List[str]) -> None:
        corpus = [t for t in texts if t and t.strip()]
        self._vectorizer.fit(corpus or [""])
        self._fitted = True

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not self._fitted:
            self.fit(texts)
        arr = self._vectorizer.transform(texts).toarray()
        if arr.shape[1] < self._fixed_dim:
            arr = np.pad(arr, ((0, 0), (0, self._fixed_dim - arr.shape[1])))
        return arr.tolist()


class SentenceTransformersEmbedder(Embedder):
    """本地 sentence-transformers 模型（首次使用需下载模型权重）。"""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("sentence-transformers 未安装") from e
        self._model = SentenceTransformer(model_name)
        self._dim = self._model.get_sentence_embedding_dimension()

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return vectors.tolist()


class OpenAIEmbedder(Embedder):
    """OpenAI Embeddings API（标准库 urllib，无额外依赖）。"""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small",
                 base_url: str = "", dim: int = 1536):
        if not api_key:
            raise ValueError("OpenAIEmbedder 需要 api_key（配置 embedding_api_key_env）")
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vectors = [item["embedding"] for item in data["data"]]
        # L2 归一化
        return [(_normalize(v)).tolist() for v in np.asarray(vectors, dtype=np.float64)]


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def build_embedder(config) -> Embedder:
    """按 MemoryConfig 构造嵌入器。"""
    kind = config.embedder
    if kind == "sentence-transformers":
        return SentenceTransformersEmbedder(config.embedding_model or "all-MiniLM-L6-v2")
    if kind == "openai":
        api_key = os.environ.get(config.embedding_api_key_env or "OPENAI_API_KEY", "")
        return OpenAIEmbedder(
            api_key=api_key,
            model=config.embedding_model or "text-embedding-3-small",
            base_url=config.embedding_base_url or "",
        )
    if kind == "tfidf":
        return TfidfEmbedder()
    # auto: 有本地模型用本地模型，否则 TF-IDF
    if importlib.util.find_spec("sentence_transformers") is not None:
        try:
            return SentenceTransformersEmbedder(config.embedding_model or "all-MiniLM-L6-v2")
        except Exception as e:  # 模型加载失败时回退
            logger.warning("sentence-transformers 加载失败，回退 TF-IDF: %s", e)
    return TfidfEmbedder()