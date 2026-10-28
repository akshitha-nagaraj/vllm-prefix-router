FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.28.1
WORKDIR /app
COPY loadgen.py .
CMD ["python", "loadgen.py"]
