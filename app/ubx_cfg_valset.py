"""
Configures ZED-F9 and ZED-X20 series GNSS receivers for fixed base station operation using CFG-VALSET.

May be compatible with other UBX-enabled precision GNSS receivers (untested) that support CFG-VALSET.

Adapted from https://github.com/semuconsulting/pyubx2/blob/master/examples/f9p_basestation.py

-- Yuri - May 2026
"""

import logging
from enum import IntEnum, StrEnum
from time import monotonic
from typing import cast

from pyrtcm import RTCMParseError
from pyubx2 import (
    POLL,
    SET,
    UBXMessage,
    UBXMessageError,
    UBXParseError,
    UBXReader,
    val2sphp,
)
from serial import Serial


class PORT(StrEnum):
    USB = "USB"
    UART1 = "UART1"
    UART2 = "UART2"


class LAYER(IntEnum):
    RAM = 1
    BBR = 2
    Flash = 4


class TMODE(IntEnum):
    Disabled = 0
    Survey_In = 1
    Fixed = 2


log = logging.getLogger(__name__)

COMMON_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
TARGET_BAUDS = (115200, 230400, 460800, 921600)
SERIAL_TIMEOUT = 1.0


def _open_and_verify(
    port: str, baud: int, poll: UBXMessage
) -> tuple[Serial, UBXMessage] | tuple[None, None]:
    """
    Open serial port at baud, send MON-VER poll, return (stream, MON-VER) if confirmed.
    Stream timeout is reset to SERIAL_TIMEOUT on success; closed and (None, None) returned otherwise.
    """
    stream = None
    try:
        stream = Serial(port, baud, timeout=0.1)
        stream.reset_input_buffer()
        stream.write(poll.serialize())
        reader = UBXReader(stream, protfilter=2, quitonerror=0)
        deadline = monotonic() + SERIAL_TIMEOUT
        while monotonic() < deadline:
            try:
                if stream.in_waiting:
                    _, msg = reader.read()
                    if msg and msg.identity == "MON-VER":
                        log.debug("MON-VER confirmed at %d baud on %s", baud, port)
                        stream.timeout = SERIAL_TIMEOUT
                        return stream, cast(UBXMessage, msg)
            except (UBXParseError, UBXMessageError):
                pass
        log.debug("No MON-VER response at %d baud on %s", baud, port)
        stream.close()
    except Exception as e:
        log.warning("Serial error on %s at %d baud: %s", port, baud, e)
        if stream and stream.is_open:
            stream.close()
    return None, None


def auto_baud_connect(
    port: str, port_type: str, target_baud: int
) -> tuple[Serial, UBXMessage] | tuple[None, None]:
    """
    Iterate COMMON_BAUDS sending CFG-VALSET to set target_baud on port_type.
    Verify by polling MON-VER at target_baud. Returns (Serial, MON-VER) if successful.
    USB ports have no configurable baud rate; skips scan and connects directly.
    """
    poll = UBXMessage("MON", "MON-VER", POLL)

    # USB CDC ignores baud rate — skip the scan and connect directly
    if port_type == PORT.USB:
        log.info("USB port — connecting directly on %s", port)
        return _open_and_verify(port, target_baud, poll)

    SCAN_BAUDS = (target_baud, *[b for b in COMMON_BAUDS if b != target_baud])
    cfg_key = f"CFG_{port_type}_BAUDRATE"
    valset = cast(UBXMessage, UBXMessage.config_set(1, 0, [(cfg_key, target_baud)]))

    for baud in SCAN_BAUDS:
        log.debug("Trying %d baud on %s", baud, port)
        stream = None
        try:
            stream = Serial(port, baud, timeout=0.1)
            stream.reset_input_buffer()
            stream.write(valset.serialize())
            stream.flush()
            stream.close()
        except Exception as e:
            log.warning("Serial error on %s at %d baud: %s", port, baud, e)
            if stream and stream.is_open:
                stream.close()
            continue

        result = _open_and_verify(port, target_baud, poll)
        if result[0] is not None:
            return result

    return None, None


