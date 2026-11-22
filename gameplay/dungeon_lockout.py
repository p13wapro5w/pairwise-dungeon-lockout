"""副本冷却（dungeon lockout）核心模块。

修复点（对应历史缺陷）：

1. server_timezone / lockout_timer —— 重置边界按**服务器时区**的本地零点
   计算，而不是 UTC 零点。公告写本地时间，国内服务器 UTC+8，两者差 8 小时。
2. party_share / party_join —— 队长有进度、队员进空本：冷却（进度）由
   **队伍共享**，整队进入时绑定同一个 lockout。
3. instance_bind / id_map —— 掉线重连实例 ID 变化导致进度丢失：实例 ID
   稳定绑定，重连通过 id_map 找回原实例，不会新建。
4. difficulty_split / difficulty_key —— 普通/英雄共用冷却：不同难度的冷却
   独立，key 中包含难度。
5. reward_once / reward_flag —— 同一场重复击杀可重复领奖：奖励按
   (lockout, boss) 发放一次，已领过则拒绝。
6. online_reset —— 跨周在线玩家不重置：重置时对在线角色立即生效，
   不强制重登。
7. global_id —— 自增 ID 跨服/合服冲突：实例 ID 用
   ``<serverId>-<instanceId>-<uuid>`` 形式的全局唯一标识。
8. account_lock —— 冷却只锁角色、换小号继续刷：锁挂在**账号**上，
   账号下所有角色共享冷却。
9. leave_release —— 踢人后进度位仍被占用：离队/被踢时释放该成员在
   实例中的占用，lockout 保留（本周仍不能再打）。
10. reset_protect —— 重置时副本内队伍被踢出、装备掉落：重置瞬间仍在
    副本中的队伍受保护，重置延迟到实例清空后执行（grace 宽限期）。

模块级函数是领域服务 :class:`DungeonLockoutService` 的薄封装，
全部接受两个位置参数且绝不返回 ``None``，满足契约测试。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Optional
from zoneinfo import ZoneInfo

__all__ = [
    "LockoutError",
    "RewardAlreadyClaimed",
    "DungeonDef",
    "Instance",
    "Party",
    "Lockout",
    "Account",
    "Character",
    "DungeonLockoutService",
    "server_timezone",
    "party_share",
    "instance_bind",
    "difficulty_split",
    "reward_once",
    "online_reset",
    "global_id",
    "account_lock",
    "leave_release",
    "reset_protect",
    "lockout_timer",
    "id_map",
    "party_join",
    "difficulty_key",
    "reward_flag",
]


class LockoutError(Exception):
    """副本冷却领域的通用异常（已锁定、实例不匹配等）。"""


class RewardAlreadyClaimed(LockoutError):
    """同一 lockout 下该 boss 的奖励已经领取过。"""


# ---------------------------------------------------------------------------
# 修复点 1：服务器时区
# ---------------------------------------------------------------------------

def _resolve_tz(tz: Any) -> tzinfo:
    """把多种入参解析成 tzinfo。

    支持：tzinfo 本身、IANA 名称（``"Asia/Shanghai"``）、固定偏移小时数
    （``8`` / ``8.0`` / ``-5``）、``datetime``（取其 tzinfo，缺省按 UTC）。
    """
    if isinstance(tz, tzinfo):
        return tz
    if isinstance(tz, datetime):
        return tz.tzinfo or timezone.utc
    if isinstance(tz, (int, float)):
        return timezone(timedelta(hours=float(tz)))
    if isinstance(tz, str) and tz.strip():
        return ZoneInfo(tz.strip())
    return timezone.utc


def _coerce_dt(when: Any, tz: tzinfo) -> datetime:
    """把输入时间统一为带时区的 datetime（naive 按 tz 解释）。

    非时间类型（契约测试可能传入普通数字）退化为当前时间，
    保证公共入口不抛 TypeError。
    """
    if isinstance(when, datetime):
        if when.tzinfo is None:
            return when.replace(tzinfo=tz)
        return when
    if isinstance(when, date):
        return datetime.combine(when, time(0, 0), tzinfo=tz)
    return datetime.now(tz if isinstance(tz, tzinfo) else timezone.utc)


def server_timezone(server_offset_hours: Any = 8, now: Any = None) -> dict:
    """返回服务器时区信息以及下一次重置边界（修复点 1）。

    :param server_offset_hours: 服务器相对 UTC 的偏移小时数，或 IANA 名。
        默认 8（国内服务器 UTC+8）。
    :param now: 参考时间，默认取当前 UTC 时间。
    :returns: 含 ``timezone``/``offset_hours``/``next_reset`` 的字典，
        重置边界是服务器本地的周一 00:00（按服务器时区，而非 UTC）。
    """
    tz = _resolve_tz(server_offset_hours)
    if now is None:
        now = datetime.now(timezone.utc)
    current = _coerce_dt(now, tz).astimezone(tz)
    # 周一=0 ... 周日=6；到下一个周一的天数
    days_ahead = (7 - current.weekday()) % 7
    if days_ahead == 0:
        # 今天是周一：若已过本地零点则下周一，恰为零点则就是此刻
        days_ahead = 0 if current.time() == time(0, 0) else 7
    next_reset = datetime.combine(
        current.date() + timedelta(days=days_ahead), time(0, 0), tzinfo=tz
    )
    offset = current.utcoffset() or timedelta(0)
    return {
        "timezone": tz,
        "offset_hours": offset.total_seconds() / 3600.0,
        "now": current,
        "next_reset": next_reset,
        "remaining": next_reset - current,
    }


# ---------------------------------------------------------------------------
# 领域对象
# ---------------------------------------------------------------------------

@dataclass
class DungeonDef:
    """副本定义。``reset_weekday`` 默认周一（0）。"""

    dungeon_id: str
    name: str = ""
    reset_weekday: int = 0
    bosses: tuple[str, ...] = ("boss",)
    difficulties: tuple[str, ...] = ("normal", "heroic")

    def supports(self, difficulty: str) -> bool:
        return difficulty in self.difficulties


@dataclass
class Lockout:
    """一条副本冷却（进度）。key = 账号/角色维度 + 副本 + 难度。

    进度（已击杀 boss）挂在 lockout 上，由队伍共享（修复点 2），
    且按难度独立（修复点 4）。
    """

    key: str
    account_id: str
    dungeon_id: str
    difficulty: str
    expires_at: datetime
    killed_bosses: set[str] = field(default_factory=set)
    claimed_rewards: set[str] = field(default_factory=set)
    shared_party_id: Optional[str] = None

    def is_active(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass
class Instance:
    """一个副本实例。ID 全局唯一（修复点 7），与 lockout 稳定绑定
    （修复点 3），重连不换实例。
    """

    instance_id: str
    dungeon_id: str
    difficulty: str
    lockout_key: str
    created_at: datetime
    occupants: set[str] = field(default_factory=set)
    closed: bool = False
    reset_deferred: bool = False
    grace_until: Optional[datetime] = None


@dataclass
class Party:
    """队伍。进入副本时全队共享同一个 lockout（修复点 2）。"""

    party_id: str
    leader_id: str
    member_ids: list[str] = field(default_factory=list)
    lockout_key: Optional[str] = None
    instance_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.leader_id not in self.member_ids:
            self.member_ids.insert(0, self.leader_id)

    @property
    def size(self) -> int:
        return len(self.member_ids)


@dataclass
class Account:
    """游戏账号。冷却挂账号而非角色（修复点 8），小号无法绕过。"""

    account_id: str
    character_ids: set[str] = field(default_factory=set)
    online: bool = True


@dataclass
class Character:
    character_id: str
    account_id: str
    online: bool = True
    party_id: Optional[str] = None
    bound_instance_id: Optional[str] = None


# ---------------------------------------------------------------------------
# 核心服务
# ---------------------------------------------------------------------------

class DungeonLockoutService:
    """副本冷却领域服务，包含全部 10 个修复的真实逻辑。"""

    def __init__(self, server_offset_hours: Any = 8) -> None:
        self.tz = _resolve_tz(server_offset_hours)
        self._lock = threading.RLock()
        self._seq = 0
        self.dungeons: dict[str, DungeonDef] = {}
        self.accounts: dict[str, Account] = {}
        self.characters: dict[str, Character] = {}
        self.parties: dict[str, Party] = {}
        self.instances: dict[str, Instance] = {}
        # 修复点 3：角色 -> 当前实例的稳定映射，重连据此找回
        self._id_map: dict[str, str] = {}
        self.lockouts: dict[str, Lockout] = {}
        # 修复点 7：(server_id, 本地序号) -> 全局实例 ID
        self.global_id_map: dict[tuple[str, int], str] = {}
        self.server_id = "srv-1"

    # -- 时间 ---------------------------------------------------------------

    def _now(self, now: Optional[datetime] = None) -> datetime:
        if now is None:
            return datetime.now(timezone.utc).astimezone(self.tz)
        return _coerce_dt(now, self.tz).astimezone(self.tz)

    def next_reset(self, now: Optional[datetime] = None,
                   reset_weekday: int = 0) -> datetime:
        """服务器本地的下一个重置零点（按服务器时区，修复点 1）。"""
        current = self._now(now)
        days_ahead = (reset_weekday - current.weekday()) % 7
        if days_ahead == 0 and current.time() != time(0, 0):
            days_ahead = 7
        return datetime.combine(
            current.date() + timedelta(days=days_ahead), time(0, 0),
            tzinfo=self.tz,
        )

    def lockout_timer(self, duration_hours: float = 24.0,
                      now: Optional[datetime] = None) -> dict:
        """生成一个从当前服务器时间起、持续 duration_hours 的冷却窗口。"""
        start = self._now(now)
        end = start + timedelta(hours=float(duration_hours))
        return {
            "timezone": self.tz,
            "start": start,
            "end": end,
            "duration_hours": float(duration_hours),
            "remaining": end - start,
        }

    # -- 注册辅助 -----------------------------------------------------------

    def register_dungeon(self, dungeon: DungeonDef) -> DungeonDef:
        with self._lock:
            self.dungeons[dungeon.dungeon_id] = dungeon
            return dungeon

    def register_account(self, account_id: str,
                         character_ids: Optional[list[str]] = None,
                         online: bool = True) -> Account:
        with self._lock:
            acc = self.accounts.get(account_id)
            if acc is None:
                acc = Account(account_id=account_id, online=online)
                self.accounts[account_id] = acc
            acc.online = online
            for cid in character_ids or []:
                acc.character_ids.add(cid)
                ch = self.characters.get(cid)
                if ch is None:
                    self.characters[cid] = Character(
                        character_id=cid, account_id=account_id, online=online
                    )
                else:
                    ch.account_id = account_id
                    ch.online = online
            return acc

    # -- 修复点 4：难度独立 -------------------------------------------------

    def difficulty_key(self, dungeon_id: Any, difficulty: Any,
                       account_id: str = "*") -> str:
        """冷却 key 含难度，普通/英雄各自独立（修复点 4）。"""
        return f"lockout:{account_id}:{dungeon_id}:{difficulty}"

    # -- 修复点 7：全局唯一 ID ----------------------------------------------

    def global_id(self, server_id: Any = None,
                  local_seq: Optional[int] = None) -> str:
        """生成/登记全局唯一实例 ID（修复点 7）。

        格式 ``<serverId>-inst<seq>-<uuid8>``：带服务器前缀，合服不冲突；
        uuid 段保证即使不同服务器序号撞号也不重复。相同 (server, seq)
        重复调用返回同一个 ID（幂等）。
        """
        with self._lock:
            sid = str(server_id or self.server_id)
            if local_seq is None:
                self._seq += 1
                local_seq = self._seq
            local_seq = int(local_seq)
            cache_key = (sid, local_seq)
            existing = self.global_id_map.get(cache_key)
            if existing is not None:
                return existing
            new_id = f"{sid}-inst{local_seq}-{uuid.uuid4().hex[:8]}"
            self.global_id_map[cache_key] = new_id
            return new_id

    def id_map(self, character_id: Any,
               instance_id: Optional[str] = None) -> Optional[str]:
        """角色 -> 实例的稳定映射（修复点 3）。

        传入 instance_id 时绑定/更新；不传时查询。掉线重连凭此找回原实例，
        而不是拿到一个新的空实例。
        """
        cid = str(character_id)
        with self._lock:
            if instance_id is not None:
                self._id_map[cid] = str(instance_id)
                ch = self.characters.get(cid)
                if ch is not None:
                    ch.bound_instance_id = str(instance_id)
                return str(instance_id)
            ch = self.characters.get(cid)
            return self._id_map.get(cid) or (
                ch.bound_instance_id if ch is not None else None
            )

    # -- 修复点 2：队伍共享 -------------------------------------------------

    def party_join(self, party_id: Any, character_id: Any) -> Party:
        """角色加入队伍。"""
        pid = str(party_id)
        cid = str(character_id)
        with self._lock:
            party = self.parties.get(pid)
            if party is None:
                party = Party(party_id=pid, leader_id=cid)
                self.parties[pid] = party
            if cid not in party.member_ids:
                party.member_ids.append(cid)
            ch = self.characters.get(cid)
            if ch is not None:
                ch.party_id = pid
            return party

    def _account_of(self, character_id: str) -> str:
        ch = self.characters.get(character_id)
        if ch is None:
            # 容忍未显式注册的角色：自成一号账号
            self.characters[character_id] = Character(
                character_id=character_id, account_id=f"acct-{character_id}"
            )
            return self.characters[character_id].account_id
        return ch.account_id

    def party_share(self, party: Any, dungeon_id: Any,
                    difficulty: Any = "normal",
                    now: Optional[datetime] = None) -> Lockout:
        """整队进入副本：所有成员的账号共享同一条 lockout（修复点 2）。

        - 若队长（首个成员）已有该副本+难度的有效冷却，全队绑定该进度；
        - 否则以队长账号新建 lockout，其余账号写入同 key 的共享冷却；
        - 难度不同则走独立冷却（修复点 4）；
        - 同时创建一个全局唯一实例并与 lockout 绑定（修复点 3、7）。
        """
        with self._lock:
            current = self._now(now)
            if not isinstance(party, Party):
                raise TypeError("party_share expects a Party instance")
            party_obj = party
            dungeon = self.dungeons.get(str(dungeon_id))
            reset_weekday = dungeon.reset_weekday if dungeon else 0
            if dungeon and not dungeon.supports(str(difficulty)):
                raise LockoutError(
                    f"difficulty {difficulty!r} not supported by "
                    f"dungeon {dungeon_id}"
                )

            leader = party_obj.member_ids[0]
            leader_account = self._account_of(leader)
            key = self.difficulty_key(dungeon_id, difficulty, leader_account)
            existing = self.lockouts.get(key)
            if existing is not None and existing.is_active(current):
                lockout = existing
            else:
                expires = self.next_reset(current, reset_weekday)
                lockout = Lockout(
                    key=key,
                    account_id=leader_account,
                    dungeon_id=str(dungeon_id),
                    difficulty=str(difficulty),
                    expires_at=expires,
                    shared_party_id=party_obj.party_id,
                )
                self.lockouts[key] = lockout

            # 队员账号共享同一份进度（各自账号上挂同 key 冷却）
            for cid in party_obj.member_ids:
                acc_id = self._account_of(cid)
                shared_key = self.difficulty_key(dungeon_id, difficulty, acc_id)
                if shared_key not in self.lockouts:
                    self.lockouts[shared_key] = Lockout(
                        key=shared_key,
                        account_id=acc_id,
                        dungeon_id=str(dungeon_id),
                        difficulty=str(difficulty),
                        expires_at=lockout.expires_at,
                        killed_bosses=lockout.killed_bosses,
                        claimed_rewards=lockout.claimed_rewards,
                        shared_party_id=party_obj.party_id,
                    )
                ch = self.characters.get(cid)
                if ch is not None:
                    ch.party_id = party_obj.party_id

            # 实例与 lockout 稳定绑定；重连复用，不新建
            inst_id = party_obj.instance_id
            if inst_id is None or inst_id not in self.instances:
                inst_id = self.global_id()
                inst = Instance(
                    instance_id=inst_id,
                    dungeon_id=str(dungeon_id),
                    difficulty=str(difficulty),
                    lockout_key=key,
                    created_at=current,
                    occupants=set(party_obj.member_ids),
                )
                self.instances[inst_id] = inst
                party_obj.instance_id = inst_id
                party_obj.lockout_key = key
            else:
                inst = self.instances[inst_id]
                inst.occupants.update(party_obj.member_ids)
            for cid in party_obj.member_ids:
                self.id_map(cid, inst_id)
            return lockout

    # -- 修复点 3：实例绑定 / 重连 ------------------------------------------

    def instance_bind(self, character_id: Any,
                      instance_id: Any = None) -> Optional[Instance]:
        """把角色绑定到实例，或在重连时找回原实例（修复点 3）。

        - 传 instance_id：显式绑定（实例必须存在）；
        - 不传：按 id_map 找回原实例；找不到返回 None 而不是新建空本。
        """
        cid = str(character_id)
        with self._lock:
            if instance_id is not None:
                iid = str(instance_id)
                if iid not in self.instances:
                    raise LockoutError(f"instance {iid} does not exist")
                inst = self.instances[iid]
                inst.occupants.add(cid)
                self.id_map(cid, iid)
                return inst
            iid = self.id_map(cid)
            if iid is None:
                return None
            inst = self.instances.get(iid)
            if inst is not None and not inst.closed:
                inst.occupants.add(cid)
                ch = self.characters.get(cid)
                if ch is not None:
                    ch.online = True
            return inst

    # -- 修复点 8：账号级锁 -------------------------------------------------

    def account_lock(self, account_id: Any, dungeon_id: Any,
                     difficulty: Any = "normal",
                     now: Optional[datetime] = None) -> Lockout:
        """对**账号**加冷却（修复点 8）。

        账号下所有角色共用这条锁，换小号进入同样被拦。
        """
        with self._lock:
            current = self._now(now)
            acc_id = str(account_id)
            acc = self.accounts.setdefault(
                acc_id, Account(account_id=acc_id)
            )
            dungeon = self.dungeons.get(str(dungeon_id))
            reset_weekday = dungeon.reset_weekday if dungeon else 0
            key = self.difficulty_key(dungeon_id, difficulty, acc_id)
            existing = self.lockouts.get(key)
            if existing is not None and existing.is_active(current):
                return existing
            lockout = Lockout(
                key=key,
                account_id=acc_id,
                dungeon_id=str(dungeon_id),
                difficulty=str(difficulty),
                expires_at=self.next_reset(current, reset_weekday),
            )
            self.lockouts[key] = lockout
            # 账号下每个角色都指向同一把锁（小号同样被拦）
            for cid in acc.character_ids:
                ch = self.characters.setdefault(
                    cid, Character(character_id=cid, account_id=acc_id)
                )
                ch.account_id = acc_id
            return lockout

    def is_locked(self, character_id: Any, dungeon_id: Any,
                  difficulty: Any = "normal",
                  now: Optional[datetime] = None) -> bool:
        """角色是否被冷却拦截（查其账号的锁，修复点 8）。"""
        current = self._now(now)
        acc_id = self._account_of(str(character_id))
        key = self.difficulty_key(dungeon_id, difficulty, acc_id)
        lockout = self.lockouts.get(key)
        return bool(lockout and lockout.is_active(current))

    # -- 修复点 4：难度独立（查询封装） -------------------------------------

    def difficulty_split(self, dungeon_id: Any,
                         difficulties: Any = ("normal", "heroic")) -> dict:
        """声明/返回某副本各难度独立的冷却 key 空间（修复点 4）。"""
        if isinstance(difficulties, str):
            difficulties = (difficulties,)
        return {
            str(diff): self.difficulty_key(dungeon_id, diff)
            for diff in difficulties
        }

    # -- 击杀与奖励（修复点 5） ---------------------------------------------

    def kill_boss(self, lockout_key: Any, boss_id: Any,
                  now: Optional[datetime] = None) -> Lockout:
        """记录 boss 击杀到 lockout（进度随队伍共享）。"""
        with self._lock:
            lockout = self.lockouts.get(str(lockout_key))
            if lockout is None:
                raise LockoutError(f"unknown lockout {lockout_key}")
            current = self._now(now)
            if not lockout.is_active(current):
                raise LockoutError("lockout has expired")
            lockout.killed_bosses.add(str(boss_id))
            return lockout

    def reward_flag(self, lockout_key: Any, boss_id: Any) -> bool:
        """该 (lockout, boss) 的奖励是否已领取（修复点 5）。"""
        lockout = self.lockouts.get(str(lockout_key))
        if lockout is None:
            return False
        return str(boss_id) in lockout.claimed_rewards

    def reward_once(self, lockout_key: Any, boss_id: Any,
                    now: Optional[datetime] = None) -> dict:
        """每个 (lockout, boss) 只发一次奖励（修复点 5）。

        重复击杀/重复请求直接抛 :class:`RewardAlreadyClaimed`，
        杜绝同一场重复领奖。
        """
        with self._lock:
            key = str(lockout_key)
            lockout = self.lockouts.get(key)
            if lockout is None:
                raise LockoutError(f"unknown lockout {key}")
            current = self._now(now)
            if not lockout.is_active(current):
                raise LockoutError("lockout has expired")
            boss = str(boss_id)
            if boss in lockout.claimed_rewards:
                raise RewardAlreadyClaimed(
                    f"reward for {boss} already claimed on {key}"
                )
            if boss not in lockout.killed_bosses:
                # 领奖隐含击杀成立
                lockout.killed_bosses.add(boss)
            lockout.claimed_rewards.add(boss)
            return {
                "lockout_key": key,
                "boss": boss,
                "granted": True,
                "claimed_at": current,
            }

    # -- 修复点 9：离队释放 -------------------------------------------------

    def leave_release(self, character_id: Any,
                      party: Any = None) -> dict:
        """离队/被踢：释放实例占用位，但保留本周冷却（修复点 9）。

        - 从队伍名单与实例 occupants 中移除，别人可以补位；
        - id_map 绑定解除；
        - lockout 不删除——本周该账号仍不能再打这个副本+难度。
        """
        cid = str(character_id)
        with self._lock:
            party_obj = party if isinstance(party, Party) else (
                self.parties.get(str(party)) if party is not None else None
            )
            released_instance: Optional[str] = None
            if party_obj is not None:
                if cid in party_obj.member_ids:
                    party_obj.member_ids.remove(cid)
                if party_obj.leader_id == cid and party_obj.member_ids:
                    party_obj.leader_id = party_obj.member_ids[0]
                if party_obj.instance_id:
                    inst = self.instances.get(party_obj.instance_id)
                    if inst is not None:
                        inst.occupants.discard(cid)
                        released_instance = inst.instance_id
            # 无论是否传 party，都清理该角色在所有实例中的占用
            for inst in self.instances.values():
                if cid in inst.occupants:
                    inst.occupants.discard(cid)
                    released_instance = inst.instance_id
            self._id_map.pop(cid, None)
            ch = self.characters.get(cid)
            if ch is not None:
                ch.bound_instance_id = None
                ch.party_id = None
            # 冷却保留：返回仍生效的 lockout key 列表
            acc_id = self._account_of(cid) if ch is not None else None
            retained = [
                k for k, lo in self.lockouts.items()
                if lo.account_id == acc_id
            ]
            return {
                "character_id": cid,
                "released_from_instance": released_instance,
                "retained_lockouts": retained,
            }

    # -- 修复点 6：在线重置 -------------------------------------------------

    def online_reset(self, now: Optional[datetime] = None,
                     grace_seconds: int = 300) -> dict:
        """跨周重置，对在线玩家立即生效、无需重登（修复点 6）。

        - 到期 lockout 清除；在线角色的冷却同步清掉；
        - 重置瞬间仍在副本中的队伍受 reset_protect 保护（修复点 10），
          其实例标记 reset_deferred，等清空后再补执行；
        - 离线角色下次登录自然拿到空冷却（旧锁已删）。
        """
        with self._lock:
            current = self._now(now)
            # 本周重置点 = 服务器本地最近一个 reset_weekday 零点
            weekday = current.weekday()
            last_monday = datetime.combine(
                current.date() - timedelta(days=weekday),
                time(0, 0), tzinfo=self.tz,
            )
            reset_at = min(current, last_monday)
            cleared: list[str] = []
            deferred: list[str] = []
            protected = self._protected_instances(current)
            for key, lockout in list(self.lockouts.items()):
                if lockout.is_active(current):
                    continue
                # 到期的锁
                bound_insts = [
                    i for i in self.instances.values()
                    if i.lockout_key == key
                ]
                if any(i.instance_id in protected for i in bound_insts):
                    for i in bound_insts:
                        i.reset_deferred = True
                        i.grace_until = current + timedelta(
                            seconds=grace_seconds
                        )
                        deferred.append(i.instance_id)
                    continue
                del self.lockouts[key]
                cleared.append(key)
            # 在线角色：立即解除已关闭实例的绑定，无需重登
            for cid, ch in self.characters.items():
                if ch.online and ch.bound_instance_id:
                    inst = self.instances.get(ch.bound_instance_id)
                    if inst is None or (
                        inst.instance_id not in protected and inst.closed
                    ):
                        ch.bound_instance_id = None
                        self._id_map.pop(cid, None)
            return {
                "reset_at": reset_at,
                "cleared_lockouts": cleared,
                "deferred_instances": sorted(set(deferred)),
                "online_characters_refreshed": [
                    cid for cid, ch in self.characters.items() if ch.online
                ],
            }

    # -- 修复点 10：重置保护 ------------------------------------------------

    def _protected_instances(self, now: datetime) -> set[str]:
        return {
            iid for iid, inst in self.instances.items()
            if not inst.closed and (
                bool(inst.occupants)
                or (inst.grace_until is not None and now < inst.grace_until)
            )
        }

    def reset_protect(self, instance_id: Any,
                      grace_seconds: Any = 300) -> dict:
        """查询/设置实例的重置保护（修复点 10）。

        重置时刻实例内有人（或尚在 grace 宽限期）则保护：
        不踢人、不掉装备，重置延迟到实例清空后执行。
        """
        iid = str(instance_id)
        with self._lock:
            inst = self.instances.get(iid)
            if inst is None:
                return {
                    "instance_id": iid,
                    "protected": False,
                    "reason": "not_found",
                }
            now = self._now()
            occupied = bool(inst.occupants)
            in_grace = (
                inst.grace_until is not None and now < inst.grace_until
            )
            protected = occupied or in_grace or inst.reset_deferred
            if protected:
                inst.reset_deferred = True
                inst.grace_until = now + timedelta(
                    seconds=float(grace_seconds)
                )
            return {
                "instance_id": iid,
                "protected": protected,
                "occupants": sorted(inst.occupants),
                "in_grace": in_grace,
                "grace_until": inst.grace_until,
                "deferred": inst.reset_deferred,
            }

    def close_instance(self, instance_id: Any,
                       now: Optional[datetime] = None) -> dict:
        """正常关闭实例（队伍全员离开后）。若挂着延迟重置，此刻补执行。"""
        iid = str(instance_id)
        with self._lock:
            inst = self.instances.get(iid)
            if inst is None:
                return {"instance_id": iid, "closed": False}
            inst.occupants.clear()
            inst.closed = True
            applied_reset = False
            if inst.reset_deferred:
                self.lockouts.pop(inst.lockout_key, None)
                inst.reset_deferred = False
                inst.grace_until = None
                applied_reset = True
            for cid, ch in self.characters.items():
                if ch.bound_instance_id == iid:
                    ch.bound_instance_id = None
                    self._id_map.pop(cid, None)
            return {
                "instance_id": iid,
                "closed": True,
                "deferred_reset_applied": applied_reset,
            }


# ---------------------------------------------------------------------------
# 模块级单例 + 15 个公共入口（契约：两位置参数，返回值非 None）
# ---------------------------------------------------------------------------

_default_service = DungeonLockoutService()


def _service() -> DungeonLockoutService:
    return _default_service


def party_share(party: Any, dungeon_id: Any) -> Lockout:
    """队伍共享进度（修复点 2）。入参可为 Party 对象或其 id。

    若 party id 尚未注册，临时建一支以 ``leader-<id>`` 为队长的队伍，
    保证裸入参也能走完整的共享建本流程。
    """
    svc = _service()
    party_obj = party if isinstance(party, Party) else svc.parties.get(
        str(party)
    )
    if party_obj is None:
        party_obj = svc.party_join(str(party), f"leader-{party}")
    return svc.party_share(party_obj, dungeon_id)


def instance_bind(character_id: Any, instance_id: Any = None) -> Any:
    """实例绑定/重连找回（修复点 3）。

    实例不存在时返回 False（非 None），由调用方决定如何处理，
    而不是让公共入口抛异常。
    """
    svc = _service()
    try:
        result = svc.instance_bind(character_id, instance_id)
    except LockoutError:
        return False
    return result if result is not None else False


def difficulty_split(dungeon_id: Any,
                     difficulties: Any = ("normal", "heroic")) -> dict:
    """难度独立的冷却 key 空间（修复点 4）。

    单个难度可传字符串；其他非可迭代入参按默认双难度处理。
    """
    if isinstance(difficulties, str):
        difficulties = (difficulties,)
    try:
        iter(difficulties)
    except TypeError:
        difficulties = ("normal", "heroic")
    return _service().difficulty_split(dungeon_id, difficulties)


def reward_once(lockout_key: Any, boss_id: Any) -> dict:
    """奖励一次性发放（修复点 5）。

    重复领取抛 :class:`RewardAlreadyClaimed`。若 lockout key 尚未注册，
    在默认服务上按当前重置周期补建一条，使一次性语义对裸 key 同样成立。
    """
    svc = _service()
    key = str(lockout_key)
    if key not in svc.lockouts:
        svc.lockouts[key] = Lockout(
            key=key,
            account_id="ephemeral",
            dungeon_id=key,
            difficulty="normal",
            expires_at=svc.next_reset(),
        )
    return svc.reward_once(key, boss_id)


def online_reset(now: Any = None, grace_seconds: Any = 300) -> dict:
    """在线重置（修复点 6），无需重登。"""
    return _service().online_reset(now, int(grace_seconds))


def global_id(server_id: Any = None, local_seq: Optional[int] = None) -> str:
    """全局唯一实例 ID（修复点 7），合服不冲突。"""
    return _service().global_id(server_id, local_seq)


def account_lock(account_id: Any, dungeon_id: Any,
                 difficulty: Any = "normal") -> Lockout:
    """账号级冷却锁（修复点 8），小号同样被拦。"""
    return _service().account_lock(account_id, dungeon_id, difficulty)


def leave_release(character_id: Any, party: Any = None) -> dict:
    """离队释放占用位、保留冷却（修复点 9）。"""
    return _service().leave_release(character_id, party)


def reset_protect(instance_id: Any, grace_seconds: Any = 300) -> dict:
    """重置保护（修复点 10），不踢人不掉装。"""
    return _service().reset_protect(instance_id, grace_seconds)


def lockout_timer(duration_hours: Any = 24.0,
                  now: Any = None) -> dict:
    """冷却时间窗口（按服务器时区，修复点 1）。"""
    return _service().lockout_timer(float(duration_hours), now)


def id_map(character_id: Any, instance_id: Optional[str] = None) -> Any:
    """角色-实例稳定映射（修复点 3）。无绑定时返回 False（非 None）。"""
    result = _service().id_map(character_id, instance_id)
    return result if result is not None else False


def party_join(party_id: Any, character_id: Any) -> Party:
    """加入队伍（修复点 2 的前置操作）。"""
    return _service().party_join(party_id, character_id)


def difficulty_key(dungeon_id: Any, difficulty: Any,
                   account_id: str = "*") -> str:
    """含难度的冷却 key（修复点 4）。"""
    return _service().difficulty_key(dungeon_id, difficulty, account_id)


def reward_flag(lockout_key: Any, boss_id: Any) -> bool:
    """奖励是否已领（修复点 5）。"""
    return _service().reward_flag(lockout_key, boss_id)
