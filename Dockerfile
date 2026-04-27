FROM omnibot-base:latest

WORKDIR /app
ENV TZ=Asia/Shanghai

COPY . .

RUN chmod +x /app/entrypoint.mhxy.sh 2>/dev/null || true