def send_msg(stream: Serial, ubx: UBXMessage, timeout: float = SERIAL_TIMEOUT) -> bool:
    """
    Send config message to receiver and wait for ACK-ACK.
    Returns True if acknowledged within timeout.
    """
    log.debug("Sending config message: %s", ubx.identity)
    stream.write(ubx.serialize())

    reader = UBXReader(stream, protfilter=6, quitonerror=0)
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            if stream.in_waiting:
                _, msg = reader.read()
                if msg and msg.identity == "ACK-ACK":
                    log.debug("ACK received for %s", ubx.identity)
                    return True
                if msg and msg.identity == "ACK-NAK":
                    log.warning("NAK received for %s", ubx.identity)
                    return False
        except (UBXParseError, UBXMessageError, RTCMParseError):
            pass
    log.warning("No ACK received for %s within %.1fs", ubx.identity, timeout)
    return False


def config_factory_reset() -> UBXMessage:
    """
    Revert u-Blox receiver to default.
    """
    return UBXMessage(
        "CFG",
        "CFG-CFG",
        SET,
        clearMask=b"\x1f\x1f\x00\x00",  # clear everything
        loadMask=b"\x1f\x1f\x00\x00",  # reload everything
        devBBR=1,  # clear from battery-backed RAM
        devFlash=1,  # clear from flash memory
        devEEPROM=1,  # clear from EEPROM memory
    )


def config_ubx_raw_interval(ports: list[str], interval: int) -> UBXMessage:
    """
    Configure raw (RAWX / SFRBX) output interval.
    """
    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    transaction = 0
    cfg_data: list[tuple[int | str, object]] = []
    for port_type in ports:
        cfg_data.append((f"CFG_MSGOUT_UBX_RXM_RAWX_{port_type}", interval))
        cfg_data.append((f"CFG_MSGOUT_UBX_RXM_SFRBX_{port_type}", interval))

    ubx = cast(UBXMessage, UBXMessage.config_set(layers, transaction, cfg_data))

    log.debug("CFG-VALSET payload (UBX output): %s", cast(bytes, ubx.payload).hex())

    return ubx


def config_ubx(ports: list[str]) -> UBXMessage:
    """
    Configure UBX output and suppress NMEA.
    """

    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    transaction = 0
    cfg_data: list[tuple[int | str, object]] = [
        ("CFG_RATE_MEAS", 1000),
        ("CFG_RATE_NAV", 1),
    ]
    for port_type in ports:
        cfg_data.append((f"CFG_{port_type}OUTPROT_NMEA", 0))
        cfg_data.append((f"CFG_{port_type}OUTPROT_UBX", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_PVT_{port_type}", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_POSECEF_{port_type}", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_POSLLH_{port_type}", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_HPPOSLLH_{port_type}", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_SAT_{port_type}", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_SIG_{port_type}", 1))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_STATUS_{port_type}", 5))
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_DOP_{port_type}", 5))

    ubx = cast(UBXMessage, UBXMessage.config_set(layers, transaction, cfg_data))

    log.debug("CFG-VALSET payload (UBX output): %s", cast(bytes, ubx.payload).hex())

    return ubx


def _is_multiband(mon_ver: UBXMessage) -> bool:
    """Returns True if MON-VER extension strings identify an X20-class receiver."""
    for i in range(1, 31):
        ext = getattr(mon_ver, f"extension_{i:02d}", None)
        if ext is None:
            break
        if isinstance(ext, bytes):
            ext = ext.decode(errors="ignore")
        if "X20" in ext:
            return True
    return False


