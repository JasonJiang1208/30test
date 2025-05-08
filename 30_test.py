import re, pandas as pd, numpy as np

# ========= 0. 别名映射表（如有新列名只需在此补充） =========
alias_map = {
    # —— 通用 —— -------------------------------------------------
    "Control Point":               ["CTRL_PNT"],
    "Cooling Setpoint 1":          ["Cooling Setpoint1"],
    "Cooling Setpoint 2":          ["Cooling Setpoint2"],
    "Heating Setpoint":            ["Heating Setpoint 1", "Heating Setpoint1"],
    "Percent Total Capacity":      ["Total Percent Capacity",
                                    "Percent Total Capacity A",
                                    "Actual Capacity cir A/B"],
    "Total Operating Hours":       ["Machine Operating Hours"],
    "Outside Air Temperature":     ["External Temperature"],

    # —— 冷冻 / 冷凝水 -------------------------------------------
    "Entering Chilled Water":      ["Cooler Entering Fluid"],
    "Leaving Chilled Water":       ["Cooler Leaving Fluid"],
    "Entering Condenser Water":    ["Condenser Entering Fluid"],
    "Leaving Condenser Water":     ["Condenser Leaving Fluid"],

    # —— Cir / Cp / Comp ----------------------------------------
    "Saturated Suction Temp Cp":   ["Saturated Suction Temp Cir",
                                    "Saturated Suction Temp",
                                    "Saturated Suction Temp A/B"],
    "Saturated Condensing Tmp Cp": ["Saturated Condensing Tmp Cir",
                                    "Saturated Cond Tmp cir"],
    "Discharge Gas Temp Cp":       ["Discharge Gas Temp cir"],
    "Oil Press Difference Cp":     ["Oil Pressure DifferenceA/B"],
    "Motor Temperature Comp":      ["Motor Temperature cir"],
    "Discharge Pressure":          ["Discharge Pressure A/B",
                                    "Discharge Pressure Cir A",
                                    "Discharge Pressure Cir B"],

    # —— 压缩机小时 / 次数 ----------------------------------------
    "Compressor A1 Hours":  ["Compressor A1/A2/B1/... Hours",
                             "Compressor A/B Hours",
                             "Compressor A1 Hours"],
    "Compressor B1 Hours":  ["Compressor A1/A2/B1/... Hours",
                             "Compressor A/B Hours",
                             "Compressor B1 Hours"],
    "Compressor A1 Starts": ["Compressor A1/A2/B1/... Starts",
                             "Compressor A/B Starts",
                             "Compressor A1 Starts"],
    "Compressor B1 Starts": ["Compressor A1/A2/B1/... Starts",
                             "Compressor A/B Starts",
                             "Compressor B1 Starts"],
}

# ========= 读取报表 =========================================================
#df = pd.read_csv("downloadThu, 08 May 2025 05_51_17 GMT.csv", low_memory=False)
df = pd.read_csv("downloadThu, 08 May 2025 03_06_54 GMT.csv", low_memory=False)

#df = pd.read_csv("downloadThu, 08 May 2025 03_08_32 GMT.csv", low_memory=False)
df.columns = df.columns.str.strip()

# =========  别名 → 标准列 ====================================================
for std, alts in alias_map.items():
    if std not in df.columns:
        for alt in alts:
            if alt in df.columns:
                df[std] = df[alt]
                break

# =========  时间 & 数值清洗 ==================================================
df["DateTime"] = pd.to_datetime(df["DateTime"],
                                format="%m/%d/%Y %I:%M:%S %p",
                                errors="coerce")
df = df.sort_values("DateTime")

num_cols = ["Total Operating Hours",
            "Compressor A1 Hours", "Compressor B1 Hours",
            "Percent Total Capacity"]
for col in num_cols:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

if ("Percent Total Capacity" in df.columns and
        df["Percent Total Capacity"].dtype == object):
    df["Percent Total Capacity"] = (df["Percent Total Capacity"]
        .astype(str).str.replace('%', '', regex=False)
        .str.strip().replace('', np.nan).astype(float))

# 30min 滚动平均 (前置条件 1 用到)
df["PTC_30m_avg"] = (df.set_index("DateTime")["Percent Total Capacity"]
                       .rolling("30min").mean().values)

