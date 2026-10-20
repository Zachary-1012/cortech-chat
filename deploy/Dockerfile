# Cortech Chat 云上镜像。依赖在构建时安装，使用网页的人不用在自己的电脑上 pip install。
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CORTECH_SAAS=1 \
    CORTECH_DATA_DIR=/var/lib/cortech \
    CORTECH_HOST=0.0.0.0 \
    CORTECH_PORT=8000

COPY monitor/requirements.txt /app/monitor/requirements.txt
RUN pip install --no-cache-dir -r /app/monitor/requirements.txt

COPY monitor /app/monitor
COPY frontend/dist /app/frontend/dist
COPY extensions /app/extensions

RUN mkdir -p /var/lib/cortech && chmod 755 /var/lib/cortech
VOLUME ["/var/lib/cortech"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["python", "-m", "monitor.app"]
