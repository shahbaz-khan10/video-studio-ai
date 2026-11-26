# Docker Desktop runbook

The image builds the Vite frontend, copies `dist/` into the FastAPI runtime, and serves both on **one port**.

## Start

```powershell
docker compose up -d --build
```

Open <http://localhost:4001>. The API lives at <http://localhost:4001/api/v1> and the same-origin frontend is served by the backend process.

## Useful commands

```powershell
docker compose logs -f cinema-ai-dub
docker compose ps
docker compose restart cinema-ai-dub
docker compose down
```

## Data and models

`./data` on the host is mounted at `/app/data`, so existing projects, uploads, `cinema.db` and downloaded model caches remain visible and persist across container rebuilds.

CPU-only PyTorch wheels are installed from the PyTorch CPU index to avoid pulling CUDA runtime packages. All optional AI backends stay local/CPU by default; no Hugging Face token or paid API is required for the default models.

The first transcription, diarization or translation job downloads its open-source model into `./data/models`, so allow internet access on first use. Docker Desktop normally provides this automatically.
