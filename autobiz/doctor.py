"""环境体检（H-3）：`autobiz doctor` 逐项输出 ✓ / ✗ 与修复建议。

粉丝安装、升级、出问题时先跑这个；输出给人看，不进数据库。
"""
from __future__ import annotations

import os
import shutil


def run() -> list[tuple[bool, str, str]]:
    """返回 (通过?, 检查项, 说明/修复建议) 列表。"""
    out: list[tuple[bool, str, str]] = []

    def add(ok: bool, name: str, note: str):
        out.append((ok, name, note))

    # 1. 数据库可连接
    from autobiz import db
    try:
        c = db.connect()
        c.close()
        add(True, "数据库连接", db.url().split("@")[-1])
    except Exception as e:
        add(False, "数据库连接", f"{e} → 先 docker compose up -d")
        return out

    # 2. 扩展
    c = db.connect()
    try:
        exts = {r["extname"] for r in c.execute("SELECT extname FROM pg_extension").fetchall()}
        add("vector" in exts, "pgvector 扩展", "" if "vector" in exts else "重建数据库容器（pgvector/pgvector 镜像）")
        add("pg_trgm" in exts, "pg_trgm 扩展", "" if "pg_trgm" in exts else "autobiz migrate 会安装；失败则重建容器")

        # 3. 迁移
        from autobiz.migrate import pending
        pend = pending(c)
        add(not pend, "数据库迁移", "" if not pend else f"待应用 {pend} → uv run autobiz migrate")
        c.rollback()

        # 4. 本体
        try:
            from autobiz import ontology
            onto = ontology.load()
            add(True, "本体编译", f"v{onto.version}：{len(onto.object_types)} 类 / {len(onto.action_types)} 行动")
        except Exception as e:
            add(False, "本体编译", f"{e} → 检查 $AUTOBIZ_HOME/ontology/ 覆盖层 YAML")

        # 5. 向量维度一致（F-3）
        try:
            from autobiz.embed import dim
            col = c.execute("SELECT atttypmod dim FROM pg_attribute WHERE attrelid='objects'::regclass AND attname='embedding'").fetchone()
            add(bool(col) and col["dim"] == dim(), "向量维度",
                "" if col and col["dim"] == dim() else f"列 {col and col['dim']} ≠ 配置 {dim()} → 换模型需全量重算向量")
        except Exception as e:
            add(False, "向量维度", str(e))
        c.rollback()

        # 6. 模型 Key（只查当前任务路由实际用到的 provider）
        try:
            from autobiz.llm import models_config
            used = {t["provider"] for t in (models_config().get("tasks") or {}).values()}
            missing = [models_config()["providers"][p]["api_key_env"] for p in used
                       if not os.environ.get(models_config()["providers"][p]["api_key_env"])]
            add(not missing, "模型 API Key", "" if not missing else f"缺 {missing} → 写入 .env")
        except Exception as e:
            add(False, "模型 API Key", str(e))

        # 7. 今日花费
        try:
            spent = c.execute("SELECT COALESCE(sum(cost_cny),0) s FROM llm_usage WHERE at > current_date").fetchone()["s"]
            bud = float((models_config().get("budget") or {}).get("daily_cny", 0))
            add(bud <= 0 or float(spent) < bud, "今日模型花费",
                f"{float(spent):.2f} 元" + (f" / 预算 {bud:.2f}" if bud else "（未设预算）"))
            c.rollback()
        except Exception:
            add(True, "今日模型花费", "（llm_usage 表尚未建立，migrate 后启用）")

        # 8. 备份新鲜度
        from autobiz.paths import home
        bdir = os.path.join(home(), "backups")
        newest = None
        if os.path.isdir(bdir):
            files = [os.path.join(bdir, f) for f in os.listdir(bdir)]
            if files:
                newest = max(files, key=os.path.getmtime)
        if newest:
            import time
            age_h = (time.time() - os.path.getmtime(newest)) / 3600
            add(age_h < 36, "备份新鲜度", f"{os.path.basename(newest)}（{age_h:.0f} 小时前）"
                + ("" if age_h < 36 else " → 运行 scripts/backup.sh"))
        else:
            add(False, "备份新鲜度", "工作区 backups/ 为空 → 运行 scripts/backup.sh 并配置定时任务")

        # 9. 审批 PIN
        from autobiz.approve import pin_configured, pin_path
        add(pin_configured(), "审批 PIN", "" if pin_configured() else
            "未设置 → uv run autobiz setup-pin（单人自用可暂缓；分发 / 交给他人使用前必设）")

        # 10. 经营目标与飞书简报（ADR-0015，可选项）
        from autobiz.profile import goals
        g = goals()
        add(bool(g.get("monthly_revenue") or g.get("weekly_posts") or g.get("monthly_leads")), "经营目标",
            "已设置" if (g.get("monthly_revenue") or g.get("weekly_posts")) else "未设置 → 在 profile.yaml 的 goals 填写（可选）")
        add(bool(os.environ.get("FEISHU_BOT_WEBHOOK")), "飞书简报",
            "已配置（试推：uv run autobiz brief --send）" if os.environ.get("FEISHU_BOT_WEBHOOK")
            else "未配置 → .env 填 FEISHU_BOT_WEBHOOK / FEISHU_BOT_SECRET，再装 scripts/schedule 定时任务（可选）")

        # 11. 个人事实台账（ADR-0016）：没有台账时，内容里任何"我"的经历与数字都会被体检拦下
        from autobiz.content import claims_path, load_claims
        n = len(load_claims())
        add(n > 0, "个人事实台账", f"{n} 条" if n else f"未建立 → 复制 config/claims.example.yaml 到 {claims_path()} 并改成自己的经历（可选）")

        # 12. 磁盘
        try:
            total, used, free = shutil.disk_usage(home())
            add(free > 2 * 1024 ** 3, "磁盘余量", f"{free / 1024 ** 3:.1f} GB 可用（工作区所在盘）")
        except Exception:
            pass
    finally:
        c.close()
    return out


def onto_check_data(onto) -> list[str]:
    """F-2（基础版）：逐对象用当前本体校验，报告未知属性 / 非法枚举 / 未知类型 / 孤儿关系。只读。"""
    from autobiz import db
    issues: list[str] = []
    c = db.connect()
    try:
        for r in c.execute("SELECT id, type, title, props FROM objects").fetchall():
            t = onto.object_types.get(r["type"])
            if not t:
                issues.append(f"未知类型：{r['id']}（{r['title']}）")
                continue
            for k in r["props"]:
                if k not in t.properties:
                    issues.append(f"{t.label}「{r['title']}」有未知属性 {k}")
            for k, pt in t.properties.items():
                v = r["props"].get(k)
                if v is None or pt.base != "enum" or pt.is_list or not isinstance(v, str):
                    continue
                if v not in onto.enums.get(pt.enum, {}):
                    issues.append(f"{t.label}「{r['title']}」.{k}={v} 不在枚举 {pt.enum} 中")
        for r in c.execute("SELECT l.link_type, l.from_id, l.to_id FROM links l "
                           "LEFT JOIN objects a ON a.id=l.from_id LEFT JOIN objects b ON b.id=l.to_id "
                           "WHERE a.id IS NULL OR b.id IS NULL").fetchall():
            issues.append(f"孤儿关系：{r['link_type']} {r['from_id']} → {r['to_id']}")
    finally:
        c.close()
    return issues
