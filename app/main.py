import asyncio
import copy
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pyubx2 import UBXMessage

from app import __version__, receiver
from app.filters import deg_to_dms, google_maps_link, m_to_ft
from app.receiver import (
    CARRIER_SOLUTION_TYPES,
    FIX_TYPES,
    LOG_DIR,
    TMODE_NAMES,
    clear_log_files,
    configure_logging,
    delete_log_file,
    list_log_files,
    logging_active,
    ntrip_client_count,
    ntrip_in_active,
    ntrip_in_connect,
    ntrip_in_disconnect,
    ntrip_in_status_str,
    ntrip_mount,
    ntrip_out_connect,
    ntrip_out_connected,
    ntrip_out_disconnect,
    ntrip_out_status_str,
    ntrip_port,
    save_last_configure,
)
from app.ubx_cfg_valset import (
    PORT,
    TARGET_BAUDS,
    TMODE,
    auto_baud_connect,
    config_disabled,
    config_factory_reset,
    config_fixed,
    config_fixed_ecef,
    config_rtcm,
    config_signals,
    config_svin,
    config_ubx,
    config_ubx_raw_interval,
    send_msg,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)
logging.getLogger("app").setLevel(
    getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
)


class _ErrorsOnlyAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args[-1], int):
            return record.args[-1] >= 400
        return True


logging.getLogger("uvicorn.access").addFilter(_ErrorsOnlyAccessFilter())


def _apply_config_msgs(
    send_fn: Callable[[UBXMessage], bool],
    port_type: list[str],
    raw_interval: int,
    signal_msgs: list[UBXMessage],
    use_msm7: bool,
    tmode: int,
    acc_limit: float,
    svin_min_dur: int,
    coord_type: str,
    lat: float,
    lon: float,
    height: float,
    ecef_x: float,
    ecef_y: float,
    ecef_z: float,
) -> tuple[bool, list[str], str]:
    logs: list[str] = []
    if not send_fn(config_ubx(port_type)):
        return False, logs, "Failed to configure UBX output."
    logs.append(f"UBX output configured on {', '.join(port_type)}.")

    if not send_fn(config_ubx_raw_interval(port_type, raw_interval)):
        return False, logs, "Failed to configure raw data interval."
    logs.append(f"Raw data interval set to {raw_interval}s.")

    for msg in signal_msgs:
        if not send_fn(msg):
            return False, logs, "Failed to configure signal types."
    logs.append("Signal configuration applied.")

    if not send_fn(config_rtcm(port_type, use_msm7=use_msm7)):
        return False, logs, "Failed to configure RTCM output."
    logs.append(f"RTCM3 {'MSM7' if use_msm7 else 'MSM4'} output configured.")

    ok, tmode_log = _run_tmode_config(
        send_fn,
        tmode,
        acc_limit,
        svin_min_dur,
        coord_type,
        lat,
        lon,
        height,
        ecef_x,
        ecef_y,
        ecef_z,
        port_type,
    )
    if not ok:
        return False, logs, tmode_log
    if tmode_log:
        logs.append(tmode_log)
    return True, logs, ""


def _build_last_configure(
    existing: dict,
    tmode: int,
    acc_limit: float,
    svin_min_dur: int,
    use_msm7: bool,
    port_type: list[str],
    coord_type: str,
    lat: float,
    lon: float,
    height: float,
    ecef_x: float,
    ecef_y: float,
    ecef_z: float,
) -> dict:
    last = {
        **existing,
        "tmode": tmode,
        "acc_limit": acc_limit,
        "svin_min_dur": svin_min_dur,
        "use_msm7": use_msm7,
        "port_type": port_type,
    }
    if tmode == TMODE.Fixed:
        last.update(
            {
                "coord_type": coord_type,
                "lat": lat,
                "lon": lon,
                "height": height,
                "ecef_x": ecef_x,
                "ecef_y": ecef_y,
                "ecef_z": ecef_z,
            }
        )
    return last


