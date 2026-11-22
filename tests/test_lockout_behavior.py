"""10 个修复点的行为测试——每条对应一个线上事故场景。"""

from datetime import datetime, timedelta, timezone

import pytest

from gameplay.dungeon_lockout import (
    DungeonLockoutService,
    DungeonDef,
    Party,
    RewardAlreadyClaimed,
    account_lock,
    difficulty_key,
    difficulty_split,
    global_id,
    id_map,
    instance_bind,
    leave_release,
    lockout_timer,
    online_reset,
    party_join,
    party_share,
    reset_protect,
    reward_flag,
    reward_once,
    server_timezone,
)

SH = timezone(timedelta(hours=8))
UTC = timezone.utc


def make_service():
    svc = DungeonLockoutService(8)
    svc.register_dungeon(
        DungeonDef("d1", bosses=("b1", "b2"),
                   difficulties=("normal", "heroic"))
    )
    svc.register_account("acct-leader", ["leader"])
    svc.register_account("acct-mate", ["mate"])
    svc.register_account("acct-alt", ["main", "alt"])
    return svc


# -- 修复点 1：按服务器时区重置，而不是 UTC 零点 ----------------------------

def test_server_timezone_reset_boundary_is_local_midnight():
    # UTC 2026-11-22 20:00 == 北京时间 11-23 04:00（周一）
    now = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
    info = server_timezone(8, now)
    assert info["offset_hours"] == 8
    # 下一个重置点是北京时间 11-30（周一）00:00 == UTC 11-29 16:00
    assert info["next_reset"] == datetime(2026, 11, 30, 0, 0, tzinfo=SH)
    assert info["next_reset"].astimezone(UTC) == datetime(
        2026, 11, 29, 16, 0, tzinfo=UTC
    )


def test_utc_midnight_is_not_reset_for_cn_server():
    # UTC 周一零点对国内服务器是周一早 8 点，重置边界应仍是本地零点
    utc_monday_midnight = datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
    info = server_timezone(8, utc_monday_midnight)
    local = info["now"]
    assert local.hour == 8  # 本地时间已是 8 点，不是零点
    assert info["next_reset"] > local


def test_lockout_timer_uses_server_tz():
    t = lockout_timer(24.0, datetime(2026, 11, 23, 0, 0, tzinfo=UTC))
    assert t["end"] - t["start"] == timedelta(hours=24)
    assert t["start"].utcoffset() == timedelta(hours=8)


# -- 修复点 2：队伍进度共享 -------------------------------------------------

def test_party_share_same_lockout_for_whole_party():
    svc = make_service()
    party = svc.party_join("p1", "leader")
    svc.party_join("p1", "mate")
    lockout = svc.party_share(party, "d1", "normal")
    mate_key = difficulty_key("d1", "normal", "acct-mate")
    assert mate_key in svc.lockouts
    # 队员账号上的冷却与队长共享同一份击杀集合
    svc.kill_boss(lockout.key, "b1")
    assert "b1" in svc.lockouts[mate_key].killed_bosses
    assert svc.lockouts[mate_key].expires_at == lockout.expires_at


def test_party_entry_reuses_leader_progress_not_empty_instance():
    svc = make_service()
    leader_party = svc.party_join("p0", "leader")
    first = svc.party_share(leader_party, "d1", "normal")
    svc.kill_boss(first.key, "b1")
    # 队长带新队员再进：新队员看到的是已有进度，不是空本
    full = svc.party_join("p1", "leader")
    svc.party_join("p1", "mate")
    svc.party_share(full, "d1", "normal")
    mate_lock = svc.lockouts[difficulty_key("d1", "normal", "acct-mate")]
    assert "b1" in mate_lock.killed_bosses


# -- 修复点 3：实例绑定，掉线重连不换实例 -----------------------------------

def test_reconnect_finds_same_instance_via_id_map():
    svc = make_service()
    party = svc.party_join("p1", "leader")
    svc.party_share(party, "d1", "normal")
    bound = svc.id_map("leader")
    assert bound  # 非空
    # 模拟掉线：角色状态清空后重连
    svc.characters["leader"].online = False
    reconnect = svc.instance_bind("leader")
    assert reconnect is not None
    assert reconnect.instance_id == bound