def config_signals(
    mon_ver: UBXMessage | None = None, multiband: bool = False
) -> list[UBXMessage]:
    """
    Return a list of CFG-VALSET messages configuring constellations and signal types.

    Core signals (GPS L1/L2, Galileo E1/E5b, BeiDou B1/B2, GLONASS L1/L2) are sent
    to all receivers. On X20-class hardware a second message adds GPS L5, Galileo E5a/E6,
    and BeiDou B1C/B2a/B3. Sending separately avoids a whole-message NAK if the receiver
    doesn't recognize an extended key.

    Pass either a MON-VER message or the pre-computed multiband bool.
    """
    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    is_mb = _is_multiband(mon_ver) if mon_ver is not None else multiband

    core: list[tuple[int | str, object]] = [
        ("CFG_SIGNAL_GPS_ENA", 1),
        ("CFG_SIGNAL_GPS_L1CA_ENA", 1),
        ("CFG_SIGNAL_GPS_L2C_ENA", 1),
        ("CFG_SIGNAL_GAL_ENA", 1),
        ("CFG_SIGNAL_GAL_E1_ENA", 1),
        ("CFG_SIGNAL_GAL_E5B_ENA", 1),
        ("CFG_SIGNAL_BDS_ENA", 1),
        ("CFG_SIGNAL_BDS_B1_ENA", 1),
        ("CFG_SIGNAL_BDS_B2_ENA", 1),
        ("CFG_SIGNAL_GLO_ENA", 1),
        ("CFG_SIGNAL_GLO_L1_ENA", 1),
        ("CFG_SIGNAL_GLO_L2_ENA", 1),
        ("CFG_SIGNAL_SBAS_ENA", 0),
        ("CFG_SIGNAL_QZSS_ENA", 0),
    ]

    msgs = [cast(UBXMessage, UBXMessage.config_set(layers, 0, core))]

    if is_mb:
        extended: list[tuple[int | str, object]] = [
            ("CFG_SIGNAL_GPS_L5_ENA", 1),
            ("CFG_SIGNAL_HEALTH_L5", 1),  # ignore pre-operational L5 health flags
            ("CFG_SIGNAL_BDS_B1C_ENA", 1),
            ("CFG_SIGNAL_BDS_B2A_ENA", 1),
            ("CFG_SIGNAL_BDS_B3_ENA", 1),
            ("CFG_SIGNAL_GAL_E5A_ENA", 1),
            ("CFG_SIGNAL_GAL_E6_ENA", 1),
            ("CFG_SIGNAL_NAVIC_ENA", 0),
            # ("CFG_SIGNAL_LBAND_ENA", 0), # future implementation if pyubx2 adds support
            # ("CFG_SIGNAL_LBAND_PMP_ENA", 0), # future implementation if pyubx2 adds support
        ]
        msgs.append(cast(UBXMessage, UBXMessage.config_set(layers, 0, extended)))
        log.debug("X20-class receiver — extended signal config applied")
    else:
        log.debug("F9-class receiver — core signal config only")

    return msgs


def config_rtcm(ports: list[str], use_msm7: bool = False) -> UBXMessage:
    """
    Configure which RTCM3 messages to output.
    """

    enable_msm_ver = "7" if use_msm7 else "4"
    disable_msm_ver = "4" if use_msm7 else "7"
    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    transaction = 0
    cfg_data: list[tuple[int | str, object]] = []
    for rtcm_type, rate in (
        ("1005", 5),
        (f"107{enable_msm_ver}", 1),
        (f"108{enable_msm_ver}", 1),
        (f"109{enable_msm_ver}", 1),
        (f"112{enable_msm_ver}", 1),
        (f"107{disable_msm_ver}", 0),
        (f"108{disable_msm_ver}", 0),
        (f"109{disable_msm_ver}", 0),
        (f"112{disable_msm_ver}", 0),
        ("1230", 5),
    ):
        for port_type in ports:
            cfg = f"CFG_MSGOUT_RTCM_3X_TYPE{rtcm_type}_{port_type}"
            cfg_data.append((cfg, rate))

    ubx = cast(UBXMessage, UBXMessage.config_set(layers, transaction, cfg_data))

    log.debug("CFG-VALSET payload (RTCM output): %s", cast(bytes, ubx.payload).hex())

    return ubx