def _run_tmode_config(
    send_fn: Callable[[UBXMessage], bool],
    tmode: int,
    acc_limit: float,
    svin_min_dur: int,
    coord_type: str,
    lat: float,
    lon: float,
    height: float,
    ecef_x: float,
    ecef_y: float,
    ecef_z: float,
    port_type: list[str],
) -> tuple[bool, str]:
    if tmode == TMODE.Disabled:
        if not send_fn(config_disabled()):
            return False, "Failed to disable time mode."
        return True, "Time mode disabled."
    if tmode == TMODE.Survey_In:
        if not send_fn(
            config_svin(port_type, int(acc_limit * 10000), svin_min_dur * 60)
        ):
            return False, "Failed to configure Survey-In mode."
        return (
            True,
            f"Survey-In mode configured (acc limit {acc_limit} m, min dur {svin_min_dur} min).",
        )
    if tmode == TMODE.Fixed:
        if coord_type == "ecef":
            fixed_msg = config_fixed_ecef(
                int(acc_limit * 10000), ecef_x, ecef_y, ecef_z
            )
            fixed_desc = f"ECEF ({ecef_x}, {ecef_y}, {ecef_z}) m"
        else:
            fixed_msg = config_fixed(int(acc_limit * 10000), lat, lon, height * 1000)
            fixed_desc = f"{lat}, {lon}, height {height} m HAE"
        if not send_fn(fixed_msg):
            return False, "Failed to configure Fixed mode."
        return True, f"Fixed mode configured: {fixed_desc}, acc limit {acc_limit} m"
    return True, ""


def get_serial_ports(show_all: bool = False) -> list[str]:
    if show_all:
        exclude = None
    else:
        pattern = (receiver.load_config() or {}).get("tty_exclude", "")
        try:
            exclude = re.compile(pattern) if pattern else None
        except re.error:
            exclude = None
    ttys = (
        p for p in Path("/dev").glob("tty*") if not exclude or not exclude.match(p.name)
    )
    serial_dir = Path("/dev/serial")
    serials = serial_dir.glob("**/*") if serial_dir.is_dir() else []
    return sorted({str(p) for p in (*ttys, *serials) if p.is_char_device()})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    receiver.start()
    yield
    await asyncio.to_thread(receiver.stop)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["dms"] = deg_to_dms
templates.env.filters["m_to_ft"] = m_to_ft
templates.env.filters["google_maps_link"] = google_maps_link


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = receiver.load_config() or {}
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "ports": list(PORT),
            "bauds": TARGET_BAUDS,
            "tmodes": list(TMODE),
            "serial_ports": get_serial_ports(),
            "cfg": cfg,
            "last_cfg": cfg.get("last_configure", {}),
            "ntrip_in_active": ntrip_in_active(),
            "ntrip_out_status": ntrip_out_connected(),
            "log_cfg": cfg.get("logging", {}),
            "version": __version__,
        },
    )


@app.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    with receiver._lock:
        s = copy.copy(receiver.state)
    cfg = receiver.load_config() or {}
    ntrip_out_url = cfg.get("external_caster", {}).get("url") or ""
    ntrip_out_url = ntrip_out_url.removeprefix("https://").removeprefix("http://")
    return templates.TemplateResponse(
        request,
        "_status.html",
        {
            "state": s,
            "acc_limit": cfg.get("last_configure", {}).get("acc_limit"),
            "fix_types": FIX_TYPES,
            "carrier_solution_types": CARRIER_SOLUTION_TYPES,
            "tmode_names": TMODE_NAMES,
            "tcp_port": receiver.tcp_port(),
            "tcp_clients": receiver.tcp_client_count(),
            "ntrip_port": ntrip_port(),
            "ntrip_mount": ntrip_mount(),
            "ntrip_clients": ntrip_client_count(),
            "ntrip_out_connected": ntrip_out_connected(),
            "ntrip_out_status": ntrip_out_status_str(),
            "ntrip_out_url": ntrip_out_url,
            "ntrip_in_status": ntrip_in_status_str(),
        },
    )


@app.get("/serial-ports", response_class=HTMLResponse)
async def serial_ports_route(request: Request, show_all: bool = Query(False)):
    cfg = receiver.load_config() or {}
    return templates.TemplateResponse(
        request,
        "_serial_port_select.html",
        {
            "serial_ports": get_serial_ports(show_all=show_all),
            "cfg": cfg,
        },
    )


