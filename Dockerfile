FROM python:3.11-slim

WORKDIR /app

# DejaVu fonts are required by services/deep_research/pdf_template.py to
# render the rupee glyph (₹) and other Unicode symbols correctly in
# generated equity-research PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Create uploads directory
RUN mkdir -p uploads

# Expose port
EXPOSE 8080

# Start server
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--worker-class", "gthread", "--workers", "3", "--threads", "8", "--timeout", "120", "--graceful-timeout", "30", "--keep-alive", "5", "--max-requests", "1000", "--max-requests-jitter", "50", "--preload"]
