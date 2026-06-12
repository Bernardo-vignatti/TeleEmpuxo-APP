#!/usr/bin/env python3
"""
================================================================
  TELEMETRIA DE EMPUXO — Interface Unificada  v5.0
================================================================
  Interface Python única que substitui dashboard.html,
  layer_control.html e server.py em uma só janela.
  O overlay.html permanece para o OBS.

  Instalar dependências:
    pip install customtkinter matplotlib websockets aiohttp numpy

  Uso:
    python telemetria_app.py
    Overlay OBS: http://localhost:8080/overlay.html
================================================================
"""

import asyncio
import json
import csv
import time
import sys
import logging
import signal
import threading
import tkinter as tk
import customtkinter as ctk
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from datetime import datetime
from pathlib import Path
from enum import Enum
from typing import Set, Dict, Optional, List, Tuple
from collections import deque
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from aiohttp import web
import websockets

# ── Tema ─────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ── Configuração ──────────────────────────────────────────────────
ESP32_HOST      = "0.0.0.0"
ESP32_PORT      = 8765
CLIENT_PORT     = 8766
HTTP_PORT       = 8080
LOG_DIR         = Path("logs")
PLOT_DIR        = Path("plots")
BURN_THRESHOLD  = 2.0
BURN_END_DELAY  = 1.5
MAX_FORCE       = 120.0
GRAPH_POINTS    = 500

# ── Compensação de drift / creep ─────────────────────────────────
# O sistema aprende o drift em IDLE via regressão linear e aplica
# um offset de correção em software. A tara física (hard) só dispara
# se o drift acumulado ultrapassar DRIFT_HARD_TARE_N — situação em
# que a compensação por software já não é suficiente.
DRIFT_WINDOW_S       = 50.0   # janela de amostras IDLE para regressão (s)
DRIFT_MIN_SAMPLES    = 15     # mínimo de amostras para estimar tendência
DRIFT_APPLY_THRESH_N = 0.20   # offset mínimo para valer a pena aplicar (N)
DRIFT_RATE_WARN_NS   = 0.05   # taxa de drift que gera aviso no log (N/s)
DRIFT_HARD_TARE_N    = -0.5    # offset acumulado para forçar tara física

# Paleta de cores
C_BG     = "#0a0a10"
C_PANEL  = "#0e0e16"
C_PANEL2 = "#13131e"
C_BORDER = "#1e1e2e"
C_RED    = "#e53935"
C_RED2   = "#ff5252"
C_AMBER  = "#ffab00"
C_CYAN   = "#00e5ff"
C_GREEN  = "#00e676"
C_BLUE   = "#448aff"
C_TEXT   = "#c8c8d0"
C_DIM    = "#555570"