@app.post("/configure", response_class=HTMLResponse)
async def configure(
    request: Request,
    serial_port: str = Form(...),
    port_type: Annotated[list[str], Form()] = [],
    target_baud: int = Form(...),
    tmode: int = Form(...),
    acc_limit: float = Form(0.2),
    svin_min_dur: int = Form(15),
    lat: float = Form(0.0),
    lon: float = Form(0.0),
    height: float = Form(0.0),
    coord_type: str = Form("lla"),
    ecef_x: float = Form(0.0),
    ecef_y: float = Form(0.0),
    ecef_z: float = Form(0.0),
    use_msm7: str = Form(""),
):
    def run_config():
        if not port_type:
            return False, "No port type selected.", {}

        cfg = receiver.load_config() or {}
        raw_interval = cfg.get("logging", {}).get("raw_interval_s", 30)
        msm7 = use_msm7 == "true"
        existing = (receiver.load_config() or {}).get("last_configure", {})

        with receiver._lock:
            connected = receiver.state.connected
            is_multiband = receiver.state.is_multiband

        live = (
            connected
            and serial_port == cfg.get("serial_port")
            and target_baud == cfg.get("target_baud")
        )

        if live:
            ok, logs, err = _apply_config_msgs(
                receiver.send_config,
                port_type,
                raw_interval,
                config_signals(multiband=is_multiband),
                msm7,
                tmode,
                acc_limit,
                svin_min_dur,
                coord_type,
                lat,
                lon,
                height,
                ecef_x,
                ecef_y,
                ecef_z,
            )
            if not ok:
                return False, err, {}
            with receiver._lock:
                receiver.state.tmode = tmode
                if tmode != 1:
                    receiver.state.svin_active = False
                    receiver.state.svin_valid = False
            logs.insert(0, f"Reconfiguring live connection on {', '.join(port_type)}.")
            receiver.save_config(serial_port, port_type[0], target_baud)
            save_last_configure(
                _build_last_configure(
                    existing,
                    tmode,
                    acc_limit,
                    svin_min_dur,
                    msm7,
                    port_type,
                    coord_type,
                    lat,
                    lon,
                    height,
                    ecef_x,
                    ecef_y,
                    ecef_z,
                )
            )
            return True, "\n".join(logs), {}

        # Not live — stop receiver, connect fresh, configure, restart
        receiver.stop(timeout=3.0)
        connect_port = port_type[0]
        stream, mon_ver = auto_baud_connect(serial_port, connect_port, target_baud)
        if not stream or mon_ver is None:
            receiver.start()
            return False, "Could not connect: no response at any baud rate.", {}

        receiver_info = {
            k: v.decode().strip() if isinstance(v, bytes) else str(v)
            for k, v in vars(mon_ver).items()
            if not k.startswith("_")
        }
        ok, logs, err = _apply_config_msgs(
            lambda msg: send_msg(stream, msg),
            port_type,
            raw_interval,
            config_signals(mon_ver=mon_ver),
            msm7,
            tmode,
            acc_limit,
            svin_min_dur,
            coord_type,
            lat,
            lon,
            height,
            ecef_x,
            ecef_y,
            ecef_z,
        )
        stream.close()
        if not ok:
            receiver.start()
            return False, err, receiver_info
        logs.insert(0, f"Connected via {connect_port}.")
        receiver.save_config(serial_port, connect_port, target_baud)
        save_last_configure(
            _build_last_configure(
                existing,
                tmode,
                acc_limit,
                svin_min_dur,
                msm7,
                port_type,
                coord_type,
                lat,
                lon,
                height,
                ecef_x,
                ecef_y,
                ecef_z,
            )
        )
        receiver.start()
        return True, "\n".join(logs), receiver_info

    ok, message, receiver_info = await asyncio.to_thread(run_config)
    return templates.TemplateResponse(
        request,
        "_configure_result.html",
        {
            "ok": ok,
            "message": message,
            "receiver_info": receiver_info,
        },
    )


