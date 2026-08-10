FROM python:3.11-slim

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 利用 Docker layer cache：先复制 lock 文件
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# 再复制源码
COPY core/ core/
COPY extensions/ extensions/

ENV MONOX_CONFIG=/etc/agent/config.toml
ENV MONOX_DATA=/var/agent

VOLUME ["/var/agent", "/etc/agent"]

CMD ["uv", "run", "python", "-m", "core.main", "/etc/agent/config.toml"]