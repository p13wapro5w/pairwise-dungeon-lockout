import pytest
import gameplay.dungeon_lockout as mod

CASES = [
    ("server_timezone", (8, 2)),
    ("party_share", (1, 2)),
    ("instance_bind", (1, 2)),
    ("difficulty_split", (1, 2)),
    ("reward_once", (1, 2)),
    ("online_reset", (1, 2)),
    ("global_id", (1, 2)),
    ("account_lock", (1, 2)),
    ("leave_release", (1, 2)),
    ("reset_protect", (1, 2)),
    ("lockout_timer", (24.0, 1.0)),
    ("id_map", (1, 2)),
    ("party_join", (1, 2)),
    ("difficulty_key", (1, 2)),
    ("reward_flag", (1, 2)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