@app.get("/tmode-fields", response_class=HTMLResponse)
async def tmode_fields(
    request: Request,
    tmode: int = Query(0),
    ecef_x: float | None = Query(None),
    ecef_y: float | None = Query(None),
    ecef_z: float | None = Query(None),
    acc_limit: float | None = Query(None),
):
    if tmode == TMODE.Survey_In:
        template = "_tmode_fields_svin.html"
    elif tmode == TMODE.Fixed:
        template = "_tmode_fields_fixed.html"
    else:
        template = "_tmode_fields_disabled.html"
    cfg = receiver.load_config() or {}
    last_cfg = cfg.get("last_configure", {})
    if tmode == TMODE.Fixed and ecef_x is not None:
        last_cfg = {
            **last_cfg,
            "coord_type": "ecef",
            "ecef_x": ecef_x,
            "ecef_y": ecef_y,
            "ecef_z": ecef_z,
        }
    if acc_limit is not None:
        last_cfg = {**last_cfg, "acc_limit": acc_limit}
    return templates.TemplateResponse(request, template, {"last_cfg": last_cfg})


@app.get("/ntrip-in/status", response_class=HTMLResponse)
async def ntrip_in_status_route(request: Request):
    return templates.TemplateResponse(
        request,
        "_ntrip_in_actions.html",
        {"connected": ntrip_in_active()},
    )


@app.post("/ntrip-in/connect", response_class=HTMLResponse)
async def ntrip_in_connect_route(
    request: Request,
    ntrip_in_url: str = Form(""),
    ntrip_in_port: int = Form(2101),
    ntrip_in_mount: str = Form(""),
    ntrip_in_user: str = Form(""),
    ntrip_in_password: str = Form(""),
):
    def run():
        cfg = receiver.load_config() or {}
        cfg["ntrip_in"] = {
            "url": ntrip_in_url,
            "port": ntrip_in_port,
            "mount_point": ntrip_in_mount,
            "username": ntrip_in_user,
            "password": ntrip_in_password,
        }
        receiver.CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        return ntrip_in_connect()

    error = await asyncio.to_thread(run)
    return templates.TemplateResponse(
        request,
        "_ntrip_in_result.html",
        {
            "ok": error is None,
            "message": error or "Connected to external NTRIP source.",
            "connected": error is None,
        },
    )


@app.post("/ntrip-in/disconnect", response_class=HTMLResponse)
async def ntrip_in_disconnect_route(request: Request):
    await asyncio.to_thread(ntrip_in_disconnect)
    return templates.TemplateResponse(
        request,
        "_ntrip_in_result.html",
        {
            "ok": True,
            "message": "External NTRIP source disconnected.",
            "connected": False,
        },
    )


@app.post("/ntrip-out/connect", response_class=HTMLResponse)
async def ntrip_out_connect_route(
    request: Request,
    ntrip_out_url: str = Form(""),
    ntrip_out_port: str = Form(""),
    ntrip_out_mount: str = Form(""),
    ntrip_out_password: str = Form(""),
):
    port = int(ntrip_out_port) if ntrip_out_port.strip() else 2101

    def run():
        cfg = receiver.load_config() or {}
        cfg["external_caster"] = {
            "url": ntrip_out_url,
            "port": port,
            "mount_point": ntrip_out_mount,
            "password": ntrip_out_password,
        }
        receiver.CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        if not ntrip_out_url.strip() or not port:
            ntrip_out_disconnect()
            return None
        return ntrip_out_connect()

    error = await asyncio.to_thread(run)
    disabled = not ntrip_out_url.strip() or not port
    return templates.TemplateResponse(
        request,
        "_ntrip_out_result.html",
        {
            "ok": disabled or error is None,
            "message": "External caster disabled."
            if disabled
            else (error or "Connected to external NTRIP caster."),
            "connected": False if disabled else error is None,
        },
    )


@app.post("/ntrip-out/disconnect", response_class=HTMLResponse)
async def ntrip_out_disconnect_route(request: Request):
    await asyncio.to_thread(ntrip_out_disconnect)
    return templates.TemplateResponse(
        request,
        "_ntrip_out_result.html",
        {
            "ok": True,
            "message": "External NTRIP caster disconnected.",
            "connected": False,
        },
    )


