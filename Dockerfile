FROM python:3.12-slim

WORKDIR /srv

# system deps kept minimal; no CV libs in the API image (detection runs separately)
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

COPY app/ ./app/
COPY data/ ./data/

# seed POS + layout into the mounted /data volume at runtime via env
ENV STORE_DB_PATH=/data/store_intel.db \
    POS_CSV_PATH=/data/pos_transactions.csv \
    STORE_LAYOUT_PATH=/data/store_layout.json

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
