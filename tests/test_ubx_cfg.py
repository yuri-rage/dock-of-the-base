from typing import Any

from app.ubx_cfg_valset import config_fixed, config_fixed_ecef


def test_config_fixed_height_scaling():
    """Verify height is correctly scaled from metres to cm and high-precision offset."""
    # 95.5 metres -> 9550 cm, 0 HP
    msg: Any = config_fixed(2000, 33.0, -96.0, 95.5)
    assert msg.CFG_TMODE_MODE == 2
    assert msg.CFG_TMODE_POS_TYPE == 1
    assert msg.CFG_TMODE_FIXED_POS_ACC == 2000
    assert msg.CFG_TMODE_HEIGHT == 9550
    assert msg.CFG_TMODE_HEIGHT_HP == 0

    # 95.5432 metres -> 9554 cm, 32 HP (3.2 mm)
    msg_hp: Any = config_fixed(2000, 33.0, -96.0, 95.5432)
    assert msg_hp.CFG_TMODE_HEIGHT == 9554
    assert msg_hp.CFG_TMODE_HEIGHT_HP == 32


def test_config_fixed_ecef():
    """Verify ECEF coordinates are stored as integer cm."""
    msg: Any = config_fixed_ecef(2000, 100.5, -200.25, 300.75)
    assert msg.CFG_TMODE_MODE == 2
    assert msg.CFG_TMODE_POS_TYPE == 0
    assert msg.CFG_TMODE_ECEF_X == 10050
    assert msg.CFG_TMODE_ECEF_Y == -20025
    assert msg.CFG_TMODE_ECEF_Z == 30075
