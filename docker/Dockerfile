# Alpha-SWE 沙箱基础镜像：python 3.11 + 常用开发工具
# 构建: docker build -t alphaswe/dev:latest -f docker/Dockerfile .
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl wget ca-certificates vim bash gnupg \
        nodejs npm jq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir pytest \
    && node --version && python --version && git --version && python -m pytest --version

WORKDIR /workspace
CMD ["/bin/bash", "-c", "sleep infinity"]