@app.post("/factory-reset", response_class=HTMLResponse)
async def factory_reset(request: Request):
    def run_reset():
        cfg = receiver.load_config()
        if not cfg:
            return False, "No saved configuration — cannot connect to receiver."
        receiver.stop(timeout=3.0)
        stream, _ = auto_baud_connect(
            cfg["serial_port"], cfg["port_type"], cfg["target_baud"]
        )
        if not stream:
            receiver.start()
            return False, "Could not connect: no response at any baud rate."
        stream.write(config_factory_reset().serialize())
        stream.flush()
        stream.close()
        receiver.start()
        return True, "Factory reset sent. Receiver is rebooting."

    ok, message = await asyncio.to_thread(run_reset)
    return templates.TemplateResponse(
        request,
        "_configure_result.html",
        {"ok": ok, "message": message, "receiver_info": {}},
    )


@app.post("/network", response_class=HTMLResponse)
async def network(
    request: Request,
    tcp_port_val: int = Form(0, alias="tcp_port"),
    ntrip_port_val: int = Form(0, alias="ntrip_port"),
    ntrip_mount_val: str = Form("BASE", alias="ntrip_mount"),
):
    await asyncio.to_thread(
        receiver.configure_network,
        tcp_port_val,
        ntrip_port_val,
        ntrip_mount_val,
    )
    return templates.TemplateResponse(
        request,
        "_network_result.html",
        {
            "tcp_port": tcp_port_val,
            "ntrip_port": ntrip_port_val,
            "ntrip_mount": ntrip_mount_val,
        },
    )


@app.post("/logging", response_class=HTMLResponse)
async def configure_logging_route(
    request: Request,
    enabled: str = Form(""),
    file_duration_h: float = Form(1.0),
    retention_days: int = Form(2),
    raw_interval_s: int = Form(30),
):
    old_interval = (
        (receiver.load_config() or {}).get("logging", {}).get("raw_interval_s", 30)
    )

    await asyncio.to_thread(
        configure_logging,
        enabled == "true",
        file_duration_h,
        retention_days,
        raw_interval_s,
    )

    if raw_interval_s != old_interval:

        def apply_interval():
            cfg = receiver.load_config() or {}
            last = cfg.get("last_configure", {})
            port_types = last.get("port_type") or [cfg.get("port_type", "UART1")]
            if isinstance(port_types, str):
                port_types = [port_types]
            with receiver._lock:
                connected = receiver.state.connected
            if connected:
                receiver.send_config(
                    config_ubx_raw_interval(port_types, raw_interval_s)
                )
                return
            receiver.stop(timeout=3.0)
            stream, _ = auto_baud_connect(
                cfg.get("serial_port", ""),
                cfg.get("port_type", "UART1"),
                cfg.get("target_baud", 230400),
            )
            if not stream:
                receiver.start()
                return
            send_msg(stream, config_ubx_raw_interval(port_types, raw_interval_s))
            stream.close()
            receiver.start()

        await asyncio.to_thread(apply_interval)
    return templates.TemplateResponse(
        request,
        "_logging_result.html",
        {
            "ok": True,
            "enabled": enabled == "true",
            "file_duration_h": file_duration_h,
            "retention_days": retention_days,
        },
    )


@app.get("/logging/files", response_class=HTMLResponse)
async def logging_files_route(request: Request):
    files = await asyncio.to_thread(list_log_files)
    return templates.TemplateResponse(
        request,
        "_logging_files.html",
        {"files": files, "active": logging_active()},
    )


@app.post("/logging/clear", response_class=HTMLResponse)
async def logging_clear(request: Request):
    await asyncio.to_thread(clear_log_files)
    files = await asyncio.to_thread(list_log_files)
    return templates.TemplateResponse(
        request,
        "_logging_files.html",
        {"files": files, "active": logging_active()},
    )


@app.post("/logging/delete/{filename}", response_class=HTMLResponse)
async def logging_delete(request: Request, filename: str):
    if not re.match(r"^rawx_\d{8}_\d{6}\.(ubx|obs|nav)$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    await asyncio.to_thread(delete_log_file, filename)
    files = await asyncio.to_thread(list_log_files)
    return templates.TemplateResponse(
        request,
        "_logging_files.html",
        {"files": files, "active": logging_active()},
    )


@app.get("/logging/download/{filename}")
async def logging_download(filename: str):
    if not re.match(r"^rawx_\d{8}_\d{6}\.(ubx|obs|nav)$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = LOG_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
