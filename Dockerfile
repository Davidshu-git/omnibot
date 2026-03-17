FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（Playwright Chromium 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    libxcomposite1 libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 \
    libgtk-3-0 libxdamage1 libxfixes3 fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium 内核
RUN playwright install chromium

# 代码由 volume mount 注入，此处仅用于构建层缓存
COPY . .
