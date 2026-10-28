FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY router.py .
# One worker only: routing state (pod list, cache table, in-flight counts) is
# in-process, and several workers would each keep their own separate copy.
CMD ["uvicorn", "router:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