def config_svin(ports: list[str], acc_limit: int, svin_min_dur: int) -> UBXMessage:
    """
    Configure Survey-In mode. acc_limit in 0.1 mm units (CFG_TMODE_SVIN_ACC_LIMIT).
    """

    tmode = TMODE.Survey_In
    layers = LAYER.RAM
    transaction = 0
    cfg_data: list[tuple[int | str, object]] = [
        ("CFG_TMODE_MODE", tmode),
        ("CFG_TMODE_SVIN_ACC_LIMIT", acc_limit),
        ("CFG_TMODE_SVIN_MIN_DUR", svin_min_dur),
    ]

    for port_type in ports:
        cfg_data.append((f"CFG_MSGOUT_UBX_NAV_SVIN_{port_type}", 1))

    ubx = cast(UBXMessage, UBXMessage.config_set(layers, transaction, cfg_data))

    log.debug("CFG-VALSET payload (Survey-In mode): %s", cast(bytes, ubx.payload).hex())

    return ubx


def config_disabled() -> UBXMessage:
    """
    Disable time mode, reverting the receiver from Fixed or Survey-In.
    """
    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    cfg_data: list[tuple[int | str, object]] = [
        ("CFG_TMODE_MODE", TMODE.Disabled),
    ]
    ubx = cast(UBXMessage, UBXMessage.config_set(layers, 0, cfg_data))

    log.debug(
        "CFG-VALSET payload (time mode disabled): %s", cast(bytes, ubx.payload).hex()
    )

    return ubx


def config_fixed(acc_limit: int, lat: float, lon: float, height: float) -> UBXMessage:
    """
    Configure Fixed mode with LLA coordinates. acc_limit in 0.1 mm units.
    height in mm (CFG_TMODE_HEIGHT).
    """

    tmode = TMODE.Fixed
    pos_type = 1  # LLH (as opposed to ECEF)
    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    transaction = 0
    lats, lath = val2sphp(lat)
    lons, lonh = val2sphp(lon)

    height = int(height)
    cfg_data: list[tuple[int | str, object]] = [
        ("CFG_TMODE_MODE", tmode),
        ("CFG_TMODE_POS_TYPE", pos_type),
        ("CFG_TMODE_FIXED_POS_ACC", acc_limit),
        ("CFG_TMODE_HEIGHT_HP", 0),
        ("CFG_TMODE_HEIGHT", height),
        ("CFG_TMODE_LAT", lats),
        ("CFG_TMODE_LAT_HP", lath),
        ("CFG_TMODE_LON", lons),
        ("CFG_TMODE_LON_HP", lonh),
    ]

    ubx = cast(UBXMessage, UBXMessage.config_set(layers, transaction, cfg_data))

    log.debug("CFG-VALSET payload (fixed mode LLA): %s", cast(bytes, ubx.payload).hex())

    return ubx


def config_fixed_ecef(
    acc_limit: int, ecef_x: float, ecef_y: float, ecef_z: float
) -> UBXMessage:
    """
    Configure Fixed mode with ECEF coordinates. acc_limit in 0.1 mm units.
    ecef_x/y/z in metres; stored as integer cm (CFG_TMODE_ECEF_X/Y/Z scale = 0.01 m).
    """

    layers = LAYER.RAM | LAYER.BBR | LAYER.Flash
    cfg_data: list[tuple[int | str, object]] = [
        ("CFG_TMODE_MODE", TMODE.Fixed),
        ("CFG_TMODE_POS_TYPE", 0),  # 0 = ECEF
        ("CFG_TMODE_FIXED_POS_ACC", acc_limit),
        ("CFG_TMODE_ECEF_X", int(ecef_x * 100)),
        ("CFG_TMODE_ECEF_X_HP", 0),
        ("CFG_TMODE_ECEF_Y", int(ecef_y * 100)),
        ("CFG_TMODE_ECEF_Y_HP", 0),
        ("CFG_TMODE_ECEF_Z", int(ecef_z * 100)),
        ("CFG_TMODE_ECEF_Z_HP", 0),
    ]

    ubx = cast(UBXMessage, UBXMessage.config_set(layers, 0, cfg_data))

    log.debug(
        "CFG-VALSET payload (fixed mode ECEF): %s", cast(bytes, ubx.payload).hex()
    )

    return ubx
