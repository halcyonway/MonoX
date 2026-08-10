FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv pip install --system .

COPY core/ core/
COPY extensions/ extensions/

ENV MONOX_CONFIG=/etc/agent/config.toml
ENV MONOX_DATA=/var/agent

VOLUME ["/var/agent", "/etc/agent"]

CMD ["python", "-m", "core.main", "/etc/agent/config.toml"]