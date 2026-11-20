# 개발 중 미리보기용. 키는 이미지에 넣지 않고 실행할 때 환경변수로 준다.
#   docker build -t jeju-agent .
#   docker run -d --name jeju-agent -p 8765:8765 --env-file .env jeju-agent
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
