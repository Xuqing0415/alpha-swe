"""下载 sentence-transformers 本地模型（离线运行前执行一次）。

背景：本机 Python（如 .venv 托管解释器）可能缺少系统证书库，直连 HuggingFace 会报
"SSL: CERTIFICATE_VERIFY_FAILED"；且 huggingface_hub 共享 httpx.Client 在失败重试时
会抛 "Cannot send a request, as the client has been closed."。

本脚本通过 huggingface_hub.set_client_factory 注入 verify=False 的 httpx.Client，
一次下载到本地目录；之后配置 memory.embedding_model_path 指向该目录，Agent 全程离线
（local_files_only），不再发起任何网络请求。

用法：
    python -X utf8 scripts/download_embedding_model.py
    python -X utf8 scripts/download_embedding_model.py --model "BAAI/bge-small-zh-v1.5"
    python -X utf8 scripts/download_embedding_model.py --out "D:/models/mymodel"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "models" / "sentence-transformers"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -X utf8 scripts/download_embedding_model.py",
        description="下载 sentence-transformers 本地模型（含 SSL 证书绕过）")
    parser.add_argument("--model", default="all-MiniLM-L6-v2",
                        help="HuggingFace 模型名（短名自动映射到 sentence-transformers/ 前缀）")
    parser.add_argument("--out", default=None,
                        help="本地输出目录（默认 ./models/sentence-transformers/<model>）")
    parser.add_argument("--verify", action="store_true",
                        help="保留 SSL 证书校验（默认关闭，规避本机证书库缺失）")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="HTTP 超时秒数（默认 120）")
    args = parser.parse_args(argv)

    repo_id = args.model
    if "/" not in repo_id:
        repo_id = f"sentence-transformers/{repo_id}"
    out = Path(args.out) if args.out else DEFAULT_OUT / args.model
    out.mkdir(parents=True, exist_ok=True)

    try:
        import httpx
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils._http import set_client_factory
    except ImportError as e:
        print(f"缺少依赖（httpx / huggingface_hub）: {e}", file=sys.stderr)
        return 1

    # 注入自定义 httpx 客户端工厂：set_client_factory 内部会先 close_session()
    # 重置共享客户端（httpx 生命周期修复），再替换工厂为 verify=False 的实现。
    def _client_factory() -> httpx.Client:
        return httpx.Client(
            verify=not args.verify,
            follow_redirects=True,
            timeout=args.timeout,
        )

    set_client_factory(_client_factory)

    print(f"下载 {repo_id} -> {out} （verify={not args.verify}）...")
    try:
        snapshot_download(repo_id=repo_id, local_dir=str(out))
    except Exception as e:
        print(f"下载失败: {e}", file=sys.stderr)
        return 1

    local = str(out).replace("\\", "/")
    print("下载完成 ✅")
    print("请在 config/agent.yaml 中配置：")
    print(f'  memory.embedding_model_path: "{local}"')
    print("之后 Agent 将全程离线加载该模型，不再访问 HuggingFace。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
