FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY app app
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
ENV DATA_DIR=/data
USER app
EXPOSE 8000
# One worker only: the app holds a single MQTT session with the broker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
