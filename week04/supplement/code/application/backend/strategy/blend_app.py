"""Isolated UI for the fixed exploratory spread and independent paper controls."""
import truststore
truststore.inject_into_ssl()
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from backend.nh_api import runtime
from backend.strategy.blend_runtime import BlendRuntime,service
from backend.strategy.blend_mock import BlendMock
from backend.ui_static import UiStaticFiles

@asynccontextmanager
async def lifespan(app):
    worker=BlendRuntime(service)
    await worker.start()
    service.mock=BlendMock(service)
    await service.mock.start()
    try:yield
    finally:
        await service.mock.stop();await worker.stop();runtime.reset()

app=FastAPI(title='NH Partial Hedge Forward',lifespan=lifespan)
class StartRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode:Literal['paper']='paper'
    duration_seconds:Literal[1800,10800]=10800

class ManualCloseRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:str
    trade_at:int=Field(gt=0)

@app.get('/api/strategy/rotation')
def status():return service.view()

@app.post('/api/strategy/rotation/start')
def start(ask:StartRequest):
    try:return service.start(ask.duration_seconds)
    except ValueError as exc:raise HTTPException(400,detail=str(exc)) from exc

@app.post('/api/strategy/rotation/stop')
def stop():return service.stop()

@app.post('/api/strategy/rotation/manual-close')
def manual_close(ask:ManualCloseRequest):
    try:return service.manual_close(ask.run_id,ask.trade_at)
    except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc

@app.post('/api/strategy/rotation/reset')
async def reset():
    try:return await service.reset()
    except ValueError as exc:raise HTTPException(400,detail=str(exc)) from exc

@app.get('/api/strategy/rotation/export')
def export():
    return Response(service.export(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="blend-forward.zip"'})

app.mount('/',UiStaticFiles(directory=str(Path(__file__).resolve().parents[1]/'static'),html=True),name='ui')
