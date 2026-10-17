"""工作区路径（D-6）：代码目录只读；配置、方案、视频、缓存、备份都在 $AUTOBIZ_HOME。

当前私有实例在 .env 里把 AUTOBIZ_HOME 指回仓库根（行为与整改前一致）；
粉丝安装默认 ~/AutoBiz，升级内核不冲突。
"""
from __future__ import annotations

import os


def home() -> str:
    """用户工作区根目录。"""
    return os.path.expanduser(os.environ.get("AUTOBIZ_HOME", "~/AutoBiz"))


def sub(*p) -> str:
    """工作区子目录（不存在则创建）。"""
    d = os.path.join(home(), *p)
    os.makedirs(d, exist_ok=True)
    return d
