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

K_SUMP = 3.0        # коэффициент насоса: Y = K_SUMP * h_sump

# Денежный рынок (бак «денежных остатков» Филлипса)
KAPPA = 0.5         # спрос на деньги для сделок: L₁ = KAPPA · Y
R_MAX = 12.0        # ставка при пустом баке денежных остатков, %
BETA = 0.4          # насколько падает ставка на единицу свободных денег
LAMBDA = 0.5        # скорость перетока между активными и свободными деньгами
R_INV = 15.0        # ставка, при которой инвестиции обнуляются


class Params(BaseModel):
    c: float = Field(0.80, ge=0.0, le=1.0, description="склонность к потреблению")
    t: float = Field(0.20, ge=0.0, le=1.0, description="ставка налога")
    m: float = Field(0.15, ge=0.0, le=1.0, description="склонность к импорту")
    G: float = Field(15.0, ge=0.0, le=100.0, description="госрасходы")
    I0: float = Field(20.0, ge=0.0, le=100.0, description="автономные инвестиции")
    Ms: float = Field(60.0, ge=0.0, le=200.0, description="денежная масса")
    mm: bool = Field(True, description="денежный рынок: ставку задаёт бак свободных денег")
    r: float = Field(5.0, ge=0.0, le=30.0, description="ставка вручную (если mm выключен), %")
    X: float = Field(10.0, ge=0.0, le=100.0, description="экспорт")


@dataclass
class State:
    """
    Внутреннее состояние гидравлической машины.

    h_gov  — накопленное сальдо бюджета: > 0 профицит, < 0 госдолг.
    h_sav  — накопленное сальдо частного сектора (S − I): > 0 чистое
             богатство, < 0 чистые заимствования.
    h_sump — уровень в нижнем резервуаре; насос качает Y = K_SUMP * h_sump.
    h_idle — бак свободных денежных остатков: часть денежной массы, не занятая
             в сделках. Его уровень через поплавок задаёт процентную ставку r.
    """
    h_gov: float = 0.0
    h_sav: float = 0.0
    h_sump: float = 20.0
    h_idle: float = 30.0
    r: float = 0.0
    Y: float = 60.0
    t_sec: float = 0.0
    # мгновенные потоки (для отрисовки)
    T: float = 0.0
    S: float = 0.0
    C: float = 0.0
    Cd: float = 0.0
    M: float = 0.0
    G_out: float = 0.0
    I_out: float = 0.0
    X: float = 0.0


def liquidity_rate(idle: float) -> float:
    """
    Предпочтение ликвидности: чем больше свободных денег, тем ниже ставка.
    При пустом баке r = R_MAX; при idle ≥ R_MAX / BETA ставка упирается
    в ноль — ловушка ликвидности.
    """
    return min(R_MAX, max(0.0, R_MAX - BETA * max(0.0, idle)))


def target_idle(p: Params, Y: float) -> float:
    """Свободные остатки = денежная масса − деньги, занятые в сделках."""
    return max(0.0, p.Ms - KAPPA * Y)


def investment(p: Params, r: float) -> float:
    return max(0.0, p.I0 * (1.0 - r / R_INV))


def multiplier(p: Params) -> float:
    leak = p.c * (1.0 - p.t) * (1.0 - p.m)
    return 1.0 / (1.0 - leak) if leak < 1.0 else 0.0


def equilibrium(p: Params) -> tuple[float, float]:
    """
    Совместное равновесие товарного и денежного рынков (IS–LM):
        Y = k · (G + I(r) + X),   r = f(Ms − KAPPA · Y).
    F(Y) = k·(G + I(r(Y)) + X) − Y монотонно убывает → бисекция.
    """
    k = multiplier(p)

    def rate(Y: float) -> float:
        if not p.mm:
            return p.r
        return liquidity_rate(target_idle(p, Y))

    def F(Y: float) -> float:
        return k * (p.G + investment(p, rate(Y)) + p.X) - Y

    lo, hi = 0.0, k * (p.G + p.I0 + p.X) + 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if F(mid) > 0:
            lo = mid
        else:
            hi = mid
    Y = 0.5 * (lo + hi)
    return Y, rate(Y)


def initial_state(p: Params) -> State:
    s = State()
    s.h_idle = target_idle(p, s.Y)
    s.r = liquidity_rate(s.h_idle) if p.mm else p.r
    return s


def step(s: State, p: Params, dt: float) -> None:
    """
    Один шаг интегрирования (Эйлер, малый dt).

    Если денежный рынок включён (p.mm), ставка r не задаётся извне: её определяет уровень в баке свободных
    денежных остатков (как поплавок в машине Филлипса), а она уже
    управляет клапаном инвестиций.

    Утечки (T, S, M) уходят из оборота, инъекции (G, I, X) — экзогенные
    потоки, заданные политикой и не ограниченные уровнем в баках.
    Баки лишь копят разницу: государство может работать в дефицит
    (бак уходит в минус = госдолг).
    """
    # денежный рынок: вода перетекает между активными и свободными деньгами
    s.h_idle += LAMBDA * (target_idle(p, s.Y) - s.h_idle) * dt
    s.h_idle = max(0.0, s.h_idle)
    # бак считается всегда, чтобы при включении рынка не было скачка;
    # при выключенном рынке ставка задаётся вручную
    s.r = liquidity_rate(s.h_idle) if p.mm else p.r

    I = investment(p, s.r)
    G, X = p.G, p.X

    # распределение дохода
    T = p.t * s.Y
    Yd = s.Y - T
    C = p.c * Yd
    M = p.m * C
    Cd = C - M
    S = Yd - C

    # накопленные сальдо
    s.h_gov += (T - G) * dt
    s.h_sav += (S - I) * dt

    # нижний резервуар и насос: совокупный спрос на отечественное
    pump = K_SUMP * s.h_sump
    s.h_sump += (Cd + G + I + X - pump) * dt
    s.h_sump = max(0.0, s.h_sump)
    s.Y = K_SUMP * s.h_sump

    s.T, s.S, s.C, s.Cd, s.M = T, S, C, Cd, M
    s.G_out, s.I_out, s.X = G, I, X
    s.t_sec += dt


def snapshot(s: State, p: Params) -> dict:
    y_star, r_star = equilibrium(p)
    return {
        "hGov": s.h_gov, "hSav": s.h_sav, "hSump": s.h_sump, "hIdle": s.h_idle,
        "Y": s.Y, "r": s.r, "tSec": s.t_sec,
        "flows": {"T": s.T, "S": s.S, "C": s.C, "Cd": s.Cd,
                  "M": s.M, "Gout": s.G_out, "Iout": s.I_out, "X": s.X},
        "params": p.model_dump(),
        "multiplier": multiplier(p),
        "yStar": y_star,
        "rStar": r_star,
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
    state = initial_state(params)
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
                state = initial_state(params)
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