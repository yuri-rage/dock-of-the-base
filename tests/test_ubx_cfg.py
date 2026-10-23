from typing import Any

from pyubx2 import UBXMessage

from app.main import _run_tmode_config
from app.ubx_cfg_valset import TMODE, config_fixed, config_fixed_ecef, split_m_cm_hp


def test_split_m_cm_hp_bounds():
    """Verify HP component strictly remains in [-99, +99] and avoids binary float truncation."""
    # Values that cause val2sphp to return 100 or -100
    assert split_m_cm_hp(0.29) == (29, 0)
    assert split_m_cm_hp(1.15) == (115, 0)
    assert split_m_cm_hp(-0.57) == (-57, 0)

    # Sub-cm precision values
    assert split_m_cm_hp(95.5432) == (9554, 32)
    assert split_m_cm_hp(-12.3456) == (-1234, -56)
    assert split_m_cm_hp(-612428.9355) == (-61242893, -55)


def test_config_fixed_height_scaling():
    """Verify height is correctly scaled from metres to cm and high-precision offset."""
    # 95.5 metres -> 9550 cm, 0 HP
    msg: Any = config_fixed(2000, 33.0, -96.0, 95.5)
    assert msg.CFG_TMODE_MODE == 2
    assert msg.CFG_TMODE_POS_TYPE == 1
    assert msg.CFG_TMODE_FIXED_POS_ACC == 2000
    assert msg.CFG_TMODE_HEIGHT == 9550
    assert msg.CFG_TMODE_HEIGHT_HP == 0

    # 0.29 metres must not produce HP == 100
    msg_29: Any = config_fixed(2000, 33.0, -96.0, 0.29)
    assert msg_29.CFG_TMODE_HEIGHT == 29
    assert msg_29.CFG_TMODE_HEIGHT_HP == 0

    # 95.5432 metres -> 9554 cm, 32 HP (3.2 mm)
    msg_hp: Any = config_fixed(2000, 33.0, -96.0, 95.5432)
    assert msg_hp.CFG_TMODE_HEIGHT == 9554
    assert msg_hp.CFG_TMODE_HEIGHT_HP == 32


def test_config_fixed_ecef():
    """Verify ECEF coordinates are stored as integer cm with HP offsets."""
    msg: Any = config_fixed_ecef(2000, 100.5, -200.25, 300.75)
    assert msg.CFG_TMODE_MODE == 2
    assert msg.CFG_TMODE_POS_TYPE == 0
    assert msg.CFG_TMODE_ECEF_X == 10050
    assert msg.CFG_TMODE_ECEF_X_HP == 0
    assert msg.CFG_TMODE_ECEF_Y == -20025
    assert msg.CFG_TMODE_ECEF_Y_HP == 0
    assert msg.CFG_TMODE_ECEF_Z == 30075
    assert msg.CFG_TMODE_ECEF_Z_HP == 0

    # High precision coordinates (e.g. from survey-in)
    msg_hp: Any = config_fixed_ecef(2000, 0.29, 1.15, -612428.9355)
    assert msg_hp.CFG_TMODE_ECEF_X == 29
    assert msg_hp.CFG_TMODE_ECEF_X_HP == 0
    assert msg_hp.CFG_TMODE_ECEF_Y == 115
    assert msg_hp.CFG_TMODE_ECEF_Y_HP == 0
    assert msg_hp.CFG_TMODE_ECEF_Z == -61242893
    assert msg_hp.CFG_TMODE_ECEF_Z_HP == -55


def test_run_tmode_config_call_site_scaling():
    """Integration test verifying _run_tmode_config sends correctly scaled height to send_fn."""
    sent: list[Any] = []

    def stub_send(msg: UBXMessage) -> bool:
        sent.append(msg)
        return True

    ok, desc = _run_tmode_config(
        send_fn=stub_send,
        tmode=TMODE.Fixed,
        acc_limit=0.2,
        svin_min_dur=15,
        coord_type="lla",
        lat=33.1234567,
        lon=-96.7654321,
        height=95.5,
        ecef_x=0.0,
        ecef_y=0.0,
        ecef_z=0.0,
        port_type=["UART1"],
    )

    assert ok is True
    assert "height 95.5 m HAE" in desc
    assert len(sent) == 1
    assert sent[0].CFG_TMODE_HEIGHT == 9550
    assert sent[0].CFG_TMODE_HEIGHT_HP == 0
