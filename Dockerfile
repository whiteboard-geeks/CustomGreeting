# Use Python 3.11 so pyarrow & streamlit get prebuilt wheels
FROM python:3.11-slim

# Work directory inside the container
WORKDIR /app

# Saner Python defaults
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System packages needed for moviepy / ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy the rest of the app
COPY . .

# Optional: Streamlit default port (Render will override with $PORT)
EXPOSE 8501

# Render provides $PORT. Use it, fallback to 8501 for local runs.
CMD ["sh", "-c", "streamlit run app.py --server.port ${PORT:-8501} --server.address 0.0.0.0"]
