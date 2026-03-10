FROM python:3.9-slim

# Instala o Ghostscript (a prensa hidráulica de PDFs)
RUN apt-get update && apt-get install -y ghostscript && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install flask werkzeug

COPY . .

EXPOSE 8095

CMD ["python", "app.py"]
