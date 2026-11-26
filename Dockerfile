# syntax=docker/dockerfile:1

# Stage 1: build the React/Vite frontend.
FROM node:22-bookworm-slim AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: API + compiled frontend on a single port.
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe are used for probing, decoding and dialogue-centred frame sampling.
# libgomp1/libglib2.0-0 cover the CPU-only NumPy/scikit-learn/OpenCV wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only PyTorch wheels first. requirements.txt still declares
# torch>=2.7,<3, so the second pip step keeps these wheels instead of pulling
# the much larger CUDA build from PyPI.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    "torch==2.15.0+cpu" "torchaudio==2.11.1+cpu"

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY --from=frontend /app/dist ./dist
RUN mkdir -p /app/data

WORKDIR /app/backend
EXPOSE 4001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4001"]
