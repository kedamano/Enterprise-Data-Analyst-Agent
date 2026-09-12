"""生成 **分析师能力评测** 专用演示库：``data/sample_analyst.db``。

## 为什么需要它（不要删）

`app/eval/golden.py` 里 7 条 `requires_real=True` 的用例是按**一份业务数据集**写的，
而内置的 `data/sample_enterprise.db` 只有 `fact_sales(3120) + 3 张维表`、
字段仅 `revenue/orders/customers`。2026-09-12 真 LLM 重跑暴露：

    7 条 → 2 FINISH / 5 CLARIFY，断言通过率 0.0；
    且 工具成功率 0.0、平均报告长度 19.9、findings 0（没有一条真正基于数据产出结论）。

逐题对照后确认 **5 题要的字段在库里根本不存在**（转化率 / GMV / 8 个渠道 / 订单表），
模型对"无从查证的数字"选择反问（CLARIFY）是**正确行为**，不是缺陷。
详见 `docs/progress/pending-real.md` §C.1。

本脚本造一个**超集**库：既保留与 `sample_enterprise.db` 同构的 `fact_sales` 星型模型
（既有 golden 仍可跑），又补齐那 7 题需要的实体，并**刻意植入可判定的分析陷阱**。

## 与 `sample_enterprise.db` 的关系

- **独立文件**，不覆盖、不改动既有样例库（`tests/` 有 20 个文件硬依赖它）。
- 表结构是超集 ⇒ 所有 golden（含 `requires_real=False` 的 3 条）都能在这一个库上跑。
- 用法：`DATA_DB_URL=sqlite:///./data/sample_analyst.db` 跑 eval。

## 刻意植入的陷阱（每题一个，都可被判对/判错）

| 陷阱 | 服务用例 | 构造 |
|---|---|---|
| **辛普森悖论** | `r_simpson_check` | 总转化率 6.0%→7.0% **上升**，但**每个渠道的转化率都下降**（10.0→9.2 / 4.0→3.6），升的是"高转化渠道流量占比"（33%→60%） |
| **多组比较未校正** | `r_multiple_comparison` | **8 个渠道**，转化率分层明显（约 9–10% 与 3–4% 两组）；逐一两两比较必然假阳性 |
| **口径周期错配** | `r_caliber_period_mismatch` | 月度数据完整（24 个月），"本月 vs 上季度"是可判定的口径错配 |
| **分母缺失** | `r_ratio_denominator` | 转化率有明确分母（`visits`）；"提升 7%"需先问是**绝对百分点**还是**相对** |
| **拆解先于归因** | `r_decompose_before_attribution` | 2024-08 GMV 同比 **-12%**，但内部驱动分化（某品类/渠道下滑，另一部分增长）——不拆解就会归错因 |
| **相关≠因果** | `r_causal_overreach` | 某渠道营收下降 **且** 同期渠道结构变化（两者相关，但无因果证据） |
| **join 放大** | `r_join_amplification_guard` | `fact_orders`(订单行) 与 `dim_product` 关联；直接用 `fact_orders.gmv` 求和而不按品类聚合会重复计数 |

**确定性**：固定随机种子（`SEED = 2026`），任何机器上生成的数据逐行一致。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB = "data/sample_analyst.db"
SEED = 2026

# 8 个渠道（`r_multiple_comparison` 要"逐个比较 8 个渠道"）
CHANNELS = [
    (1, "直销"), (2, "合作伙伴"), (3, "线上商城"), (4, "电话销售"),
    (5, "内容种草"), (6, "短视频"), (7, "私域社群"), (8, "线下门店"),
]
# 按转化率分成两组：高转化老渠道(1-4) / 低转化新渠道(5-8) —— 辛普森悖论的"分层"依据
HIGH_CVR_CHANNELS = (1, 2, 3, 4)
LOW_CVR_CHANNELS = (5, 6, 7, 8)

REGIONS = [(1, "华东", "CN"), (2, "华北", "CN"), (3, "华南", "CN"), (4, "西部", "CN"), (5, "境外", "OVERSEAS")]
# 含 category（"订单表 JOIN 商品表统计各品类营收"要用它）
PRODUCTS = [
    (1, "企业版SaaS", "Software"), (2, "专业版SaaS", "Software"),
    (3, "硬件终端", "Hardware"), (4, "配件耗材", "Hardware"),
    (5, "咨询服务", "Service"), (6, "实施服务", "Service"),
    (7, "线上课程", "Training"), (8, "认证考试", "Training"),
]
SEGMENTS = [("大客户", 0.30), ("中小客户", 0.50), ("个人用户", 0.20)]

# 转化率：单位 %。**每个渠道 2024 都比 2023 低**（辛普森悖论的另一半）
_CVR_2023 = {"high": 10.0, "low": 4.0}
_CVR_2024 = {"high": 9.2, "low": 3.6}
# 流量结构：2023 高转化渠道只占 33%，2024 升到 60% → **总转化率反而上升**
_MIX_2023 = {1: 0.09, 2: 0.08, 3: 0.08, 4: 0.08, 5: 0.20, 6: 0.18, 7: 0.17, 8: 0.12}
_MIX_2024 = {1: 0.16, 2: 0.15, 3: 0.15, 4: 0.14, 5: 0.09, 6: 0.11, 7: 0.11, 8: 0.09}

START = date(2023, 1, 1)
END = date(2024, 12, 31)


def _months() -> list[date]:
    out, cur = [], START
    while cur <= END:
        out.append(cur)
        cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    return out


def _ddl(cur: sqlite3.Cursor) -> None:
    cur.executescript(
        """
        DROP TABLE IF EXISTS fact_sales;
        DROP TABLE IF EXISTS fact_orders;
        DROP TABLE IF EXISTS fact_traffic;
        DROP TABLE IF EXISTS dim_region;
        DROP TABLE IF EXISTS dim_product;
        DROP TABLE IF EXISTS dim_channel;
        DROP TABLE IF EXISTS dim_customer;

        CREATE TABLE dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT, country TEXT);
        CREATE TABLE dim_product (product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT);
        CREATE TABLE dim_channel (channel_id INTEGER PRIMARY KEY, channel_name TEXT);
        CREATE TABLE dim_customer (
            customer_id INTEGER PRIMARY KEY, customer_name TEXT, segment TEXT, region_id INTEGER
        );

        -- 与 sample_enterprise.fact_sales **同列**（既有 golden 可复用）
        CREATE TABLE fact_sales (
            sale_id INTEGER PRIMARY KEY, sale_date TEXT, region_id INTEGER,
            product_id INTEGER, channel_id INTEGER, revenue REAL,
            orders INTEGER, customers INTEGER
        );
        -- 订单表（r_join_amplification_guard / GMV 同比）
        CREATE TABLE fact_orders (
            order_id INTEGER PRIMARY KEY, order_date TEXT, region_id INTEGER,
            product_id INTEGER, channel_id INTEGER, customer_id INTEGER,
            gmv REAL, units INTEGER, is_refund INTEGER
        );
        -- 流量表：转化率 = conversions / visits（r_simpson_check / r_ratio_denominator）
        CREATE TABLE fact_traffic (
            traffic_id INTEGER PRIMARY KEY, stat_date TEXT, channel_id INTEGER,
            region_id INTEGER, visits INTEGER, orders INTEGER, conversions INTEGER
        );
        """
    )


def _dims(cur: sqlite3.Cursor, rng: random.Random) -> list[tuple]:
    cur.executemany("INSERT INTO dim_region VALUES (?,?,?)", REGIONS)
    cur.executemany("INSERT INTO dim_product VALUES (?,?,?)", PRODUCTS)
    cur.executemany("INSERT INTO dim_channel VALUES (?,?)", CHANNELS)

    customers = []
    cid = 1
    for seg, share in SEGMENTS:
        n = int(200 * share)
        for i in range(n):
            customers.append((cid, f"{seg}{i + 1:03d}", seg, rng.choice([r[0] for r in REGIONS])))
            cid += 1
    cur.executemany("INSERT INTO dim_customer VALUES (?,?,?,?)", customers)
    return customers


def _fact_orders(cur: sqlite3.Cursor, rng: random.Random, customers: list[tuple]) -> int:
    """订单表：2024-08 GMV 同比 2023-08 约 -12%，且**内部驱动分化**（便于拆解）。"""
    rows, oid = [], 1
    for d in _months():
        # 月度订单量：平稳 + 年末旺季
        base_orders = 900
        if d.month in (11, 12):
            base_orders = int(base_orders * 1.35)
        n_orders = int(base_orders * rng.uniform(0.9, 1.1))

        # 2024-08 整体同比约 -12%：总量下调，但只砍 Software/Service 两个品类，
        # Hardware/Training 反而增长 → 不拆解品类就会归错因。
        month_factor = 1.0
        if d.year == 2024 and d.month == 8:
            month_factor = 0.913

        for _ in range(n_orders):
            pid, _, cat = rng.choice(PRODUCTS)
            cat_factor = 1.0
            if d.year == 2024 and d.month == 8:
                cat_factor = {"Software": 0.62, "Service": 0.66,
                              "Hardware": 1.28, "Training": 1.22}[cat]
            cust = rng.choice(customers)
            gmv = round(rng.uniform(800, 12000) * month_factor * cat_factor, 2)
            rows.append((oid, d.isoformat(), cust[3], pid,
                         rng.choice([c[0] for c in CHANNELS]), cust[0],
                         gmv, rng.randint(1, 30), 1 if rng.random() < 0.04 else 0))
            oid += 1
    cur.executemany("INSERT INTO fact_orders VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _fact_traffic(cur: sqlite3.Cursor, rng: random.Random) -> int:
    """流量表：**总转化率上升、每渠道转化率下降**（辛普森悖论）。

    注意：自检不看这里的内部累加值，而是回读**库内**数据重算（见 ``main()``），
    因为落到 4 个区域时的整除会带来微小偏差。
    """
    rows, tid = [], 1
    for d in _months():
        year = d.year
        cvr_base = _CVR_2023 if year == 2023 else _CVR_2024
        mix = _MIX_2023 if year == 2023 else _MIX_2024
        total_visits = 200_000 if year == 2023 else 240_000
        for ch_id in [c[0] for c in CHANNELS]:
            visits = int(total_visits * mix[ch_id] * rng.uniform(0.95, 1.05))
            group = "high" if ch_id in HIGH_CVR_CHANNELS else "low"
            cvr = cvr_base[group] * rng.uniform(0.96, 1.04) / 100.0
            for region in [r[0] for r in REGIONS[:4]]:
                v = visits // 4
                c = int(v * cvr)
                rows.append((tid, d.isoformat(), ch_id, region, v, int(c * 0.98), c))
                tid += 1
    cur.executemany("INSERT INTO fact_traffic VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _fact_sales(cur: sqlite3.Cursor, rng: random.Random) -> int:
    """与 `sample_enterprise.db` 同构的星型事实表（周粒度，含华北下半年下滑）。"""
    rows, sid = [], 1
    start = START
    weeks = ((END - START).days // 7) + 1
    for week in range(weeks):
        d = start + timedelta(days=7 * week)
        for rid, _, _ in REGIONS:
            for pid, _, _ in PRODUCTS:
                for cid, _ in CHANNELS:
                    base = 50000 if rid != 5 else 30000
                    rev = base * rng.uniform(0.7, 1.4) / 42.0  # 渠道从 3→8，同量级缩放
                    if rid == 2 and d >= date(2024, 7, 1):     # 华北 2024 下半年下滑
                        rev *= 0.6
                    rows.append((sid, d.isoformat(), rid, pid, cid, round(rev, 2),
                                 rng.randint(20, 200), rng.randint(10, 120)))
                    sid += 1
    cur.executemany("INSERT INTO fact_sales VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def main(target: str | None = None) -> None:
    """生成演示库。``target`` 可选（测试可传 tmp_path，避免写进仓库）。"""
    db = target or DB
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    _ddl(cur)
    customers = _dims(cur, rng)
    n_orders = _fact_orders(cur, rng, customers)
    n_traffic = _fact_traffic(cur, rng)
    n_sales = _fact_sales(cur, rng)
    conn.commit()

    # --- 自检：把"陷阱"是否按设计**落库**打印出来（不是打印生成器内部变量） ---
    print(f"Analyst sample DB created: {db}")
    print(f"  fact_sales   {n_sales:>6} 行")
    print(f"  fact_orders  {n_orders:>6} 行（含 GMV，2023-2024 跨年）")
    print(f"  fact_traffic {n_traffic:>6} 行")
    print(f"  渠道 {len(CHANNELS)} 个 / 品类 {len({p[2] for p in PRODUCTS})} 个 / 客户 {len(customers)} 个")

    # [辛普森] 从**库内**数据算：整体 vs 分层，两年对比
    def _cvr(where: str) -> float:
        v, c = cur.execute(
            f"SELECT SUM(visits), SUM(conversions) FROM fact_traffic WHERE {where}").fetchone()
        return round(c / v * 100, 2) if v else 0.0

    hi = ",".join(str(c) for c in HIGH_CVR_CHANNELS)
    lo = ",".join(str(c) for c in LOW_CVR_CHANNELS)
    y23, y24 = "substr(stat_date,1,4)='2023'", "substr(stat_date,1,4)='2024'"
    print(f"  [辛普森] 整体转化率 2023 → 2024：{_cvr(y23)}% → {_cvr(y24)}%")
    print("           分层却**都下降**："
          f"高转化渠道 {_cvr(f'{y23} AND channel_id IN ({hi})')}% → "
          f"{_cvr(f'{y24} AND channel_id IN ({hi})')}%；"
          f"低转化渠道 {_cvr(f'{y23} AND channel_id IN ({lo})')}% → "
          f"{_cvr(f'{y24} AND channel_id IN ({lo})')}%")

    # [拆解] 2024-08 GMV 同比 + 品类分化（不拆解就会归错因）
    def _gmv_by_cat(ym: str) -> dict:
        return dict(cur.execute(
            "SELECT p.category, SUM(o.gmv) FROM fact_orders o JOIN dim_product p "
            "ON o.product_id = p.product_id WHERE substr(o.order_date,1,7) = ? "
            "GROUP BY p.category", (ym,)).fetchall())

    cur.execute("SELECT substr(order_date,1,7) AS m, SUM(gmv) FROM fact_orders "
                "WHERE substr(order_date,1,7) IN ('2023-08','2024-08') GROUP BY m")
    m = dict(cur.fetchall())
    if "2023-08" in m and "2024-08" in m:
        yoy = (m["2024-08"] - m["2023-08"]) / m["2023-08"] * 100
        print(f"  [拆解] 2024-08 GMV 同比 {yoy:+.1f}%（目标约 -12%）；品类分化：")
        now, before = _gmv_by_cat("2024-08"), _gmv_by_cat("2023-08")
        for cat in sorted(now):
            chg = (now[cat] - before.get(cat, 1)) / before[cat] * 100
            print(f"           {cat:<10} {chg:+6.1f}%")
    conn.close()


if __name__ == "__main__":
    main()