# =========  safe_get（带动态 Cir/Cp/Comp 替换） ===============================
def safe_get(row, key):
    def _try(col):
        try:    return float(row.get(col, np.nan))
        except: return np.nan

    v = _try(key)
    if not np.isnan(v): return v

    for alt in alias_map.get(key, []):
        v = _try(alt)
        if not np.isnan(v): return v

    # —— 动态列名：Cir / Cp / Comp --------------------------------------------
    m = re.match(r"(Saturated Suction Temp Cp |Saturated Condensing Tmp Cp )([AB])", key)
    if m:
        base, circ = m.groups()
        return _try(f"{base.replace('Cp ', 'Cir ')}{circ}")

    m = re.match(r"Discharge Gas Temp Cp ([AB]\d)", key)
    if m:
        return _try(f"Discharge Gas Temp cir {m.group(1)[0]}")

    m = re.match(r"(Oil Press Difference Cp|Motor Temperature Comp )([AB]\d)", key)
    if m:
        base, comp = m.groups(); circ = comp[0]
        return _try(f"{base.strip()} {circ}")

    return np.nan

def is_cooling(row):   # 0‑Cool, 1‑Heat, 2‑Auto ⇒ Auto 视作冷
    return str(row.get("Heat/Cool Select", 0)).strip() != "1"

# =========  两条前置条件 (失败直接退出) =======================================
pre_ok = False
if "Total Operating Hours" in df.columns:
    run_hrs = df["Total Operating Hours"]
    if run_hrs.max() - run_hrs.min() >= 20:                       # 前置①
        a1 = df.get("Compressor A1 Hours", pd.Series(dtype=float))
        b1 = df.get("Compressor B1 Hours", pd.Series(dtype=float))
        if max(a1.max()-a1.min(), b1.max()-b1.min()) >= 0.30*(run_hrs.max()-run_hrs.min()):  # 前置②
            pre_ok = True

if not pre_ok:
    df["Total Score"] = np.nan
    df.to_csv("13.csv", index=False)
    print("❌ 前置条件未通过，已导出空结果 13.csv")
    exit()

