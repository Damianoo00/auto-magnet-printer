FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libheif-plugin-aomdec \
    libheif-plugin-libde265 \
    libheif1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    libcairo2 \
    libpangoft2-1.0-0 \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY template.jinja2 .
COPY templates/ templates/

RUN mkdir -p incoming pending archive rejected

EXPOSE 8080

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8080"]
