# main.py
"""
MONIAC — гидравлический компьютер Филлипса.
FastAPI-приложение: сервер считает дифференциальные уравнения,
клиент рисует анимацию и шлёт параметры через WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────
#  МОДЕЛЬ
# ────────────────────────────────────────────────────────────────────

TANK_H = 150.0      # ёмкость резервуара
SUMP_H = 62.0       # ёмкость нижнего резервуара
K_TANK = 0.25       # коэффициент слива Сатро-Вейра (линейный)
K_SUMP = 3.0        # коэффициент насоса


class Params(BaseModel):
    c: float = Field(0.80, ge=0.0, le=1.0, description="склонность к потреблению")
    t: float = Field(0.20, ge=0.0, le=1.0, description="ставка налога")
    m: float = Field(0.15, ge=0.0, le=1.0, description="склонность к импорту")
    G: float = Field(15.0, ge=0.0, le=100.0, description="госрасходы")
    I0: float = Field(20.0, ge=0.0, le=100.0, description="автономные инвестиции")
    r: float = Field(5.0, ge=0.0, le=30.0, description="процентная ставка, %")
    X: float = Field(10.0, ge=0.0, le=100.0, description="экспорт")


@dataclass
class State:
    """Внутреннее состояние гидравлической машины."""
    h_gov: float = 22.0     # уровень в резервуаре правительства
    h_sav: float = 22.0     # уровень в резервуаре сбережений
    h_sump: float = 20.0    # уровень в нижнем резервуаре
    Y: float = 60.0         # национальный доход (поток насоса)
    t_sec: float = 0.0      # модельное время
    # мгновенные потоки (для отрисовки)
    T: float = 0.0
    S: float = 0.0
    C: float = 0.0
    Cd: float = 0.0
    M: float = 0.0
    G_out: float = 0.0
    I_out: float = 0.0
    X: float = 0.0


def investment(p: Params) -> float:
    return max(0.0, p.I0 * (1.0 - p.r / 15.0))


def step(s: State, p: Params, dt: float) -> None:
    """Один шаг интегрирования (Эйлер, малый dt)."""
    I = investment(p)
    G, X = p.G, p.X

    # распределение дохода
    T = p.t * s.Y
    Yd = max(0.0, s.Y - T)
    C = p.c * Yd
    M = C * p.m
    Cd = C - M
    S = Yd - C

    # сливы из резервуаров
    G_out = min(G, K_TANK * s.h_gov)
    I_out = min(I, K_TANK * s.h_sav)

    s.h_gov += (T - G_out) * dt
    s.h_sav += (S - I_out) * dt

    over = 0.0
    if s.h_gov > TANK_H:
        over += (s.h_gov - TANK_H) / dt
        s.h_gov = TANK_H
    if s.h_sav > TANK_H:
        over += (s.h_sav - TANK_H) / dt
        s.h_sav = TANK_H
    s.h_gov = max(0.0, s.h_gov)
    s.h_sav = max(0.0, s.h_sav)

    # нижний резервуар и насос
    pump = K_SUMP * s.h_sump
    s.h_sump += (Cd + G_out + I_out + X + over - pump) * dt
    s.h_sump = max(0.0, min(SUMP_H, s.h_sump))
    s.Y = pump

    s.T, s.S, s.C, s.Cd, s.M = T, S, C, Cd, M
    s.G_out, s.I_out, s.X = G_out, I_out, X
    s.t_sec += dt


def snapshot(s: State, p: Params) -> dict:
    mult = 1.0 / (1.0 - p.c * (1.0 - p.t) * (1.0 - p.m)) if p.c * (1.0 - p.t) * (1.0 - p.m) < 1 else 0.0
    return {
        "hGov": s.h_gov, "hSav": s.h_sav, "hSump": s.h_sump,
        "Y": s.Y, "tSec": s.t_sec,
        "flows": {"T": s.T, "S": s.S, "C": s.C, "Cd": s.Cd,
                  "M": s.M, "Gout": s.G_out, "Iout": s.I_out, "X": s.X},
        "params": p.model_dump(),
        "multiplier": mult,
    }


# ────────────────────────────────────────────────────────────────────
#  СЕРВЕР
# ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="MONIAC", lifespan=lifespan)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>MONIAC</h1><p>Положите index.html в папку static/</p>")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_sim(ws: WebSocket):
    """
    Протокол:
      клиент → сервер: {"type":"params", "data":{...}}  |  {"type":"reset"}
      сервер → клиент: {"type":"state", ...snapshot...} каждые ~33 мс
    """
    await ws.accept()
    params = Params()
    state = State()
    reset_flag = False
    paused = False

    async def reader():
        nonlocal params, reset_flag, paused
        while True:
            msg = await ws.receive_text()
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "params":
                try:
                    params = Params(**obj.get("data", {}))
                except Exception:
                    pass
            elif kind == "reset":
                reset_flag = True
            elif kind == "pause":
                paused = bool(obj.get("value", not paused))

    reader_task = asyncio.create_task(reader())

    FPS = 30
    DT_FRAME = 1.0 / FPS
    SUBSTEPS = 2

    try:
        while True:
            t0 = time.perf_counter()

            if reset_flag:
                state = State()
                reset_flag = False

            # интегрируем
            sub_dt = DT_FRAME / SUBSTEPS
            if not paused:
                for _ in range(SUBSTEPS):
                    step(state, params, sub_dt)

            # отправляем снапшот
            payload = {"type": "state", **snapshot(state, params)}
            await ws.send_text(json.dumps(payload))

            # держим FPS
            elapsed = time.perf_counter() - t0
            await asyncio.sleep(max(0.0, DT_FRAME - elapsed))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)