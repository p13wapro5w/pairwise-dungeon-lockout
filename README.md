# pairwise-dungeon-lockout

## 领域

游戏、图形与多媒体

## 主题

副本冷却

## 说明

服务器时区、队伍共享与实例绑定。

初始状态下 `gameplay/dungeon_lockout.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