# =========  评分点位与权重 (完全对应 WC Screw 列) =============================
metrics = {
    # —— 制冷/制热设定点：|CP‑Setpoint| ≤2°C —— 权重 0
    "SetpointDiff": {
        "check": lambda r: (not np.isnan(cp := safe_get(r, "Control Point"))
                            and (
                                (is_cooling(r)  and not np.isnan(sp := safe_get(r, "Cooling Setpoint 1")))
                                or (not is_cooling(r) and not np.isnan(sp := safe_get(r, "Heating Setpoint")))
                            )
                            and abs(cp - sp) <= 2),
        "weight": 0,
    },

    # —— 蒸发器：进 / 出水温差 0.5‑11°C —— 权重 0
    "EWT_LWT_Diff": {
        "check": lambda r: (not np.isnan(e := safe_get(r, "Entering Chilled Water"))
                            and not np.isnan(l := safe_get(r, "Leaving Chilled Water"))
                            and 0.5 <= abs(e - l) <= 11),
        "weight": 0,
    },

    # —— 控制点 vs 出水温度 |CP‑LWT| ≤1°C —— 权重 1
    "CP_vs_LWT": {
        "check": lambda r: (not np.isnan(cp := safe_get(r, "Control Point"))
                            and not np.isnan(l := safe_get(r, "Leaving Chilled Water"))
                            and abs(cp - l) <= 1),
        "weight": 1,
    },

    # —— Circuit A / B：蒸发器趋近 (<4°C) —— 权重 2
    "EvapApproach_A": {
        "check": lambda r: (not np.isnan(l := safe_get(r, "Leaving Chilled Water"))
                            and not np.isnan(s := safe_get(r, "Saturated Suction Temp Cir A"))
                            and l - s < 4),
        "weight": 2,
    },
    "EvapApproach_B": {
        "check": lambda r: (not np.isnan(l := safe_get(r, "Leaving Chilled Water"))
                            and not np.isnan(s := safe_get(r, "Saturated Suction Temp Cir B"))
                            and l - s < 4),
        "weight": 2,
    },

    # —— Circuit A / B：冷凝器趋近 (<4°C) —— 权重 2
    "CondApproach_A": {
        "check": lambda r: (not np.isnan(l := safe_get(r, "Leaving Condenser Water"))
                            and not np.isnan(s := safe_get(r, "Saturated Condensing Tmp Cir A"))
                            and l - s < 4),
        "weight": 2,
    },
    "CondApproach_B": {
        "check": lambda r: (not np.isnan(l := safe_get(r, "Leaving Condenser Water"))
                            and not np.isnan(s := safe_get(r, "Saturated Condensing Tmp Cir B"))
                            and l - s < 4),
        "weight": 2,
    },

    # —— 排气过热度(ABS DisT‑SCT) ≥7°C – 只制冷 —— 权重 1
    "DisSuperheat_A": {
        "check": lambda r: (is_cooling(r)
                            and not np.isnan(d := safe_get(r, "Discharge Gas Temp Cp A1"))
                            and not np.isnan(s := safe_get(r, "Saturated Condensing Tmp Cir A"))
                            and abs(d - s) >= 7),
        "weight": 1,
    },
    "DisSuperheat_B": {
        "check": lambda r: (is_cooling(r)
                            and not np.isnan(d := safe_get(r, "Discharge Gas Temp Cp B1"))
                            and not np.isnan(s := safe_get(r, "Saturated Condensing Tmp Cir B"))
                            and abs(d - s) >= 7),
        "weight": 1,
    },

    # —— 饱和冷凝温度 – OAT (<30°C) —— 权重 2
    """
    "SCT_vs_OAT_A": {
        "check": lambda r: (is_cooling(r)
                            and not np.isnan(s := safe_get(r, "Saturated Condensing Tmp Cir A"))
                            and not np.isnan(o := safe_get(r, "Outside Air Temperature"))
                            and s < o + 30),
        "weight": 2,
    },
    "SCT_vs_OAT_B": {
        "check": lambda r: (is_cooling(r)
                            and not np.isnan(s := safe_get(r, "Saturated Condensing Tmp Cir B"))
                            and not np.isnan(o := safe_get(r, "Outside Air Temperature"))
                            and s < o + 30),
        "weight": 2,
    },
    """
    # —— EXV 位置 20%‑100% —— 权重 1
    "EXV_A": {
        "check": lambda r: (20 <= safe_get(r, "EXV Position Cir A") <= 100),
        "weight": 1,
    },
    "EXV_B": {
        "check": lambda r: (20 <= safe_get(r, "EXV Position Cir B") <= 100),
        "weight": 1,
    },
}

# —— 压缩机 A1/A2/B1/B2 指标（油压差、电机温度、排气压力、启停频次） —— -----
for comp in ["A1", "A2", "B1", "B2"]:
    # 油压差 <100kPa —— 权重 1
    metrics[f"Oil_{comp}"] = {
        "check": lambda r, c=comp: safe_get(r, f"Oil Press Difference Cp{c}") < 100,
        "weight": 1,
    }
    # 电机温度 <100°C —— 权重 1
    metrics[f"Motor_{comp}"] = {
        "check": lambda r, c=comp: safe_get(r, f"Motor Temperature Comp {c}") < 100,
        "weight": 1,
    }
    # 排气压力 572‑1655kPa —— 权重 1
    metrics[f"DisPress_{comp}"] = {
        "check": lambda r, c=comp:
            572 <= safe_get(r, f"Discharge Pressure {c}") <= 1655,
        "weight": 1,
    }
    # 启动频次 ≤6 次/小时 —— 权重 0 (记录用)
    metrics[f"StartsRate_{comp}"] = {
        "check": lambda r, c=comp:
            (h := safe_get(r, f"Compressor {c} Hours")) > 0
            and (s := safe_get(r, f"Compressor {c} Starts")) / h <= 6,
        "weight": 0,
    }

# ========= 得分 =========
total_score = total_weight = 0.0

for meta in metrics.values():
    w = meta["weight"]
    passed = df.apply(meta["check"], axis=1)
    valid = passed.notna()
    if valid.sum() == 0:
        continue
    rate = passed[valid].mean()
    total_score += rate * w
    total_weight += w

df["Total_Score"] = round(total_score / total_weight * 100, 2)


drop_cols = [c for c in ["Precondition_Passed"] if c in df.columns]
df.drop(columns=drop_cols, inplace=True)


df.to_csv("92.csv", index=False)
print(f"✅ success – Total Score = {df.loc[0, 'Total_Score']} ")


