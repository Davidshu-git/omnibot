FROM omnibot-base:latest

WORKDIR /app
ENV TZ=Asia/Shanghai

COPY . .
