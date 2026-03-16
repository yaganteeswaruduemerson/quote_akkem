

FROM python:3.11-slim

# Install OS dependencies for pyodbc, SQL Server drivers, and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        build-essential \
        unixodbc-dev \
        libssl-dev \
        curl \
        gnupg \
        ca-certificates \
        libpq-dev \
        libffi-dev \
        libxml2-dev \
        libxslt1-dev \
        liblzma-dev \
        && rm -rf /var/lib/apt/lists/*

# Set environment variables for Python
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Create app directory
WORKDIR /app

# Copy requirements file and install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . /app

# Expose FastAPI port
EXPOSE 8080

# Default startup command
CMD ["uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]

