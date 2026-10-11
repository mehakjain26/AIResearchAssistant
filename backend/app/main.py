import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import Base, engine
from .errors import AppError
from .routers import analysis, papers, search
from .services import storage

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    try:
        storage.ensure_bucket()
    except Exception as e:  # object storage may be down; search and library listing still work
        log.warning("Could not reach object storage at startup: %s", e)
    yield


app = FastAPI(title="AI Research Assistant", lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


app.include_router(search.router)
app.include_router(papers.router)
app.include_router(analysis.router)
