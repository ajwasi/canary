FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
# Hash-locked and wheels only: nothing is built or run from source, and every file is checked against requirements.txt.
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.txt
COPY app app
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
ENV DATA_DIR=/data
USER app
EXPOSE 8000
# One worker only: the app holds a single MQTT session with the broker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
