FROM python:3.12-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Chrome 和虚拟显示器只安装在本项目镜像内。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates xvfb xauth fonts-noto-cjk \
    && curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev \
    && useradd --uid 1000 --create-home --shell /bin/sh vat \
    && chown -R vat:vat /app /opt/venv

USER vat
EXPOSE 8765
CMD ["sh", "/app/deploy/start.sh"]
