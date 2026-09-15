from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.database import init_db
from .api import auth, machines, machine_groups, images, jobs, installer, agent_files, winpe, users
from .services.image_store_scanner import start_image_store_scanner

app = FastAPI(
    title="Bretter-IMG Console",
    description="Remote imaging management server — Ghost Console replacement",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(machines.router)
app.include_router(machine_groups.router)
app.include_router(images.router)
app.include_router(jobs.router)
app.include_router(installer.router)
app.include_router(agent_files.router)
app.include_router(winpe.router)


@app.on_event("startup")
def startup():
    init_db()
    start_image_store_scanner()


@app.get("/")
def root():
    return {"app": "Bretter-IMG", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}