def _dim(hex_color: str, factor: float = 0.15) -> str:
    """Versão escurecida de uma cor hex — hover válido sem alpha de 8 dígitos."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    br, bg_, bb = 0x0a, 0x0a, 0x10
    r2 = int(r * factor + br * (1 - factor))
    g2 = int(g * factor + bg_ * (1 - factor))
    b2 = int(b * factor + bb * (1 - factor))
    return f"#{r2:02x}{g2:02x}{b2:02x}"

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("telemetria")


# ════════════════════════════════════════════════════════════════
#  MÁQUINA DE ESTADOS (mesmo do server.py original)
# ════════════════════════════════════════════════════════════════

class State(str, Enum):
    IDLE     = "idle"
    RUNNING  = "running"
    COMPLETE = "complete"

ALLOWED_TRANSITIONS: Set[Tuple] = {
    (State.IDLE,     State.RUNNING),
    (State.RUNNING,  State.COMPLETE),
    (State.COMPLETE, State.IDLE),
}

class TestSession:
    def __init__(self):
        self._state: State = State.IDLE
        self.reset_data()

    @property
    def state(self) -> State:
        return self._state

    def transition(self, new_state: State) -> bool:
        if (self._state, new_state) not in ALLOWED_TRANSITIONS:
            log.warning(f"[FSM] Transição NEGADA: {self._state} → {new_state}")
            return False
        log.info(f"[FSM] {self._state} → {new_state}")
        self._state = new_state
        return True

    def reset_data(self):
        self.burning       = False
        self.start_time: Optional[float]    = None
        self.burn_end_time: Optional[float] = None
        self.peak_force    = 0.0
        self.impulse       = 0.0
        self.last_t_epoch: Optional[float]  = None
        self.last_force    = 0.0
        self.samples: List[Tuple[float, float]] = []
        self.csv_file      = None
        self.csv_writer    = None
        self.session_name  = ""
        self.burn_duration = 0.0
        self._ema: Optional[float] = None
        self._ema_alpha    = 0.3

    def full_reset(self) -> bool:
        if self._state == State.RUNNING:
            return False
        self.reset_data()
        self._state = State.IDLE
        return True

    def filter(self, raw: float) -> float:
        if self._ema is None:
            self._ema = raw
        else:
            self._ema = self._ema_alpha * raw + (1 - self._ema_alpha) * self._ema
        return self._ema

    def open_log(self) -> str:
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_name = ts
        path = LOG_DIR / f"test_{ts}.csv"
        f = open(path, "w", newline="", encoding="utf-8")
        self.csv_file   = f
        self.csv_writer = csv.writer(f)
        self.csv_writer.writerow(["timestamp_ms", "elapsed_s", "force_N", "force_raw_N"])
        log.info(f"[LOG] Aberto: {path}")
        return str(path)

    def close_log(self):
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception as e:
                log.warning(f"[LOG] Erro: {e}")
            finally:
                self.csv_file   = None
                self.csv_writer = None

    def write_sample(self, t_ms, elapsed, force, raw):
        if self.csv_writer:
            try:
                self.csv_writer.writerow([t_ms, f"{elapsed:.4f}", f"{force:.4f}", f"{raw:.4f}"])
                self.csv_file.flush()
            except Exception as e:
                log.warning(f"[LOG] Erro ao escrever: {e}")

    def snapshot(self) -> Dict:
        return {
            "state":    self._state.value,
            "burning":  self.burning,
            "peak":     round(self.peak_force, 2),
            "impulse":  round(self.impulse, 4),
            "duration": round(self.burn_duration, 3),
            "session":  self.session_name,
            "samples":  len(self.samples),
        }


class MissionState(str, Enum):
    IDLE     = "mission_idle"
    COUNTING = "mission_counting"
    HOLD     = "mission_hold"
    SCRUBBED = "mission_scrubbed"

DEFAULT_GONOGO = [
    {"id": "esp32",  "label": "ESP32",            "auto": True,  "go": False},
    {"id": "camera", "label": "Câmera",            "auto": False, "go": False},
    {"id": "hx711",  "label": "HX711 / Balança",   "auto": False, "go": False},
    {"id": "area",   "label": "Área Livre",         "auto": False, "go": False},
    {"id": "clima",  "label": "Condição Climática", "auto": False, "go": False},
]

class MissionControl:
    def __init__(self):
        self.state: MissionState = MissionState.IDLE
        self.t_seconds: int      = 60
        self.seconds_left: int   = 60
        self.hold_reason: str    = ""
        self.scrub_reason: str   = ""
        self.countdown_task      = None
        self.t0_reached: bool    = False
        self.gonogo: list        = [dict(i) for i in DEFAULT_GONOGO]

    def all_go(self) -> bool:
        return all(item["go"] for item in self.gonogo)

    def reset(self):
        if self.countdown_task and not self.countdown_task.done():
            self.countdown_task.cancel()
        self.state        = MissionState.IDLE
        self.seconds_left = self.t_seconds
        self.hold_reason  = ""
        self.scrub_reason = ""
        self.t0_reached   = False
        self.countdown_task = None

    def snapshot(self) -> dict:
        return {
            "mission_state":   self.state.value,
            "mission_seconds": self.seconds_left,
            "mission_t":       self.t_seconds,
            "hold_reason":     self.hold_reason,
            "scrub_reason":    self.scrub_reason,
            "gonogo":          self.gonogo,
            "all_go":          self.all_go(),
            "t0_reached":      self.t0_reached,
        }


# ── Singletons globais ────────────────────────────────────────────
session            = TestSession()
mission            = MissionControl()
client_connections: Set = set()
esp_connected: bool     = False
esp_websocket           = None
esp_last_seen: float    = 0.0
esp_cooldown_until: float = 0.0
shutdown_event: Optional[asyncio.Event] = None
_start_time: float      = time.time()

ESP_HEARTBEAT_TIMEOUT = 5.0

# Fila de eventos para a GUI (thread-safe)
gui_queue: asyncio.Queue = None   # inicializado no start do loop


# ════════════════════════════════════════════════════════════════
#  GERADOR DE GRÁFICO PNG
# ════════════════════════════════════════════════════════════════

def generate_plot(session_name: str, samples: List[Tuple[float, float]]) -> str:
    PLOT_DIR.mkdir(exist_ok=True)
    if len(samples) < 5:
        return ""
    times  = np.array([s[0] for s in samples], dtype=float)
    forces = np.array([s[1] for s in samples], dtype=float)
    times  = times - times[0]
    peak   = float(np.max(forces))
    impulse = float(np.trapezoid(forces, times) if hasattr(np, 'trapezoid') else np.trapz(forces, times))
    above   = forces > BURN_THRESHOLD
    avg_f   = float(np.mean(forces[above])) if np.any(above) else 0.0
    burn_dur = float(times[above][-1] - times[above][0]) if np.any(above) else 0.0

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('#0d0d0f')
    ax = axes[0]
    ax.set_facecolor('#111114')
    ax.fill_between(times, forces, alpha=0.25, color='#e53935')
    ax.fill_between(times, forces, where=above, alpha=0.35, color='#ff6b35')
    ax.plot(times, forces, color='#ff4444', linewidth=2.2, solid_capstyle='round')
    ax.axhline(peak, color='#ffcc00', linewidth=0.8, linestyle='--', alpha=0.7)
    ax.text(times[-1] * 0.02, peak + max(peak, MAX_FORCE) * 0.02,
            f'Pico: {peak:.1f} N', color='#ffcc00', fontsize=9, fontweight='bold')
    ax.axhline(BURN_THRESHOLD, color='#555555', linewidth=0.6, linestyle=':', alpha=0.5)
    info = (f"Impulso Total: {impulse:.3f} N·s\nForça Média:   {avg_f:.2f} N\n"
            f"Força de Pico: {peak:.1f} N\nTempo Queima:  {burn_dur:.3f} s")
    ax.text(0.98, 0.97, info, transform=ax.transAxes, fontsize=10,
            verticalalignment='top', horizontalalignment='right',
            color='#cccccc', fontfamily='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1f', edgecolor='#333333', alpha=0.9))
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=-max(peak * 0.05, 1))
    ax.set_xlabel("Tempo (s)", color='#888', fontsize=10)
    ax.set_ylabel("Força (N)", color='#888', fontsize=10)
    ax.set_title(f"Curva de Empuxo — Sessão {session_name}", color='#ddd', fontsize=13, fontweight='bold', pad=12)
    ax.tick_params(colors='#666')
    ax.spines[:].set_color('#2a2a2f')
    ax.grid(True, color='#2a2a2f', linewidth=0.5)
    ax.grid(True, which='minor', color='#1d1d22', linewidth=0.3, linestyle=':')
    ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
    ax.xaxis.set_minor_locator(ticker.AutoMinorLocator())
    ax2 = axes[1]
    ax2.set_facecolor('#111114')
    if len(forces) > 2:
        dt = np.diff(times); dt[dt == 0] = 1e-6
        dF = np.diff(forces) / dt
        ax2.plot(times[1:], dF, color='#4fc3f7', linewidth=1.2, alpha=0.8)
        ax2.axhline(0, color='#444', linewidth=0.6)
        ax2.fill_between(times[1:], dF, alpha=0.15, color='#4fc3f7')
    ax2.set_ylabel("dF/dt (N/s)", color='#888', fontsize=8)
    ax2.set_xlabel("Tempo (s)", color='#888', fontsize=8)
    ax2.tick_params(colors='#666')
    ax2.spines[:].set_color('#2a2a2f')
    ax2.grid(True, color='#2a2a2f', linewidth=0.4)
    ax2.set_xlim(left=0)
    plt.tight_layout(pad=1.5)
    out = PLOT_DIR / f"plot_{session_name}.png"
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info(f"[PLOT] Salvo: {out}")
    return str(out)


# ════════════════════════════════════════════════════════════════
#  BACKEND ASSÍNCRONO (mesmo do server.py)
# ════════════════════════════════════════════════════════════════

async def broadcast(msg: Dict):
    global client_connections
    if not client_connections:
        return
    data = json.dumps(msg, ensure_ascii=False)
    dead: Set = set()
    for ws in list(client_connections):
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    if dead:
        client_connections -= dead

async def broadcast_state(extra: Optional[Dict] = None):
    global esp_connected
    msg: Dict = {
        "type": "state_update",
        **session.snapshot(),
        **mission.snapshot(),
        "esp": esp_connected
    }
    if extra:
        msg.update(extra)
    await broadcast(msg)
    # Notifica a GUI também
    if gui_queue:
        await gui_queue.put(msg)

async def send_esp_led(state: str):
    global esp_websocket, esp_cooldown_until
    ESP_COOLDOWN_DURATION = 16.0
    if state == "idle" and time.time() < esp_cooldown_until:
        return
    if state == "cooldown":
        esp_cooldown_until = time.time() + ESP_COOLDOWN_DURATION
    if esp_websocket is None:
        return
    try:
        await esp_websocket.send(json.dumps({"led": state}))
    except Exception as e:
        log.warning(f"[LED→ESP] Falha: {e}")

async def handle_esp32(websocket):
    global esp_connected, esp_last_seen, esp_websocket
    log.info(f"[ESP32] Conectado: {websocket.remote_address}")
    esp_connected  = True
    esp_websocket  = websocket
    esp_last_seen  = time.time()
    for item in mission.gonogo:
        if item["id"] == "esp32":
            item["go"] = True
    await broadcast_state()
    await broadcast({"type": "gonogo_update", **mission.snapshot()})
    asyncio.create_task(resync_led_on_connect())
    if gui_queue:
        await gui_queue.put({"type": "esp_connected", "connected": True})
    try:
        async for raw in websocket:
            esp_last_seen = time.time()
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if "hello" in d or "ping" in d:
                continue
            if "f" not in d:
                continue
            try:
                esp_raw = float(d["f"])
                raw_r   = float(d.get("r", esp_raw))
                t_ms    = int(d.get("t", time.time() * 1000))
            except (ValueError, TypeError):
                continue

            # Compensação de drift em software (regressão linear, não toca o ESP32)
            force = drift_comp.compensate(esp_raw)
            if abs(force) < 0.15:
                force = 0.0

            if (session.state == State.IDLE and mission.t0_reached and force >= BURN_THRESHOLD):
                if session.transition(State.RUNNING):
                    session.reset_data()
                    drift_comp.reset()   # modelo de drift zerado ao iniciar sessão
                    session.open_log()
                    mission.t0_reached = False
                    await send_esp_led("armed")
                    await broadcast_state({"event": "session_start", "session": session.session_name})
                    log.info("[MISSION] Auto-start por empuxo após T-0.")

            msg_data = {
                "type": "data",
                "f": round(force, 2),
                "r": round(raw_r, 2),
                "t": t_ms,
                **session.snapshot(),
                "esp": esp_connected,
            }

            if session.state != State.RUNNING:
                await broadcast(msg_data)
                if gui_queue:
                    await gui_queue.put(msg_data)
                continue

            now_epoch = time.time()
            if not session.burning and force >= BURN_THRESHOLD:
                session.burning       = True
                session.start_time    = now_epoch
                session.burn_end_time = None
                log.info(f"[QUEIMA] Detectada! F={force:.2f}N")
                await broadcast({"type": "burn_start", **session.snapshot()})
                await send_esp_led("burning")
                if gui_queue:
                    await gui_queue.put({"type": "burn_start"})

            elapsed = (now_epoch - session.start_time) if session.start_time else 0.0

            if session.burning and force < BURN_THRESHOLD:
                if session.burn_end_time is None:
                    session.burn_end_time = now_epoch
                elif (now_epoch - session.burn_end_time) >= BURN_END_DELAY:
                    session.burning       = False
                    session.burn_duration = session.burn_end_time - session.start_time
                    log.info(f"[QUEIMA] Finalizada. Duração={session.burn_duration:.3f}s — aguardando Parar manual.")
                    await broadcast({"type": "burn_end", **session.snapshot()})
                    await send_esp_led("cooldown")
                    if gui_queue:
                        await gui_queue.put({"type": "burn_end", **session.snapshot()})
                    # ── Sessão permanece em RUNNING até o operador clicar em Parar ──
            elif session.burning and force >= BURN_THRESHOLD:
                session.burn_end_time = None

            if session.last_t_epoch is not None and session.burning:
                dt = now_epoch - session.last_t_epoch
                if 0 < dt < 1.0:
                    session.impulse += 0.5 * (force + session.last_force) * dt

            session.last_t_epoch = now_epoch
            session.last_force   = force
            if force > session.peak_force:
                session.peak_force = force
            session.samples.append((now_epoch, force))
            session.write_sample(t_ms, elapsed, force, raw_r)

            msg_data = {
                "type": "data",
                "f": round(force, 2),
                "r": round(raw_r, 2),
                "t": t_ms,
                "elapsed": round(elapsed, 3),
                **session.snapshot(),
                "esp": esp_connected,
            }
            await broadcast(msg_data)
            if gui_queue:
                await gui_queue.put(msg_data)

    except Exception as e:
        log.warning(f"[ESP32] Conexão encerrada: {e}")
    finally:
        esp_connected = False
        esp_websocket = None
        for item in mission.gonogo:
            if item["id"] == "esp32":
                item["go"] = False
                break
        log.info("[ESP32] Desconectado.")
        await broadcast_state()
        await broadcast({"type": "gonogo_update", **mission.snapshot()})
        if gui_queue:
            await gui_queue.put({"type": "esp_connected", "connected": False})

async def handle_client(websocket):
    global client_connections
    client_connections.add(websocket)
    try:
        await websocket.send(json.dumps({
            "type": "state_update",
            **session.snapshot(),
            "esp": esp_connected,
        }))
    except Exception:
        pass
    try:
        async for raw in websocket:
            try:
                cmd = json.loads(raw)
                await handle_command(cmd)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        client_connections.discard(websocket)

async def mission_countdown_task():
    global mission
    log.info(f"[MISSION] Contagem T-{mission.t_seconds}s iniciada.")
    try:
        while True:
            # Emite tick imediato com valor atual (para UI mostrar o valor certo ao iniciar)
            if mission.state == MissionState.COUNTING:
                snap = mission.snapshot()
                await broadcast({"type": "mission_tick", **snap})
                if gui_queue:
                    await gui_queue.put({"type": "mission_tick", **snap})

            # Dorme exatamente 1 segundo real
            await asyncio.sleep(1.0)

            # HOLD: não decrementa, só fica esperando
            if mission.state == MissionState.HOLD:
                continue

            if mission.state != MissionState.COUNTING:
                return

            # Decrementa APÓS o sleep — 1 tick = 1 segundo real
            if mission.seconds_left <= 0:
                mission.state      = MissionState.IDLE
                mission.t0_reached = True
                log.info("[MISSION] T-0!")
                msg = {"type": "mission_t0", **mission.snapshot()}
                await broadcast(msg)
                if gui_queue:
                    await gui_queue.put(msg)
                return

            mission.seconds_left -= 1

    except asyncio.CancelledError:
        log.info("[MISSION] Countdown cancelado.")

async def handle_mission_command(action: str, cmd: dict):
    global mission, session
    if action == "mission_start":
        if mission.state not in (MissionState.IDLE, MissionState.SCRUBBED):
            return
        if not mission.all_go():
            not_go = [i["label"] for i in mission.gonogo if not i["go"]]
            msg = {"type": "mission_blocked", "reason": f"NO-GO: {', '.join(not_go)}", **mission.snapshot()}
            await broadcast(msg)
            if gui_queue:
                await gui_queue.put(msg)
            return
        t = cmd.get("t_seconds")
        if isinstance(t, int) and 5 <= t <= 3600:
            mission.t_seconds = t
        mission.reset()
        mission.state        = MissionState.COUNTING
        mission.seconds_left = mission.t_seconds
        mission.countdown_task = asyncio.create_task(mission_countdown_task())
        msg = {"type": "mission_start", **mission.snapshot()}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

    elif action == "mission_hold":
        if mission.state != MissionState.COUNTING:
            return
        mission.state       = MissionState.HOLD
        mission.hold_reason = cmd.get("reason", "Hold solicitado")
        msg = {"type": "mission_hold", **mission.snapshot()}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

    elif action == "mission_resume":
        if mission.state != MissionState.HOLD:
            return
        mission.state       = MissionState.COUNTING
        mission.hold_reason = ""
        msg = {"type": "mission_resume", **mission.snapshot()}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

    elif action == "mission_scrub":
        if mission.countdown_task and not mission.countdown_task.done():
            mission.countdown_task.cancel()
        mission.state        = MissionState.SCRUBBED
        mission.scrub_reason = cmd.get("reason", "Scrub solicitado")
        if session.state == State.RUNNING:
            asyncio.create_task(finalize_session("scrub"))
            await send_esp_led("idle")
        msg = {"type": "mission_scrub", **mission.snapshot(), **session.snapshot(), "esp": esp_connected}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

    elif action == "mission_reset":
        mission.reset()
        msg = {"type": "mission_reset", **mission.snapshot()}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

    elif action == "mission_set_t":
        t = cmd.get("t_seconds")
        if isinstance(t, int) and 5 <= t <= 3600:
            mission.t_seconds    = t
            if mission.state == MissionState.IDLE:
                mission.seconds_left = t
        msg = {"type": "mission_config", **mission.snapshot()}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

    elif action == "gonogo_set":
        item_id = cmd.get("id")
        go_val  = cmd.get("go")
        if item_id is None or go_val is None:
            return
        for item in mission.gonogo:
            if item["id"] == item_id and not item.get("auto"):
                item["go"] = bool(go_val)
                break
        msg = {"type": "gonogo_update", **mission.snapshot()}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)

async def handle_command(cmd: Dict):
    action = cmd.get("action", "")
    if not action:
        return
    log.info(f"[CMD] {action}")
    if action.startswith("mission_") or action == "gonogo_set":
        await handle_mission_command(action, cmd)
        return
    if action == "start":
        if session.transition(State.RUNNING):
            session.reset_data()
            drift_comp.reset()   # modelo de drift zerado ao iniciar sessão
            path = session.open_log()
            await send_esp_led("armed")
            msg = {"type": "state_update", "event": "session_start", **session.snapshot(), "esp": esp_connected}
            await broadcast_state({"event": "session_start"})
            if gui_queue:
                await gui_queue.put({"type": "state_update", "event": "session_start", **session.snapshot()})
    elif action == "stop":
        if session.state == State.RUNNING:
            asyncio.create_task(finalize_session("manual"))
    elif action == "reset":
        if session.full_reset():
            await send_esp_led("idle")
            await broadcast_state({"event": "reset"})
            if gui_queue:
                await gui_queue.put({"type": "state_update", "event": "reset", **session.snapshot()})
    elif action == "tare":
        if esp_websocket is not None:
            try:
                await esp_websocket.send(json.dumps({"tare": True}))
            except Exception as e:
                log.warning(f"[TARE] Falha: {e}")
        msg = {"type": "tare_ack", "esp_reached": esp_websocket is not None}
        await broadcast(msg)
        if gui_queue:
            await gui_queue.put(msg)
    elif action == "plot":
        if session.samples:
            loop = asyncio.get_running_loop()
            path = await loop.run_in_executor(None, generate_plot, session.session_name, list(session.samples))
            if path:
                msg = {"type": "plot_ready", "path": path, "name": Path(path).name}
                await broadcast(msg)
                if gui_queue:
                    await gui_queue.put(msg)
    elif action == "overlay_ctrl":
        target = cmd.get("target", "")
        show   = bool(cmd.get("show", True))
        if target:
            await broadcast({"type": "overlay_ctrl", "target": target, "show": show})
    elif action == "overlay_visibility":
        show = bool(cmd.get("show", True))
        await broadcast({"type": "overlay_visibility", "show": show})

async def finalize_session(reason: str = "manual"):
    if session.state != State.RUNNING:
        return
    await asyncio.sleep(0.3)
    if not session.transition(State.COMPLETE):
        return
    if reason == "manual":
        await send_esp_led("idle")
    session.close_log()
    samples_snap = list(session.samples)
    sname        = session.session_name
    loop         = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, generate_plot, sname, samples_snap)
    except Exception as e:
        log.error(f"[PLOT] Erro: {e}")
        path = ""
    plot_name = Path(path).name if path else ""
    msg = {
        "type": "state_update",
        "event": "session_end",
        "reason": reason,
        "plot": plot_name,
        "sample_count": len(samples_snap),
        **session.snapshot(),
        "esp": esp_connected,
    }
    await broadcast_state({
        "event": "session_end",
        "reason": reason,
        "plot": plot_name,
        "sample_count": len(samples_snap),
    })
    if gui_queue:
        await gui_queue.put(msg)

async def esp_heartbeat_monitor():
    global esp_connected, esp_websocket
    while True:
        await asyncio.sleep(1.0)
        if esp_connected and (time.time() - esp_last_seen) > ESP_HEARTBEAT_TIMEOUT:
            log.warning("[ESP32] Heartbeat timeout.")
            esp_connected = False
            esp_websocket = None
            await broadcast_state()
            if gui_queue:
                await gui_queue.put({"type": "esp_connected", "connected": False})

# ════════════════════════════════════════════════════════════════
#  COMPENSADOR DE DRIFT / CREEP
# ════════════════════════════════════════════════════════════════

class DriftCompensator:
    """
    Estima e compensa o drift da célula de carga em tempo real.

    Funcionamento:
      - Coleta amostras apenas em estado IDLE (sem queima).
      - Mantém uma janela deslizante de (timestamp, valor_bruto).
      - Aplica regressão linear simples (mínimos quadrados) para
        estimar a taxa de drift (N/s) e o offset atual.
      - O offset é subtraído de cada leitura antes de ser usada
        (compensação soft — ESP32 não é tocado).
      - Se o offset acumulado ultrapassar DRIFT_HARD_TARE_N, dispara
        uma tara física no ESP32 e zera o modelo.

    Métricas expostas:
      offset_n    — offset de compensação atual (N)
      rate_ns     — taxa de drift estimada (N/s)
      confidence  — R² da regressão (0=ruim, 1=perfeito)
      n_samples   — amostras na janela
    """

    def __init__(self):
        self._samples: List[Tuple[float, float]] = []  # (t, valor_bruto)
        self.offset_n:    float = 0.0
        self.rate_ns:     float = 0.0
        self.confidence:  float = 0.0
        self.n_samples:   int   = 0
        self._last_notify: float = 0.0

    def reset(self):
        """Zera o modelo (chamado após tara física ou início de sessão)."""
        self._samples.clear()
        self.offset_n   = 0.0
        self.rate_ns    = 0.0
        self.confidence = 0.0
        self.n_samples  = 0

    def add_sample(self, t: float, raw: float):
        """Registra amostra IDLE e recalcula o modelo."""
        self._samples.append((t, raw))
        # Remove amostras fora da janela
        cutoff = t - DRIFT_WINDOW_S
        self._samples = [(ts, v) for ts, v in self._samples if ts >= cutoff]
        self.n_samples = len(self._samples)
        if self.n_samples >= DRIFT_MIN_SAMPLES:
            self._fit()

    def _fit(self):
        """Regressão linear simples: força ~ a*t + b — O(n), sem numpy."""
        n  = self.n_samples
        t0 = self._samples[0][0]          # ancora o tempo para evitar float overflow
        xs = [s[0] - t0 for s in self._samples]
        ys = [s[1]      for s in self._samples]

        sum_x  = sum(xs)
        sum_y  = sum(ys)
        sum_xx = sum(x*x for x in xs)
        sum_xy = sum(x*y for x, y in zip(xs, ys))

        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-12:
            return  # dados constantes — sem tendência

        a = (n * sum_xy - sum_x * sum_y) / denom   # inclinação (N/s)
        b = (sum_y - a * sum_x) / n                 # intercepto

        # Offset atual = valor predito no último timestamp
        t_last = xs[-1]
        self.offset_n = a * t_last + b
        self.rate_ns  = a

        # R² (coeficiente de determinação)
        mean_y   = sum_y / n
        ss_tot   = sum((y - mean_y)**2 for y in ys)
        ss_res   = sum((y - (a*x + b))**2 for x, y in zip(xs, ys))
        self.confidence = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-12 else 0.0

    def compensate(self, raw: float) -> float:
        """Retorna o valor bruto menos o offset estimado."""
        if abs(self.offset_n) < DRIFT_APPLY_THRESH_N:
            return raw
        return raw - self.offset_n

    def snapshot(self) -> dict:
        return {
            "offset_n":   round(self.offset_n,   3),
            "rate_ns":    round(self.rate_ns,     4),
            "confidence": round(self.confidence,  3),
            "n_samples":  self.n_samples,
        }


# Instância global
drift_comp = DriftCompensator()


async def drift_monitor():
    """
    Task de background: alimenta o DriftCompensator com amostras IDLE,
    emite notificações na GUI e aciona tara física se necessário.
    """
    global esp_websocket

    while True:
        await asyncio.sleep(1.0)

        if not esp_connected or esp_websocket is None:
            drift_comp.reset()
            continue

        if session.state != State.IDLE:
            # Sessão ativa: pausa coleta mas mantém offset para pós-sessão
            continue

        now = time.time()
        raw = session.last_force
        drift_comp.add_sample(now, raw)

        snap = drift_comp.snapshot()
        offset = snap["offset_n"]
        rate   = snap["rate_ns"]
        conf   = snap["confidence"]
        n      = snap["n_samples"]

        if n < DRIFT_MIN_SAMPLES:
            continue

        # ── Notificações periódicas de taxa alta ──────────────────
        if abs(rate) >= DRIFT_RATE_WARN_NS and (now - drift_comp._last_notify) > 30.0:
            drift_comp._last_notify = now
            log.info(f"[DRIFT] Taxa: {rate:+.4f} N/s | Offset: {offset:+.3f} N | R²={conf:.2f}")
            if gui_queue:
                await gui_queue.put({
                    "type": "drift_update",
                    "rate_ns":    round(rate, 4),
                    "offset_n":   round(offset, 3),
                    "confidence": round(conf, 3),
                    "n_samples":  n,
                })

        # ── Hard tare: drift além da capacidade de compensação ────
        if abs(offset) >= DRIFT_HARD_TARE_N:
            log.warning(
                f"[DRIFT] Offset {offset:+.2f} N excede limite "
                f"({DRIFT_HARD_TARE_N} N) — aplicando tara física."
            )
            try:
                await esp_websocket.send(json.dumps({"tare": True}))
            except Exception as e:
                log.warning(f"[DRIFT] Falha na tara física: {e}")
            if gui_queue:
                await gui_queue.put({
                    "type": "drift_hard_tare",
                    "offset_n": round(offset, 2),
                })
            drift_comp.reset()

async def resync_led_on_connect():
    await asyncio.sleep(1.0)
    if session.state == State.RUNNING:
        await send_esp_led("burning" if session.burning else "armed")
    else:
        await send_esp_led("idle")

# ── HTTP ──────────────────────────────────────────────────────────
async def serve_file(request):
    fname = request.match_info.get("filename", "overlay.html")
    if ".." in fname or fname.startswith("/"):
        raise web.HTTPForbidden()
    for base in [Path("."), Path(__file__).parent]:
        fpath = base / fname
        if fpath.is_file():
            return web.FileResponse(fpath)
    raise web.HTTPNotFound()

async def serve_plot(request):
    fname = request.match_info["filename"]
    if ".." in fname:
        raise web.HTTPForbidden()
    fpath = PLOT_DIR / fname
    if fpath.is_file():
        return web.FileResponse(fpath)
    raise web.HTTPNotFound()

async def serve_log(request):
    fname = request.match_info["filename"]
    if ".." in fname:
        raise web.HTTPForbidden()
    fpath = LOG_DIR / fname
    if fpath.is_file():
        return web.FileResponse(fpath, headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    raise web.HTTPNotFound()

async def api_status(request):
    return web.json_response({**session.snapshot(), "esp": esp_connected, "clients": len(client_connections)})

async def api_health(request):
    return web.json_response({
        "status": "ok", "state": session.state.value,
        "esp": esp_connected, "clients": len(client_connections),
        "uptime": round(time.time() - _start_time, 1),
    })

async def run_backend(loop_queue: asyncio.Queue):
    global shutdown_event, gui_queue
    gui_queue = loop_queue

    esp_server = await websockets.serve(
        handle_esp32, ESP32_HOST, ESP32_PORT,
        ping_interval=None, ping_timeout=None,
        close_timeout=10, max_size=2**16,
    )
    client_server = await websockets.serve(
        handle_client, "0.0.0.0", CLIENT_PORT,
        ping_interval=20, ping_timeout=10,
    )
    app = web.Application()
    app.router.add_get("/",                 lambda r: web.HTTPFound("/overlay.html"))
    app.router.add_get("/{filename}",       serve_file)
    app.router.add_get("/plots/{filename}", serve_plot)
    app.router.add_get("/logs/{filename}",  serve_log)
    app.router.add_get("/api/status",       api_status)
    app.router.add_get("/api/health",       api_health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()

    asyncio.create_task(esp_heartbeat_monitor())
    asyncio.create_task(drift_monitor())

    shutdown_event = asyncio.Event()
    log.info("Backend iniciado.")
    try:
        await shutdown_event.wait()
    finally:
        esp_server.close()
        client_server.close()
        await runner.cleanup()


# ════════════════════════════════════════════════════════════════
#  INTERFACE GRÁFICA  (CustomTkinter)
# ════════════════════════════════════════════════════════════════

class TelemetriaApp(ctk.CTk):
    def __init__(self, async_loop: asyncio.AbstractEventLoop, msg_queue: asyncio.Queue):
        super().__init__()
        self.async_loop = async_loop
        self.msg_queue  = msg_queue

        # Estado da GUI
        self.graph_data: deque = deque(maxlen=GRAPH_POINTS)
        self.burning    = False
        self.esp_ok     = False
        self.sys_state  = "idle"
        self.mission_st = "mission_idle"
        self.hz_count   = 0
        self.last_hz_ts = time.time()
        self.hz_display = 0
        self.burn_dur   = 0.0
        self.session_name = ""
        self.ovl_state  = {"left": True, "center": True, "right": True, "info": True, "graph": True}
        self.gonogo_rows: Dict[str, dict] = {}

        self.title("TeleEmpuxo v5.0 — Telemetria de Empuxo")
        self.geometry("1280x780")
        self.configure(fg_color=C_BG)
        self.minsize(1100, 700)

        self._last_redraw: float = 0.0
        self._build_ui()

    # ── BUILD UI ─────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        self._build_topbar()
        self._build_main_area()
        self._build_sidebar()
        self._build_actionbar()

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0, height=46)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(5, weight=1)

        # Logo
        ctk.CTkLabel(bar, text="Tele", font=("Courier", 14, "bold"), text_color="#fff").pack(side="left", padx=(18, 0))
        ctk.CTkLabel(bar, text="Empuxo", font=("Courier", 14, "bold"), text_color=C_RED).pack(side="left")
        ctk.CTkLabel(bar, text=" v5.0", font=("Courier", 9), text_color=C_DIM).pack(side="left", padx=(0, 20))

        # Status pills
        self.dot_esp = self._pill(bar, "ESP32", "#333333")
        self.dot_ws  = self._pill(bar, "Servidor", C_CYAN)
        
        # Session ID
        self.lbl_session = ctk.CTkLabel(bar, text="SEM SESSÃO",
                                         font=("Courier", 10), text_color=C_DIM)
        self.lbl_session.pack(side="right", padx=18)

        # Overlay URL info
        ctk.CTkLabel(bar, text=f"OBS → http://localhost:{HTTP_PORT}/overlay.html",
                      font=("Courier", 9), text_color=C_DIM).pack(side="right", padx=12)

    def _pill(self, parent, label: str, color: str):
        f = ctk.CTkFrame(parent, fg_color=C_PANEL2, corner_radius=4)
        f.pack(side="left", padx=4, pady=8)
        dot = ctk.CTkLabel(f, text="●", font=("Arial", 10), text_color=color, width=16)
        dot.pack(side="left", padx=(8, 2))
        ctk.CTkLabel(f, text=label, font=("Courier", 9), text_color=C_DIM).pack(side="left", padx=(0, 8))
        return dot

    def _build_main_area(self):
        # Notebook (abas)
        self.tabview = ctk.CTkTabview(self, fg_color=C_PANEL, corner_radius=0,
                                       segmented_button_fg_color=C_PANEL2,
                                       segmented_button_selected_color=C_RED,
                                       segmented_button_selected_hover_color="#b71c1c",
                                       segmented_button_unselected_color=C_PANEL2,
                                       text_color=C_TEXT)
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=(0, 0))
        self.tabview.add("📈  Gráfico")
        self.tabview.add("🚀  Missão")
        self.tabview.add("✅  GO/NO-GO")
        self.tabview.add("🎬  Overlay OBS")

        self._build_tab_chart()
        self._build_tab_mission()
        self._build_tab_gonogo()
        self._build_tab_overlay()

    # ── ABA GRÁFICO ───────────────────────────────────────────────
    def _build_tab_chart(self):
        tab = self.tabview.tab("📈  Gráfico")
        tab.grid_rowconfigure(0, weight=0)
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Métricas
        mf = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        mf.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        for i in range(6):
            mf.grid_columnconfigure(i, weight=1)

        def metric(parent, col, label, unit, key):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.grid(row=0, column=col, padx=8, pady=8, sticky="ew")
            lbl = ctk.CTkLabel(f, text="0.00" if "." in unit else "0",
                                font=("Courier", 26, "bold"), text_color="#fff")
            lbl.pack()
            ctk.CTkLabel(f, text=label, font=("Courier", 8), text_color=C_DIM).pack()
            ctk.CTkLabel(f, text=unit,  font=("Courier", 8), text_color=C_DIM).pack()
            return lbl

        self.m_force   = metric(mf, 0, "FORÇA", "N", "force")
        self.m_peak    = metric(mf, 1, "PICO", "N", "peak")
        self.m_impulse = metric(mf, 2, "IMPULSO", "N·s", "impulse")
        self.m_dur     = metric(mf, 3, "QUEIMA", "s", "dur")
        self.m_hz      = metric(mf, 4, "TAXA", "Hz", "hz")

        # Badge estado
        self.state_badge = ctk.CTkLabel(mf, text="◉  IDLE",
                                         font=("Courier", 11, "bold"), text_color=C_DIM)
        self.state_badge.grid(row=0, column=5, padx=12)

        # Gráfico matplotlib
        chart_frame = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        chart_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        chart_frame.grid_rowconfigure(0, weight=1)
        chart_frame.grid_columnconfigure(0, weight=1)

        self.fig = Figure(figsize=(8, 4), dpi=100, facecolor=C_PANEL2)
        self.ax  = self.fig.add_subplot(111)
        self._setup_chart_ax()

        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.canvas.draw()

    def _setup_chart_ax(self):
        ax = self.ax
        ax.set_facecolor("#0d0d14")
        ax.tick_params(colors="#555", labelsize=7)
        for sp in ax.spines.values():
            sp.set_color("#1e1e2e")
        ax.grid(True, color="#1a1a28", linewidth=0.5)
        ax.set_xlabel("Amostras", color="#555", fontsize=8)
        ax.set_ylabel("Força (N)", color="#555", fontsize=8)
        self.fig.tight_layout(pad=1.2)

    def _redraw_chart(self):
        if not self.graph_data:
            return
        data = list(self.graph_data)
        x    = range(len(data))
        self.ax.cla()
        self._setup_chart_ax()
        color = C_RED2 if self.burning else C_RED
        self.ax.fill_between(x, data, alpha=0.2, color=color)
        self.ax.plot(x, data, color=color, linewidth=1.8)
        if data:
            self.ax.axhline(BURN_THRESHOLD, color=C_AMBER, linewidth=0.7, linestyle=":", alpha=0.5)
        self.canvas.draw_idle()

    # ── ABA MISSÃO ────────────────────────────────────────────────
    def _build_tab_mission(self):
        tab = self.tabview.tab("🚀  Missão")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)

        # Coluna esquerda: countdown
        left = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        left.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(left, text="CONTAGEM REGRESSIVA", font=("Courier", 9),
                      text_color=C_DIM).pack(pady=(16, 4))

        self.tminus_val = ctk.CTkLabel(left, text="T–  --",
                                        font=("Courier New", 52, "bold"), text_color="#fff")
        self.tminus_val.pack(pady=4)

        self.tminus_status = ctk.CTkLabel(left, text="aguardando início",
                                           font=("Courier", 10), text_color=C_DIM)
        self.tminus_status.pack(pady=(0, 16))

        # Config T
        cfg = ctk.CTkFrame(left, fg_color="transparent")
        cfg.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(cfg, text="T-minus (s):", font=("Courier", 10), text_color=C_DIM).pack(side="left")
        self.t_input = ctk.CTkEntry(cfg, width=80, font=("Courier", 14),
                                     fg_color=C_PANEL, border_color=C_BORDER)
        self.t_input.insert(0, "60")
        self.t_input.pack(side="left", padx=8)
        ctk.CTkButton(cfg, text="Definir", width=70, font=("Courier", 10),
                       fg_color=C_BORDER, hover_color=C_DIM, text_color=C_TEXT,
                       command=self._set_t).pack(side="left")

        sep = ctk.CTkFrame(left, fg_color=C_BORDER, height=1)
        sep.pack(fill="x", padx=12, pady=12)

        # Botões missão
        btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        btn_frame.pack(fill="x", padx=12, pady=4)

        self.btn_m_start  = self._mbtn(btn_frame, "▶  Iniciar",  C_CYAN,  self._mission_start)
        self.btn_m_hold   = self._mbtn(btn_frame, "⏸  Hold",     C_AMBER, self._mission_hold)
        self.btn_m_resume = self._mbtn(btn_frame, "▶  Resume",   C_GREEN, self._mission_resume)
        self.btn_m_scrub  = self._mbtn(btn_frame, "✕  Scrub",    C_RED2,  self._mission_scrub)
        self.btn_m_reset  = self._mbtn(btn_frame, "↺  Reset",    C_DIM,   self._mission_reset)

        self.btn_m_start.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.btn_m_hold.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self.btn_m_resume.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        self.btn_m_scrub.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        self.btn_m_reset.grid(row=2, column=0, columnspan=2, padx=4, pady=4, sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        self.btn_m_resume.configure(state="disabled")
        self.btn_m_reset.configure(state="disabled")

        # Coluna direita: log missão
        right = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(right, text="LOG DE MISSÃO", font=("Courier", 9),
                      text_color=C_DIM).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 6))

        self.mission_log = ctk.CTkTextbox(right, font=("Courier New", 10),
                                           fg_color=C_PANEL, text_color=C_TEXT,
                                           wrap="word", state="disabled")
        self.mission_log.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    def _mbtn(self, parent, text, color, cmd):
        return ctk.CTkButton(parent, text=text, font=("Courier", 10, "bold"),
                              fg_color="transparent", border_color=color,
                              hover_color=_dim(color, 0.25),
                              text_color=color, border_width=1,
                              command=cmd)

    # ── ABA GO/NO-GO ─────────────────────────────────────────────
    def _build_tab_gonogo(self):
        tab = self.tabview.tab("✅  GO/NO-GO")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # Resumo
        sum_f = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        sum_f.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        ctk.CTkLabel(sum_f, text="STATUS GERAL:", font=("Courier", 10), text_color=C_DIM).pack(side="left", padx=16, pady=10)
        self.gonogo_summary = ctk.CTkLabel(sum_f, text="NO-GO", font=("Courier", 14, "bold"), text_color=C_RED2)
        self.gonogo_summary.pack(side="left", padx=8)

        # Grid GO/NO-GO
        grid_f = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        grid_f.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.gonogo_frame = grid_f
        self._build_gonogo_rows()

    def _build_gonogo_rows(self):
        for widget in self.gonogo_frame.winfo_children():
            widget.destroy()
        self.gonogo_rows = {}
        for i, item in enumerate(mission.gonogo):
            row = ctk.CTkFrame(self.gonogo_frame, fg_color=C_PANEL if i % 2 == 0 else C_PANEL2, corner_radius=4)
            row.pack(fill="x", padx=10, pady=3)
            row.grid_columnconfigure(1, weight=1)

            dot = ctk.CTkLabel(row, text="●", font=("Arial", 12),
                                text_color=C_GREEN if item["go"] else C_RED2, width=24)
            dot.grid(row=0, column=0, padx=(10, 4), pady=8)

            label_text = item["label"]
            if item.get("auto"):
                label_text += "  [auto]"
            ctk.CTkLabel(row, text=label_text, font=("Courier", 12, "bold"),
                          text_color=C_TEXT).grid(row=0, column=1, sticky="w", padx=4)

            if not item.get("auto"):
                btn_go = ctk.CTkButton(row, text="GO", width=60, font=("Courier", 10, "bold"),
                                        fg_color=C_GREEN if item["go"] else "transparent",
                                        hover_color="#00b34a", text_color="#000" if item["go"] else C_GREEN,
                                        border_color=C_GREEN, border_width=1,
                                        command=lambda iid=item["id"]: self._set_gonogo(iid, True))
                btn_go.grid(row=0, column=2, padx=4)

                btn_nogo = ctk.CTkButton(row, text="NO-GO", width=70, font=("Courier", 10, "bold"),
                                          fg_color=C_RED2 if not item["go"] else "transparent",
                                          hover_color="#b71c1c", text_color="#fff",
                                          border_color=C_RED2, border_width=1,
                                          command=lambda iid=item["id"]: self._set_gonogo(iid, False))
                btn_nogo.grid(row=0, column=3, padx=(0, 10))

                self.gonogo_rows[item["id"]] = {"dot": dot, "go_btn": btn_go, "nogo_btn": btn_nogo}
            else:
                self.gonogo_rows[item["id"]] = {"dot": dot}

    # ── ABA OVERLAY OBS ───────────────────────────────────────────
    def _build_tab_overlay(self):
        tab = self.tabview.tab("🎬  Overlay OBS")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # ── Seletor de overlay ────────────────────────────────────
        sel_f = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        sel_f.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        ctk.CTkLabel(sel_f, text="OVERLAY ATIVO:", font=("Courier", 9, "bold"),
                      text_color=C_DIM).pack(side="left", padx=16, pady=10)

        self._overlay_mode = "full"  # "full" ou "mini"

        self._btn_ovl_full = ctk.CTkButton(
            sel_f, text="◉  Full (ao vivo)", width=160, font=("Courier", 11, "bold"),
            fg_color=C_CYAN, hover_color="#1ab3cc", text_color="#000",
            command=self._select_overlay_full)
        self._btn_ovl_full.pack(side="left", padx=8, pady=8)

        self._btn_ovl_mini = ctk.CTkButton(
            sel_f, text="○  Mini (teste)", width=160, font=("Courier", 11, "bold"),
            fg_color="transparent", border_color=C_CYAN, border_width=1, text_color=C_CYAN,
            command=self._select_overlay_mini)
        self._btn_ovl_mini.pack(side="left", padx=4, pady=8)

        # URL dinâmica
        url_f = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        url_f.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        ctk.CTkLabel(url_f, text="URL para o OBS →", font=("Courier", 10),
                      text_color=C_DIM).pack(side="left", padx=16, pady=10)
        self._url_label = ctk.CTkLabel(
            url_f, text=f"http://localhost:{HTTP_PORT}/overlay.html",
            font=("Courier New", 12, "bold"), text_color=C_CYAN)
        self._url_label.pack(side="left", padx=8)
        ctk.CTkButton(url_f, text="Copiar", width=70, font=("Courier", 9),
                       fg_color=C_BORDER, hover_color=C_DIM, text_color=C_TEXT,
                       command=self._copy_active_overlay_url).pack(side="right", padx=12)

        # Controles de visibilidade (apenas para overlay full)
        self._vis_frame = ctk.CTkFrame(tab, fg_color=C_PANEL2, corner_radius=6)
        self._vis_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))

        ctk.CTkLabel(self._vis_frame, text="VISIBILIDADE — OVERLAY FULL",
                      font=("Courier", 9), text_color=C_DIM).pack(anchor="w", padx=14, pady=(12, 4))

        glob_row = ctk.CTkFrame(self._vis_frame, fg_color="transparent")
        glob_row.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkButton(glob_row, text="◈ Exibir Tudo", width=130, font=("Courier", 10),
                       fg_color="transparent", border_color=C_CYAN, border_width=1, text_color=C_CYAN,
                       command=lambda: self._send_cmd("overlay_visibility", {"show": True})).pack(side="left", padx=(0, 6))
        ctk.CTkButton(glob_row, text="✕ Ocultar Tudo", width=130, font=("Courier", 10),
                       fg_color="transparent", border_color=C_RED2, border_width=1, text_color=C_RED2,
                       command=lambda: self._send_cmd("overlay_visibility", {"show": False})).pack(side="left")

        sections = [
            ("left",   "Painel Esquerdo (Pico)"),
            ("center", "Centro (Força Principal)"),
            ("right",  "Painel Direito (Tempo)"),
            ("info",   "Barra de Informações"),
            ("graph",  "Mini Gráfico"),
        ]
        for i, (sid, name) in enumerate(sections):
            row = ctk.CTkFrame(self._vis_frame, fg_color=C_PANEL if i % 2 == 0 else C_PANEL2, corner_radius=4)
            row.pack(fill="x", padx=10, pady=3)
            ctk.CTkLabel(row, text=name, font=("Courier", 12), text_color=C_TEXT).pack(side="left", padx=14, pady=8)
            ctk.CTkButton(row, text="Mostrar", width=80, font=("Courier", 9),
                           fg_color="transparent", border_color=C_CYAN, border_width=1, text_color=C_CYAN,
                           command=lambda s=sid: self._send_cmd("overlay_ctrl", {"target": s, "show": True})
                           ).pack(side="right", padx=4, pady=6)
            ctk.CTkButton(row, text="Ocultar", width=80, font=("Courier", 9),
                           fg_color="transparent", border_color=C_RED2, border_width=1, text_color=C_RED2,
                           command=lambda s=sid: self._send_cmd("overlay_ctrl", {"target": s, "show": False})
                           ).pack(side="right", padx=4)

    def _select_overlay_full(self):
        self._overlay_mode = "full"
        self._btn_ovl_full.configure(fg_color=C_CYAN, text_color="#000", text="◉  Full (ao vivo)")
        self._btn_ovl_mini.configure(fg_color="transparent", text_color=C_CYAN, text="○  Mini (teste)")
        url = f"http://localhost:{HTTP_PORT}/overlay.html"
        self._url_label.configure(text=url)
        # Habilita recursivamente os widgets da área de visibilidade que aceitam 'state'
        self._set_vis_children_state("normal")
        self._log_event("Overlay: Full (ao vivo) selecionada.", "info")

    def _select_overlay_mini(self):
        self._overlay_mode = "mini"
        self._btn_ovl_mini.configure(fg_color=C_CYAN, text_color="#000", text="◉  Mini (teste)")
        self._btn_ovl_full.configure(fg_color="transparent", text_color=C_CYAN, text="○  Full (ao vivo)")
        url = f"http://localhost:{HTTP_PORT}/overlay_mini.html"
        self._url_label.configure(text=url)
        # Desabilita controles de visibilidade para o modo mini (se suportarem 'state')
        self._set_vis_children_state("disabled")
        self._log_event("Overlay: Mini (teste) selecionada.", "info")

    def _copy_active_overlay_url(self):
        if self._overlay_mode == "mini":
            url = f"http://localhost:{HTTP_PORT}/overlay_mini.html"
        else:
            url = f"http://localhost:{HTTP_PORT}/overlay.html"
        self._copy_url(url)

    # ── SIDEBAR ───────────────────────────────────────────────────
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0, width=230)
        sb.grid(row=1, column=1, sticky="nsew")
        sb.grid_rowconfigure(3, weight=1)
        sb.pack_propagate(False)

        # Indicador de força grande
        ctk.CTkLabel(sb, text="FORÇA ATUAL", font=("Courier", 8), text_color=C_DIM).pack(pady=(14, 2))
        self.big_force = ctk.CTkLabel(sb, text="0.00", font=("Courier New", 40, "bold"), text_color="#fff")
        self.big_force.pack()
        ctk.CTkLabel(sb, text="N", font=("Courier", 12), text_color=C_DIM).pack()

        sep1 = ctk.CTkFrame(sb, fg_color=C_BORDER, height=1)
        sep1.pack(fill="x", padx=10, pady=10)

        # Status ESP
        esp_f = ctk.CTkFrame(sb, fg_color=C_PANEL2, corner_radius=4)
        esp_f.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(esp_f, text="ESP32", font=("Courier", 8), text_color=C_DIM).pack(side="left", padx=10, pady=6)
        self.esp_status = ctk.CTkLabel(esp_f, text="●  Desconectado",
                                        font=("Courier", 10, "bold"), text_color=C_DIM)
        self.esp_status.pack(side="right", padx=10)

        sep2 = ctk.CTkFrame(sb, fg_color=C_BORDER, height=1)
        sep2.pack(fill="x", padx=10, pady=8)

        # Log eventos
        ctk.CTkLabel(sb, text="EVENTOS", font=("Courier", 8), text_color=C_DIM).pack(pady=(0, 4))
        self.log_box = ctk.CTkTextbox(sb, font=("Courier New", 8), fg_color=C_PANEL2,
                                       text_color=C_TEXT, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # ── ACTION BAR ───────────────────────────────────────────────
    def _build_actionbar(self):
        bar = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0, height=52)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")

        def abtn(text, color, cmd, **kw):
            return ctk.CTkButton(bar, text=text, font=("Courier", 11, "bold"),
                                  fg_color="transparent", hover_color=_dim(color, 0.20),
                                  text_color=color, border_color=color, border_width=1,
                                  height=36, command=cmd, **kw)

        self.btn_start = abtn("▶  Iniciar",  C_GREEN, self._start_session)
        self.btn_stop  = abtn("■  Parar",    C_RED2,  self._stop_session)
        self.btn_tare  = abtn("⊙  Tara",     C_AMBER, lambda: self._send_cmd("tare"))
        self.btn_plot  = abtn("📊 Gráfico",  C_BLUE,  lambda: self._send_cmd("plot"))
        self.btn_reset = abtn("↺  Reset",    C_DIM,   self._reset_session)

        for b in [self.btn_start, self.btn_stop, self.btn_tare, self.btn_plot, self.btn_reset]:
            b.pack(side="left", padx=6, pady=8)

        self.btn_stop.configure(state="disabled")
        self.btn_plot.configure(state="disabled")

        # Estado badge
        self.action_state = ctk.CTkLabel(bar, text="● IDLE", font=("Courier", 11, "bold"), text_color=C_DIM)
        self.action_state.pack(side="right", padx=20)

    # ── HELPER UI ────────────────────────────────────────────────
    def _copy_url(self, url: str):
        self.clipboard_clear()
        self.clipboard_append(url)
        self._log_event("URL copiada para o clipboard.", "info")

    def _log_event(self, text: str, level: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        colors = {"ok": C_GREEN, "warn": C_AMBER, "err": C_RED2, "info": C_CYAN}
        color  = colors.get(level, C_TEXT)

        for box in [self.log_box, self.mission_log]:
            box.configure(state="normal")
            box.insert("end", f"[{ts}] {text}\n")
            box.see("end")
            box.configure(state="disabled")

    def _set_vis_children_state(self, state: str):
        """Habilita/desabilita recursivamente widgets na área de visibilidade.
        Muitos widgets (CTkFrame) têm método configure(), mas não aceitam
        o argumento 'state' — por isso usamos try/except e recursão.
        """
        def _recurse(parent):
            for child in parent.winfo_children():
                try:
                    child.configure(state=state)
                except Exception:
                    # widget não suporta 'state' — ignora
                    pass
                # recursão: protege caso o widget não exponha winfo_children()
                try:
                    if child.winfo_children():
                        _recurse(child)
                except Exception:
                    pass
        try:
            _recurse(self._vis_frame)
        except Exception:
            pass

    def _update_state_badge(self):
        if self.burning and self.sys_state == "running":
            text, color = "● QUEIMA", C_RED2
        elif self.sys_state == "running":
            text, color = "● RUNNING", C_CYAN
        elif self.sys_state == "complete":
            text, color = "● CONCLUÍDO", C_GREEN
        else:
            text, color = "● IDLE", C_DIM
        self.action_state.configure(text=text, text_color=color)
        self.state_badge.configure(text=text, text_color=color)

    def _sync_buttons(self):
        is_idle     = self.sys_state == "idle"
        is_running  = self.sys_state == "running"
        is_complete = self.sys_state == "complete"

        self.btn_start.configure(state="normal" if is_idle else "disabled")
        self.btn_stop.configure(state="normal" if is_running else "disabled")
        self.btn_plot.configure(state="normal" if (is_complete and self.graph_data) else "disabled")
        self.btn_reset.configure(state="normal" if not is_running else "disabled")

    def _update_gonogo_ui(self):
        for item in mission.gonogo:
            row_w = self.gonogo_rows.get(item["id"])
            if not row_w:
                continue
            color = C_GREEN if item["go"] else C_RED2
            row_w["dot"].configure(text_color=color)
            if "go_btn" in row_w:
                row_w["go_btn"].configure(
                    fg_color=C_GREEN if item["go"] else "transparent",
                    text_color="#000" if item["go"] else C_GREEN)
                row_w["nogo_btn"].configure(
                    fg_color=C_RED2 if not item["go"] else "transparent")

        all_go = mission.all_go()
        self.gonogo_summary.configure(
            text="ALL GO ✓" if all_go else "NO-GO",
            text_color=C_GREEN if all_go else C_RED2)

    def _update_mission_display(self):
        ms = mission.seconds_left
        st = mission.state

        if st == MissionState.COUNTING:
            m   = ms // 60
            sec = str(ms % 60).zfill(2)
            txt = f"T–  {m}:{sec}" if m > 0 else f"T–  {ms:02d}"
            col = C_RED2 if ms <= 10 else "#fff"
            self.tminus_val.configure(text=txt, text_color=col)
            self.tminus_status.configure(text="contagem ativa", text_color=C_CYAN)
        elif st == MissionState.HOLD:
            self.tminus_val.configure(text="HOLD", text_color=C_AMBER)
            self.tminus_status.configure(text=mission.hold_reason or "contagem pausada", text_color=C_AMBER)
        elif st == MissionState.SCRUBBED:
            self.tminus_val.configure(text="SCRUB", text_color=C_RED2)
            self.tminus_status.configure(text=mission.scrub_reason or "teste cancelado", text_color=C_RED2)
        else:
            self.tminus_val.configure(text="T–  --", text_color=C_DIM)
            self.tminus_status.configure(text="aguardando início", text_color=C_DIM)

        # Sync botões missão
        is_idle     = st == MissionState.IDLE
        is_counting = st == MissionState.COUNTING
        is_hold     = st == MissionState.HOLD
        is_scrubbed = st == MissionState.SCRUBBED

        self.btn_m_start.configure(state="normal" if (is_idle or is_scrubbed) else "disabled")
        self.btn_m_hold.configure(state="normal" if is_counting else "disabled")
        self.btn_m_resume.configure(state="normal" if is_hold else "disabled")
        self.btn_m_scrub.configure(state="normal" if (is_counting or is_hold) else "disabled")
        self.btn_m_reset.configure(state="normal" if is_scrubbed else "disabled")

    def _handle_gui_msg(self, msg: dict):
        t = msg.get("type", "")

        if t == "esp_connected":
            ok = msg.get("connected", False)
            self.esp_ok = ok
            self.esp_status.configure(
                text=f"●  {'Conectado' if ok else 'Desconectado'}",
                text_color=C_GREEN if ok else C_DIM)
            self.dot_esp.configure(text_color=C_GREEN if ok else "#333333")
            self._log_event(f"ESP32 {'conectado' if ok else 'desconectado'}.", "ok" if ok else "warn")
            return

        if t == "data":
            force   = float(msg.get("f", 0))
            elapsed = float(msg.get("elapsed", 0))
            peak    = float(msg.get("peak", 0))
            impulse = float(msg.get("impulse", 0))

            self.graph_data.append(force)
            self.big_force.configure(text=f"{force:.2f}",
                                      text_color=C_RED2 if self.burning else "#fff")
            self.m_force.configure(text=f"{force:.2f}")
            self.m_peak.configure(text=f"{peak:.1f}")
            self.m_impulse.configure(text=f"{impulse:.3f}")

            if msg.get("burning"):
                self.burn_dur = elapsed
                self.m_dur.configure(text=f"{elapsed:.2f}")

            self.hz_count += 1
            now = time.time()
            if now - self.last_hz_ts >= 1.0:
                self.hz_display = self.hz_count
                self.m_hz.configure(text=str(self.hz_display))
                self.hz_count   = 0
                self.last_hz_ts = now

            # Redraw chart limitado a ~10 fps para não sobrecarregar a UI
            now = time.time()
            if now - self._last_redraw >= 0.10:
                self._last_redraw = now
                self._redraw_chart()
            return

        if t == "burn_start":
            self.burning = True
            self.big_force.configure(text_color=C_RED2)
            self._update_state_badge()
            self._log_event("⚡ Queima detectada!", "warn")
            return

        if t == "burn_end":
            self.burning = False
            self.big_force.configure(text_color="#fff")
            self._update_state_badge()
            dur = float(msg.get("duration", 0))
            pk  = float(msg.get("peak", 0))
            self._log_event(f"Queima finalizada — {dur:.3f}s · Pico: {pk:.1f}N", "info")
            return

        if t == "state_update":
            if "state" in msg:
                self.sys_state = msg["state"]
            if "esp" in msg:
                ok = bool(msg["esp"])
                self.esp_ok = ok
                self.dot_esp.configure(text_color=C_GREEN if ok else "#333333")

            ev = msg.get("event", "")
            if ev == "session_start":
                self.graph_data.clear()
                self.burning = False
                self.session_name = msg.get("session", "")
                self.lbl_session.configure(text=self.session_name or "EM ANDAMENTO")
                self._log_event(f"Sessão iniciada: {self.session_name}", "ok")
            elif ev == "session_end":
                pk  = float(msg.get("peak", 0))
                imp = float(msg.get("impulse", 0))
                dur = float(msg.get("duration", 0))
                self._log_event(f"✓ Concluído — Pico: {pk}N · Impulso: {imp}N·s · Dur: {dur}s", "ok")
                if msg.get("plot"):
                    self._log_event(f"Gráfico: {msg['plot']}", "info")
                self._redraw_chart()
            elif ev == "reset":
                self.graph_data.clear()
                self.burning = False
                self.m_force.configure(text="0.00")
                self.m_peak.configure(text="0")
                self.m_impulse.configure(text="0.000")
                self.m_dur.configure(text="—")
                self.big_force.configure(text="0.00", text_color="#fff")
                self.lbl_session.configure(text="SEM SESSÃO")
                self._redraw_chart()
                self._log_event("Sistema resetado.", "info")

            if "mission_state" in msg:
                self._apply_mission_state(msg)
            self._update_state_badge()
            self._sync_buttons()
            return

        if t in ("mission_start", "mission_tick", "mission_hold", "mission_resume",
                 "mission_scrub", "mission_reset", "mission_t0", "mission_config"):
            self._apply_mission_state(msg)
            if t == "mission_start":
                self._log_event(f"▶ Contagem iniciada — T-{msg.get('mission_t', '?')}s", "ok")
            elif t == "mission_hold":
                self._log_event(f"⏸ HOLD — {msg.get('hold_reason', '')}", "warn")
            elif t == "mission_resume":
                self._log_event("▶ Contagem retomada.", "ok")
            elif t == "mission_scrub":
                self._log_event(f"✕ SCRUB — {msg.get('scrub_reason', '')}", "err")
            elif t == "mission_t0":
                self._log_event("⬥ T-0 atingido!", "warn")
            elif t == "mission_reset":
                self._log_event("Missão resetada.", "info")
            return

        if t == "gonogo_update":
            if "gonogo" in msg:
                mission.gonogo = msg["gonogo"]
            self._update_gonogo_ui()
            return

        if t == "mission_blocked":
            self._log_event(f"⛔ {msg.get('reason', 'Bloqueado')}", "err")
            return

        if t == "plot_ready":
            self._log_event(f"Gráfico salvo: {msg.get('name', msg.get('path', ''))}", "ok")
            return

        if t == "tare_ack":
            reached = msg.get("esp_reached", False)
            self._log_event("Tara aplicada." if reached else "Tara: ESP32 não conectado.", "info" if reached else "warn")
            return

        if t == "drift_update":
            rate = msg.get("rate_ns", 0)
            off  = msg.get("offset_n", 0)
            conf = msg.get("confidence", 0)
            n    = msg.get("n_samples", 0)
            self._log_event(
                f"〜 Drift: {off:+.3f} N  |  {rate:+.4f} N/s  |  R²={conf:.2f}  ({n} amostras)",
                "info"
            )
            return

        if t == "drift_hard_tare":
            off = msg.get("offset_n", 0)
            self._log_event(
                f"⚠ Tara física aplicada — offset {off:+.2f} N excedeu limite.",
                "warn"
            )
            return

    def _apply_mission_state(self, msg):
        if "mission_state" in msg:
            mission.state = MissionState(msg["mission_state"])
        if "mission_seconds" in msg:
            mission.seconds_left = msg["mission_seconds"]
        if "hold_reason" in msg:
            mission.hold_reason = msg["hold_reason"]
        if "scrub_reason" in msg:
            mission.scrub_reason = msg["scrub_reason"]
        if "gonogo" in msg:
            mission.gonogo = msg["gonogo"]
            self._update_gonogo_ui()
        self._update_mission_display()

    # ── ACTIONS ──────────────────────────────────────────────────
    def _send_cmd(self, action: str, extra: dict = None):
        cmd = {"action": action}
        if extra:
            cmd.update(extra)
        asyncio.run_coroutine_threadsafe(handle_command(cmd), self.async_loop)

    def _start_session(self):
        self._send_cmd("start")

    def _stop_session(self):
        self._send_cmd("stop")

    def _reset_session(self):
        if self.sys_state == "running":
            self._log_event("Pare o teste antes de resetar.", "warn")
            return
        self._send_cmd("reset")

    def _set_t(self):
        try:
            t = int(self.t_input.get())
            if 5 <= t <= 3600:
                self._send_cmd("mission_set_t", {"t_seconds": t})
            else:
                self._log_event("T deve ser entre 5 e 3600s", "err")
        except ValueError:
            self._log_event("Valor inválido para T", "err")

    def _mission_start(self):
        try:
            t = int(self.t_input.get())
        except ValueError:
            t = 60
        self._send_cmd("mission_start", {"t_seconds": t})

    def _mission_hold(self):
        self._send_cmd("mission_hold", {"reason": "Hold manual pelo operador"})

    def _mission_resume(self):
        self._send_cmd("mission_resume")

    def _mission_scrub(self):
        self._send_cmd("mission_scrub", {"reason": "Scrub manual pelo operador"})

    def _mission_reset(self):
        self._send_cmd("mission_reset")

    def _set_gonogo(self, item_id: str, go: bool):
        self._send_cmd("gonogo_set", {"id": item_id, "go": go})


# ════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════

def main():
    import queue as _queue

    log.info("=" * 58)
    log.info("  TELEMETRIA DE EMPUXO — App Unificado  v5.0")
    log.info("=" * 58)
    log.info(f"  WebSocket ESP32   : ws://0.0.0.0:{ESP32_PORT}")
    log.info(f"  WebSocket Clientes: ws://0.0.0.0:{CLIENT_PORT}")
    log.info(f"  Overlay OBS       : http://localhost:{HTTP_PORT}/overlay.html")
    log.info(f"  API Health        : http://localhost:{HTTP_PORT}/api/health")
    log.info("=" * 58)

    # Fila thread-safe: backend (asyncio) → GUI (tkinter)
    sync_queue: _queue.SimpleQueue = _queue.SimpleQueue()

    class BridgeQueue:
        """Permite que o backend (async) envie msgs para a GUI (sync) sem bloquear."""
        async def put(self, item):
            sync_queue.put_nowait(item)
        def get_nowait(self):
            return sync_queue.get_nowait()

    bridge = BridgeQueue()

    # Loop asyncio em thread dedicada (backend roda aqui)
    async_loop = asyncio.new_event_loop()

    def _run_backend():
        asyncio.set_event_loop(async_loop)
        async_loop.run_until_complete(run_backend(bridge))

    backend_thread = threading.Thread(target=_run_backend, daemon=True, name="backend")
    backend_thread.start()

    # GUI na thread principal (tkinter exige isso)
    app = TelemetriaApp(async_loop=async_loop, msg_queue=bridge)

    # Poll da fila a cada 40 ms — drena TODAS as msgs disponíveis por ciclo
    def _poll():
        try:
            for _ in range(50):          # máximo 50 msgs por tick para não travar
                msg = sync_queue.get_nowait()
                app._handle_gui_msg(msg)
        except _queue.Empty:
            pass
        except Exception:
            pass
        app.after(40, _poll)

    app.after(200, _poll)               # aguarda o backend iniciar antes do 1º poll

    try:
        app.mainloop()
    finally:
        if shutdown_event:
            async_loop.call_soon_threadsafe(shutdown_event.set)
        log.info("Aplicação encerrada.")

if __name__ == "__main__":
    main()