def test_unknown_character_reconnect_returns_false_instead_of_new_instance():
    svc = make_service()
    assert svc.instance_bind("ghost") is None


# -- 修复点 4：难度独立 -----------------------------------------------------

def test_normal_and_heroic_have_independent_lockouts():
    svc = make_service()
    keys = difficulty_split("d1")
    assert keys["normal"] != keys["heroic"]
    svc.account_lock("acct-leader", "d1", "normal")
    assert svc.is_locked("leader", "d1", "normal") is True
    # 打了普通不影响英雄
    assert svc.is_locked("leader", "d1", "heroic") is False
    svc.account_lock("acct-leader", "d1", "heroic")
    assert svc.is_locked("leader", "d1", "heroic") is True


# -- 修复点 5：奖励一次 -----------------------------------------------------

def test_reward_once_blocks_duplicate_claim():
    svc = make_service()
    party = svc.party_join("p1", "leader")
    lockout = svc.party_share(party, "d1", "normal")
    svc.kill_boss(lockout.key, "b1")
    first = svc.reward_once(lockout.key, "b1")
    assert first["granted"] is True
    assert svc.reward_flag(lockout.key, "b1") is True
    # 同一场重复击杀后再次领取 → 拒绝
    svc.kill_boss(lockout.key, "b1")
    with pytest.raises(RewardAlreadyClaimed):
        svc.reward_once(lockout.key, "b1")
    # 另一个 boss 仍可领
    assert svc.reward_once(lockout.key, "b2")["granted"] is True


# -- 修复点 6：在线重置，无需重登 -------------------------------------------

def test_online_reset_clears_expired_lockouts_without_relogin():
    svc = make_service()
    lockout = svc.account_lock("acct-leader", "d1", "normal")
    # 把过期时间拨到过去，模拟跨周
    lockout.expires_at = datetime(2026, 11, 23, 0, 0, tzinfo=SH)
    now = datetime(2026, 11, 30, 10, 0, tzinfo=SH)
    result = svc.online_reset(now)
    assert lockout.key in result["cleared_lockouts"]
    assert "leader" in result["online_characters_refreshed"]
    assert svc.is_locked("leader", "d1", "normal", now=now) is False


# -- 修复点 7：全局唯一 ID，合服不冲突 --------------------------------------

def test_global_id_unique_across_servers_and_idempotent():
    svc = make_service()
    a = global_id("serverA", 1)
    b = global_id("serverB", 1)  # 同序号不同服务器
    assert a != b
    assert a.startswith("serverA-") and b.startswith("serverB-")
    # 同 (server, seq) 幂等
    assert global_id("serverA", 1) == a
    # 自增序列也不重复
    ids = {svc.global_id() for _ in range(100)}
    assert len(ids) == 100


# -- 修复点 8：账号级锁，小号无法绕过 ---------------------------------------

def test_account_lock_covers_all_characters_on_account():
    svc = make_service()
    svc.account_lock("acct-alt", "d1", "heroic")
    assert svc.is_locked("main", "d1", "heroic") is True
    # 换同账号小号依旧被拦
    assert svc.is_locked("alt", "d1", "heroic") is True


# -- 修复点 9：离队释放占用，冷却保留 ---------------------------------------

def test_leave_releases_slot_but_keeps_lockout():
    svc = make_service()
    party = svc.party_join("p1", "leader")
    svc.party_join("p1", "mate")
    lockout = svc.party_share(party, "d1", "normal")
    instance_id = party.instance_id
    result = svc.leave_release("mate", party)
    assert result["retained_lockouts"]  # 冷却仍在
    assert "mate" not in svc.instances[instance_id].occupants
    assert svc.id_map("mate") is None  # 绑定已解除，别人可补位
    # 本周该账号仍不能再打
    assert svc.is_locked("mate", "d1", "normal") is True
    assert lockout.key in svc.lockouts


# -- 修复点 10：重置保护，不踢人不掉装 --------------------------------------

