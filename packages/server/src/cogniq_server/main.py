import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cogniq_shared.config import settings
from cogniq_shared.registry.database import connect, disconnect

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    logger.info("Starting cogniq-server (%s)", settings.environment)
    db = await connect(settings.mongodb_uri, settings.mongodb_db_name)

    # Initialize orchestrator with task queue
    from cogniq_shared.registry.repository import IssueRepository
    from cogniq_server.orchestrator.engine import OrchestrationEngine
    from cogniq_server.orchestrator.watchers import GateWatcher, ResultWatcher, StaleTaskReaper
    from cogniq_shared.taskqueue.repository import TaskQueueRepository
    from cogniq_server.dependencies import set_orchestrator

    repo = IssueRepository(db)
    task_queue = TaskQueueRepository(db)
    engine = OrchestrationEngine(repo=repo, task_queue=task_queue)

    # Store singletons for dependency injection
    set_orchestrator(engine, task_queue)

    # Start watchers
    result_watcher = ResultWatcher(engine=engine, task_queue=task_queue, db=db)
    stale_reaper = StaleTaskReaper(
        task_queue=task_queue,
        timeout_seconds=settings.worker_stale_timeout_seconds,
        check_interval=30.0,
    )
    gate_watcher = GateWatcher(repo=repo, settings=settings)

    await result_watcher.start()
    await stale_reaper.start()
    await gate_watcher.start()

    logger.info("Orchestrator ready (task_queue + watchers started)")

    yield

    # Shutdown
    await gate_watcher.stop()
    await stale_reaper.stop()
    await result_watcher.stop()
    await disconnect()
    logger.info("cogniq-server stopped")


app = FastAPI(
    title="cogniq-server",
    description="AI-powered software development automation platform",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173", "http://localhost:5174", "https://cogniq.pro", "https://www.cogniq.pro"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Register routers
def _register_routers() -> None:
    from cogniq_server.auth.router import router as auth_router
    from cogniq_server.api.teams import router as teams_router
    from cogniq_server.api.projects import router as projects_router
    from cogniq_server.api.issues import router as issues_router
    from cogniq_server.api.pipelines import router as pipelines_router
    from cogniq_server.api.documents import router as documents_router
    from cogniq_server.api.artifacts import router as artifacts_router
    from cogniq_server.api.events import router as events_router
    from cogniq_server.api.runs import router as runs_router
    from cogniq_server.api.dashboard import router as dashboard_router
    from cogniq_server.api.webhooks import router as webhooks_router
    from cogniq_server.api.code_sessions import router as code_sessions_router

    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(teams_router, prefix="/api", tags=["teams"])
    app.include_router(projects_router, prefix="/api", tags=["projects"])
    app.include_router(issues_router, prefix="/api", tags=["issues"])
    app.include_router(pipelines_router, prefix="/api", tags=["pipelines"])
    app.include_router(documents_router, prefix="/api", tags=["documents"])
    app.include_router(artifacts_router, prefix="/api", tags=["artifacts"])
    app.include_router(events_router, prefix="/api", tags=["events"])
    app.include_router(runs_router, prefix="/api", tags=["runs"])
    app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
    app.include_router(webhooks_router, prefix="/api/webhooks", tags=["webhooks"])
    app.include_router(code_sessions_router, prefix="/api", tags=["code_sessions"])


_register_routers()
