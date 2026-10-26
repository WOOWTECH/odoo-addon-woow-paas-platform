FROM python:3.11.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway

USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