def test_reset_protected_instance_defers_reset_until_empty():
    svc = make_service()
    party = svc.party_join("p1", "leader")
    svc.party_join("p1", "mate")
    svc.party_share(party, "d1", "normal")
    instance_id = party.instance_id
    info = svc.reset_protect(instance_id, 600)
    assert info["protected"] is True
    assert set(info["occupants"]) == {"leader", "mate"}
    # 跨周重置：实例有人，重置被延迟而不是踢队
    lockout = svc.lockouts[party.lockout_key]
    lockout.expires_at = datetime(2026, 11, 23, 0, 0, tzinfo=SH)
    now = datetime(2026, 11, 30, 0, 0, tzinfo=SH)
    result = svc.online_reset(now)
    assert instance_id in result["deferred_instances"]
    assert party.lockout_key in svc.lockouts  # 进度还在，装备没掉
    # 队伍全员离开、实例关闭时补执行重置
    closed = svc.close_instance(instance_id, now)
    assert closed["deferred_reset_applied"] is True
    assert party.lockout_key not in svc.lockouts


def test_empty_instance_is_not_protected():
    svc = make_service()
    party = svc.party_join("p1", "leader")
    svc.party_share(party, "d1", "normal")
    iid = party.instance_id
    svc.leave_release("leader", party)
    info = svc.reset_protect(iid)
    assert info["protected"] is False


# -- 边界：本地零点整 -------------------------------------------------------

def test_reset_boundary_exactly_at_local_midnight():
    svc = make_service()
    # 北京时间周一 00:00:00 整（= UTC 周日 16:00）
    midnight = datetime(2026, 11, 23, 0, 0, tzinfo=SH)
    nxt = svc.next_reset(midnight)
    assert nxt == midnight  # 此刻即重置点，不再顺延一周


def test_server_timezone_negative_offset():
    # 美西 UTC-8：本地重置零点对应 UTC 16:00 当天
    now = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)  # 美西 11-22 12:00 周日
    info = server_timezone(-8, now)
    assert info["offset_hours"] == -8
    assert info["next_reset"].utcoffset() == timedelta(hours=-8)
    # 美西周一 00:00 (UTC-8) == UTC 周一 08:00
    assert info["next_reset"].astimezone(UTC) == datetime(
        2026, 11, 23, 8, 0, tzinfo=UTC
    )


# -- 端到端：题目中的完整事故链回放 -----------------------------------------

def test_full_incident_chain_end_to_end():
    svc = make_service()
    # 1) 队长带队进本，全队共享进度、同一全局唯一实例
    party = svc.party_join("raid", "leader")
    svc.party_join("raid", "mate")
    lockout = svc.party_share(party, "d1", "heroic")
    iid = party.instance_id
    assert iid and "-inst" in iid
    svc.kill_boss(lockout.key, "b1")

    # 2) 队友掉线重连：回到同一实例，进度还在
    svc.characters["mate"].online = False
    reconnected = svc.instance_bind("mate")
    assert reconnected.instance_id == iid
    assert "b1" in svc.lockouts[
        difficulty_key("d1", "heroic", "acct-mate")
    ].killed_bosses

    # 3) 奖励只发一次
    svc.reward_once(lockout.key, "b1")
    with pytest.raises(RewardAlreadyClaimed):
        svc.reward_once(lockout.key, "b1")

    # 4) 英雄锁了不影响普通；账号下小号也被锁
    assert svc.is_locked("leader", "d1", "heroic")
    assert not svc.is_locked("leader", "d1", "normal")
    svc.account_lock("acct-alt", "d1", "heroic")
    assert svc.is_locked("alt", "d1", "heroic")

    # 5) 踢人：位置释放、冷却保留
    svc.leave_release("mate", party)
    assert "mate" not in svc.instances[iid].occupants
    assert svc.is_locked("mate", "d1", "heroic")

    # 6) 跨周重置：在线玩家立即生效；仍在本里的队伍受保护
    lockout.expires_at = datetime(2026, 11, 23, 0, 0, tzinfo=SH)
    cross_week = datetime(2026, 11, 30, 0, 0, tzinfo=SH)
    report = svc.online_reset(cross_week)
    assert iid in report["deferred_instances"]
    svc.close_instance(iid, cross_week)
    assert not svc.is_locked("leader", "d1", "heroic", now=cross_week)
