# 模拟模型服务：只在演练/自测时使用，见 server.py 顶部说明。
# 刻意只用 python 标准库，构建不装任何依赖 —— 断网环境里也能构建成功。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py ./

RUN useradd --create-home --uid 1000 --shell /bin/bash mock
USER mock

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:8000/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]
