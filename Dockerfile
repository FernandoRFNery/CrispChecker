FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libdbus-1-3 \
    libgtk-3-0 \
    libxss1 \
    libgbm1 \
    libpangocairo-1.0-0 \
    fonts-liberation \
    libu2f-udev \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install --with-deps

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
