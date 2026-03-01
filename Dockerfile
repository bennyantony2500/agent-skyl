FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mc_server/ ./mc_server/
COPY back/ ./back/
COPY front/ ./front/

WORKDIR /app/back

ENV MONDAY_API_KEY=""
ENV WORK_ORDERS_BOARD_ID=""
ENV DEALS_BOARD_ID=""
ENV GROQ_API_KEY=""

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
