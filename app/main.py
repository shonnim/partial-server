from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import (
    chunking,
    client,
    dashboard,
    documents,
    embedding,
    grouping,
    headers,
    parsed_docs,
    parsing,
    sections,
    summarizing,
)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="partial-server")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(chunking.router)
app.include_router(client.router)
app.include_router(dashboard.router)
app.include_router(documents.router)
app.include_router(embedding.router)
app.include_router(grouping.router)
app.include_router(headers.router)
app.include_router(parsed_docs.router)
app.include_router(parsing.router)
app.include_router(sections.router)
app.include_router(summarizing.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
