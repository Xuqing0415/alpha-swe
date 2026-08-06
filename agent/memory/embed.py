"""嵌入器 —— 向量化抽象与实现。

优先级（auto）: sentence-transformers 本地模型 > OpenAI Embeddings API > sklearn TF-IDF。
TF-IDF 使用字符 n-gram，中英文与代码符号均无需分词即可向量化，且零外部下载。

离线策略（默认）：
- sentence-transformers 只加载本地模型（memory.embedding_model_path 或 HuggingFace 缓存），
  绝不向 huggingface.co 发起网络请求，从而规避两条已知故障：
  1) 本机 SSL 证书校验失败（CERTIFICATE_VERIFY_FAILED）；
  2) huggingface_hub 共享 httpx.Client 生命周期问题——失败重试时复用已关闭的客户端，
     抛 "Cannot send a request, as the client has been closed."。
- 任一嵌入器不可用时回退 TF-IDF，build_embedder 绝不向上抛异常（避免整个 Agent 崩溃）。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

import numpy as np

logger = logging.getLogger("alpha-swe.memory.embed")

_OFFLINE_ENV_KEYS = (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HUGGINGFACE_HUB_OFFLINE",
)


def set_hf_offline_env() -> None:
    """设置 HuggingFace/transformers 离线环境变量（幂等，进程级生效）。"""
    for key in _OFFLINE_ENV_KEYS:
        os.environ.setdefault(key, "1")


def reset_hf_http_session() -> None:
    """重置 huggingface_hub 共享 httpx.Client（httpx 生命周期根源修复）。

    huggingface_hub 的 get_session() 返回进程级共享客户端；若某次请求失败后该客户端
    处于 CLOSED 状态，后续任何请求都会抛
    "Cannot send a request, as the client has been closed."。
    close_session() 先置空全局引用再关闭旧客户端，下一次 get_session() 会重建新客户端，
    保证后续调用（含重试）永远不会复用已关闭的客户端。
    """
    try:
        from huggingface_hub.utils._http import close_session
    except Exception:
        return
    try:
        close_session()
    except Exception as e:  # 库内部清理失败不影响主流程
        logger.debug("重置 huggingface_hub 客户端失败: %s", e)


def find_local_model(model_name: str, model_path: str = "") -> Optional[str]:
    """解析可用的本地模型目录；找不到返回 None（纯文件系统检查，绝不联网）。"""
    candidates: List[Path] = []
    if model_path:
        candidates.append(Path(model_path))
    if model_name:
        p = Path(model_name)
        if p.exists():
            candidates.append(p)  # 直接传本地目录
        candidates.extend(_hf_cache_dirs(model_name))
    for c in candidates:
        try:
            if c.is_dir() and (c / "modules.json").is_file():
                return str(c)
            # HF 缓存 snapshot 形态: <hub>/models--org--name/snapshots/<commit>/
            if c.name.startswith("models--"):
                snap = _first_snapshot(c)
                if snap:
                    return snap
        except OSError:
            continue
    return None


def _hf_cache_dirs(model_name: str) -> List[Path]:
    """可能的 HF 缓存目录（覆盖带 org 与 sentence-transformers 短名映射两种形态）。"""
    hub_home = Path(os.environ.get("HF_HOME") or
                    os.path.join(Path.home(), ".cache", "huggingface")) / "hub"
    if "/" in model_name:
        return [hub_home / f"models--{model_name.replace('/', '--')}"]
    return [
        hub_home / f"models--sentence-transformers--{model_name}",
        hub_home / f"models--{model_name}",
    ]


def _first_snapshot(repo_dir: Path) -> Optional[str]:
    snap_dir = repo_dir / "snapshots"
    if not snap_dir.is_dir():
        return None
    try:
        for child in sorted(snap_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                return str(child)
    except OSError:
        return None
    return None


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
    """本地 sentence-transformers 模型（离线优先，绝不触发网络下载）。"""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2",
                 model_path: str = "", offline: bool = True):
        local = find_local_model(model_name, model_path)
        if not local:
            raise RuntimeError(
                "未找到本地 embedding 模型（离线模式）。请设置 "
                "memory.embedding_model_path 指向本地模型目录，或先运行 "
                "scripts/download_embedding_model.py 下载 "
                f"（目标模型: {model_name}）"
            )
        if offline:
            set_hf_offline_env()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("sentence-transformers 未安装") from e
        try:
            try:
                self._model = SentenceTransformer(local, local_files_only=True)
            except TypeError:  # 旧版本无 local_files_only 参数
                self._model = SentenceTransformer(local)
            self._dim = self._model.get_sentence_embedding_dimension()
        except Exception as e:
            reset_hf_http_session()  # 清理可能残留的共享客户端
            raise RuntimeError(f"本地模型加载失败（{local}）: {e}") from e

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
    """按 MemoryConfig 构造嵌入器；任何后端不可用都回退 TF-IDF（绝不抛异常）。"""
    kind = config.embedder
    model_name = config.embedding_model or "all-MiniLM-L6-v2"
    model_path = getattr(config, "embedding_model_path", "") or ""
    offline = bool(getattr(config, "embedding_offline", True))

    if kind == "sentence-transformers":
        return _build_sentence_transformers(model_name, model_path, offline)
    if kind == "openai":
        try:
            api_key = os.environ.get(
                config.embedding_api_key_env or "OPENAI_API_KEY", "")
            return OpenAIEmbedder(
                api_key=api_key,
                model=model_name,
                base_url=config.embedding_base_url or "",
            )
        except Exception as e:
            logger.warning("OpenAI 嵌入器不可用，回退 TF-IDF: %s", e)
            return TfidfEmbedder()
    if kind == "tfidf":
        return TfidfEmbedder()
    # auto: 有本地模型用本地模型，否则 TF-IDF
    return _build_sentence_transformers(model_name, model_path, offline)


def _build_sentence_transformers(model_name: str, model_path: str,
                                 offline: bool) -> Embedder:
    """构造 sentence-transformers；失败回退 TF-IDF 并重置共享 httpx 客户端。"""
    try:
        return SentenceTransformersEmbedder(
            model_name, model_path=model_path, offline=offline)
    except Exception as e:
        reset_hf_http_session()
        logger.warning("sentence-transformers 加载失败，回退 TF-IDF: %s", e)
        return TfidfEmbedder()


__all__ = [
    "Embedder", "TfidfEmbedder", "SentenceTransformersEmbedder",
    "OpenAIEmbedder", "build_embedder",
    "find_local_model", "reset_hf_http_session", "set_hf_offline_env",
]
