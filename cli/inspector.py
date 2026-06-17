#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPU 壓痕檢測器 Python 主程式：線上工具 index.html 之本機 CLI 移植。
版本：v1.2（2026-06-14）；演算法對應線上工具 v2.2 論文操作點。

輸入：已裁切之壓痕影像（建議 512×512，crop_and_pretest_v2.py 之輸出）。
      單張檔案或資料夾（遞迴）皆可；路徑或檔名含 defect/bad/ng → 真值=defect，
      含 good/ok → 真值=good（與線上工具 labelOf 一致），其餘視為未標註。
判定：論文操作點（redness=18, darkT=130, margin=0.20, frame=0.30,
      blank=0.20, autoBBox=OFF），演算法與線上工具 analyze() 完全一致：
      240×240 → 48×48 接觸密度網格 → 內容區五分區（上/下/左/右/中）
      → 任一區空白率 > blank 即判 defect。
輸出：終端指標摘要＋誤判清單；--csv 逐張結果（欄位與線上工具「匯出 CSV」
      一致）；--json 完整結果；--overlay 每張缺失區標註圖（紅=缺、綠=正常）。

v1.1 新增：--thermal TIM 熱阻與 CPU 溫度推算（一階熱阻網路，模型推估非實測）。
  以影像之內容區平均接觸覆蓋率 C＝1−overallBlank 折減有效接觸面積
  Aeff＝A×C，依 Rth(TIM)＝d／(k×Aeff)、Tcpu＝Tamb＋P×(Rother＋Rth(TIM))
  推算各 TIM 厚度 d 與導熱係數 k 對應之 Rth 與 Tcpu（論文式 3.5、3.6）。
  若推估 Tcpu 超過警戒值（預設 95 °C），自內建 TIM 型錄（論文附錄四
  表附四-1／附四-3 原廠規格）重算各候選 TIM，凡滿足客戶規格
  Tcpu ≤ 目標值（預設 90 °C）者依溫度升冪列為建議選項。

v1.2 新增：--review 規則式＋Opus 邊界覆核（諮詢式）。
  以 251 張 benchmark 實測校準：規則式僅有的偽陽性皆落在「判 defect 且
  maxBlank 剛越過 20% 門檻」之邊界帶。--review 對此少數邊界案例呼叫 Opus
  視覺模型取「第二意見」並標記「待人工覆核」；★自動判定一律維持規則式結果，
  不自動翻面★——因模擬證實自動翻面會誤放真缺陷、破壞零漏檢（100% 召回）。
  此設計確保自動指標與純規則式完全相同（只升不降），AI 僅輔助人工挑出邊界
  偽陽性。預設純離線；--review 才會把邊界影像送 Anthropic API（需
  ANTHROPIC_API_KEY），無金鑰時降級為僅標出邊界待覆核清單（仍離線）。

⚠ 本程式等價於線上工具按「執行」。刻意不移植「訓練」（自動微調參數）——
  訓練會使參數偏離論文操作點，論文所有數據皆以固定操作點計算。

用法：
  python3 inspector.py ./review                      # 資料夾（路徑含真值→自動算指標）
  python3 inspector.py img.png --verbose             # 單張
  python3 inspector.py ./review --csv 結果.csv --overlay ./標註圖
  python3 inspector.py ./review --auto-bbox          # 自動內容區（論文設定為 OFF）
  python3 inspector.py ./review --thermal            # 檢測＋TIM 熱阻/Tcpu 推算
  python3 inspector.py ./review --thermal --tim TG-PCM095   # 指定型錄 TIM
  python3 inspector.py --thermal --coverage 0.5      # 無影像之情境推算（C=50%）
  python3 inspector.py ./review --review             # 規則式＋Opus 邊界覆核（諮詢式）
  python3 inspector.py --list-tims                   # 列出內建 TIM 型錄

需求：Python 3.10+、numpy、Pillow；--review 另需 anthropic 套件與 ANTHROPIC_API_KEY
"""
import os, re, math, csv, json, argparse
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

__version__ = "1.2"
VERSION_DATE = "2026-06-14"
ENGINE = "線上工具 v2.2 論文操作點"

GRID, ASZ = 48, 240
P_DEFAULT = dict(redness=18.0, darkT=130.0, margin=0.20, frame=0.30,
                 blank=0.20, autoBBox=False)
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
RESAMPLE = {"NEAREST": Image.NEAREST, "BILINEAR": Image.BILINEAR,
            "BICUBIC": Image.BICUBIC, "LANCZOS": Image.LANCZOS}
ZH = {"top": "上", "bottom": "下", "left": "左", "right": "右", "center": "中"}

def js_round(x): return math.floor(x + 0.5)   # JS Math.round

# ---------------- 五區覆蓋／空白計算（analyze 與 robust_analyze 共用） ----------------
def _zones_from_contact(contact, P):
    dens = contact.reshape(GRID, ASZ // GRID, GRID, ASZ // GRID).mean(axis=(1, 3))
    # 內容區：固定 margin，或 autoBBox（密度 >0.10 之格子 bbox）
    x0 = math.floor(GRID * P["margin"]); x1 = math.ceil(GRID * (1 - P["margin"]))
    y0, y1 = x0, x1
    if P.get("autoBBox"):
        ys, xs = np.nonzero(dens > 0.10)
        if len(xs):
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            y0, y1 = int(ys.min()), int(ys.max()) + 1
    cw, ch = x1 - x0, y1 - y0
    fwx = max(1, js_round(cw * P["frame"])); fwy = max(1, js_round(ch * P["frame"]))

    def rmean(rx0, ry0, rx1, ry1):
        sub = dens[ry0:ry1, rx0:rx1]
        return float(sub.mean()) if sub.size else 0.0
    cov = {"top": rmean(x0, y0, x1, y0 + fwy), "bottom": rmean(x0, y1 - fwy, x1, y1),
           "left": rmean(x0, y0 + fwy, x0 + fwx, y1 - fwy),
           "right": rmean(x1 - fwx, y0 + fwy, x1, y1 - fwy),
           "center": rmean(x0 + fwx, y0 + fwy, x1 - fwx, y1 - fwy)}
    blanks = {k: 1 - v for k, v in cov.items()}
    maxBlank = max(blanks.values())
    overallBlank = 1 - sum(cov.values()) / len(cov)
    return dict(cov=cov, blanks=blanks, maxBlank=maxBlank, overallBlank=overallBlank,
                label="defect" if maxBlank > P["blank"] else "good",
                bbox=dict(x0=x0, y0=y0, x1=x1, y1=y1, fwx=fwx, fwy=fwy))

# ---------------- 核心檢測（與線上工具 v2.1 analyze 一致；絕對門檻） ----------------
def analyze(pil_img, P=P_DEFAULT, resample=Image.NEAREST):
    img = pil_img.convert("RGB").resize((ASZ, ASZ), resample)
    a = np.asarray(img, dtype=np.float64)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    contact = (((r - (g + b) / 2.0) > P["redness"]) |
               ((0.299 * r + 0.587 * g + 0.114 * b) < P["darkT"])).astype(np.float64)
    return _zones_from_contact(contact, P)

# ---------------- 穩健化檢測（--robust）：自適應門檻，抗環境光/手機色差/曝光 ----------------
# 設計理由：原 analyze 用「絕對」redness/darkT，對拍攝條件敏感。實測發現，單純把背景
# 白平衡到中性會「乘法」壓掉壓痕本身的紅度訊號 → 良品誤檢（98.7%→89.7%）。故改採
# 兩段式自適應：
#   (1) 曝光正規化：以背景亮度均勻增益(uniform gain)拉到固定亮度→修過曝/不足，且
#       保留色彩比例（不動紅度比例）。
#   (2) 色偏校正：偵測紅度時「減去背景殘留紅度」(加法 offset，去暖/冷偏)；偵測暗度時
#       以背景亮度等比下移門檻。如此保留壓痕相對背景的「紅度超量／暗度落差」，去掉
#       拍攝條件造成的偏移。乾淨資料背景近中性、近曝光基準→近恆等，操作點結果不變。
# 乾淨壓痕紙背景之校準基準（依 28 張良品「真白」像素統計：bg_luma≈224、bg_red≈5）。
# 以此為基準→乾淨資料增益≈1、色偏修正≈0，robust 對乾淨資料近恆等，操作點結果不變。
BASE_LUMA, BASE_RED, ROBUST_CAP = 224.0, 5.0, 2.0   # cap=2：乾淨資料維持 98.8%/零漏檢之經驗最佳值
def robust_analyze(pil_img, P=P_DEFAULT, resample=Image.NEAREST, bg_pct=80.0,
                   use_gain=True, use_offset=True):
    # use_gain/use_offset 供消融分析(ablation)：分離「曝光增益」與「限幅去色偏」兩階段之貢獻。
    img = pil_img.convert("RGB").resize((ASZ, ASZ), resample)
    a = np.asarray(img, dtype=np.float64)
    luma0 = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    bgm = luma0 >= np.percentile(luma0, bg_pct)        # 最亮 (100-bg_pct)% ＝白紙背景
    gain = float(np.clip(BASE_LUMA / max(float(np.median(luma0[bgm])), 1.0), 0.5, 2.0)) \
        if (use_gain and int(bgm.sum()) >= 100) else 1.0  # (1) 均勻曝光增益→背景拉到基準亮度
    a = np.clip(a * gain, 0, 255)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    redness = r - (g + b) / 2.0
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    # 真白像素（亮且低紅度）估背景殘留紅度；僅修正「暖於乾淨基準」之色偏（單向：只提高
    # 門檻、不降低），避免在空白區把白紙雜訊誤判為接觸而漏檢（cool 方向交給暗度通道）。
    red_thr = P["redness"]
    if use_offset:
        white = (luma >= np.percentile(luma, bg_pct)) & (redness < P["redness"])
        bg_red = float(np.median(redness[white])) if int(white.sum()) >= 100 else BASE_RED
        red_thr = P["redness"] + min(max(bg_red - BASE_RED, 0.0), ROBUST_CAP)  # (2) 去暖色偏，限幅防過修
    contact = ((redness > red_thr) | (luma < P["darkT"])).astype(np.float64)
    return _zones_from_contact(contact, P)

# ---------------- 旋轉對齊（--deskew）：隨壓痕傾斜角度校正，仍用固定框 ----------------
# 實測(251張真實影像)：旋轉對齊 + 固定框 維持零漏檢、誤檢 3→2、準確率 98.8→99.2%（安全小贏）。
# ★注意：『框隨壓痕外形伸縮(autoBBox)』經實測會把良品邊緣誤判→誤檢暴增至 28(良品全滅)，故不採；
#   『置中/平移』會把邊緣缺失移進覆蓋區→新增漏檢，亦不採。僅採『旋轉對齊+固定框』。
def deskew(pil_img, P=P_DEFAULT, min_angle=0.8):
    try:
        import cv2
    except Exception:
        return pil_img.convert("RGB")
    a = np.asarray(pil_img.convert("RGB")); H, W = a.shape[:2]
    af = a.astype(np.float64); r, g, b = af[..., 0], af[..., 1], af[..., 2]
    mask = ((r - (g + b) / 2.0 > P["redness"]) | ((0.299*r+0.587*g+0.114*b) < P["darkT"]))
    ys, xs = np.nonzero(mask)
    if len(xs) < 80: return pil_img.convert("RGB")
    pts = np.column_stack([xs, ys]).astype(np.float32)
    (cx, cy), (rw, rh), ang = cv2.minAreaRect(pts)   # 接觸塊之最小面積旋轉矩形→傾斜角
    if ang < -45: ang += 90
    if ang > 45: ang -= 90
    if abs(ang) < min_angle: return pil_img.convert("RGB")  # 幾乎不傾斜→不轉
    M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    rot = cv2.warpAffine(a, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255))
    return Image.fromarray(rot)

# ---------------- 真值與 split（與線上工具 labelOf / parseDsPath 同精神） ----------------
def label_of(path_str):
    # 與線上工具 labelOf 同關鍵字；惟 ng/ok 改整詞比對並先去副檔名——
    # JS 版以子字串比對，「.png」含 ng 會把所有 png 誤標為 defect（已知陷阱）。
    s = re.sub(r"\.(png|jpe?g|webp|bmp)$", "", path_str.lower())
    if ("defect" in s or "bad" in s or re.search(r"(^|[^a-z])ng([^a-z]|$)", s)):
        return "defect"
    if "good" in s or re.search(r"(^|[^a-z])ok([^a-z]|$)", s):
        return "good"
    return None

def split_of(path_str):
    parts = [p.lower() for p in Path(path_str).parts]
    for sp in ("train", "val", "test"):
        if any(sp == q or q.startswith(sp + "_") for q in parts): return sp
    return ""

# ---------------- 指標（與線上工具 metrics 一致） ----------------
def metrics(recs):
    tp = sum(1 for r in recs if r["truth"] == "defect" and r["pred"] == "defect")
    tn = sum(1 for r in recs if r["truth"] == "good"   and r["pred"] == "good")
    fp = sum(1 for r in recs if r["truth"] == "good"   and r["pred"] == "defect")
    fn = sum(1 for r in recs if r["truth"] == "defect" and r["pred"] == "good")
    acc  = (tp + tn) / max(1, tp + tn + fp + fn)
    prec = tp / max(1, tp + fp); rec = tp / max(1, tp + fn)
    f1   = 2 * prec * rec / max(1e-9, prec + rec)
    iou  = tp / max(1, tp + fp + fn)
    return dict(TP=tp, TN=tn, FP=fp, FN=fn, acc=acc, prec=prec, rec=rec, f1=f1, iou=iou)

# ---------------- TIM 熱阻與 CPU 溫度推算（論文 §3.5 式 3.5、3.6；模型推估非實測） ----------------
# 型錄數值出處＝論文附錄四：表附四-1（T-Global 原廠 datasheet）、表附四-3（競品）、
# 表附四-2（類型參考範圍）。d 預設取型錄最薄厚度或類型範圍下限（最佳情形），可以 --tim-d 覆寫。
TIM_CATALOG = [
    # (型號, 類型, k W/m·K, 預設 BLT mm, 備註)
    ("Conductonaut", "液態金屬",        73.0, 0.02, "Thermal Grizzly；導電、腐蝕鋁，高風險"),
    ("Kryonaut",     "高階散熱膏",      12.5, 0.05, "Thermal Grizzly；BLT 取膏類範圍 0.02–0.1 代表值"),
    ("TG-PCM095",    "相變化 PCM",       9.5, 0.20, "T-Global；0.20/0.25 mm，相變 45 °C，無 pump-out"),
    ("PTM7950",      "相變化 PCM",       8.5, 0.25, "Honeywell；Rth≈0.04 °C·cm²/W"),
    ("MX-6",         "散熱膏",           7.5, 0.05, "Arctic；第三方測值 7.5–10.6 取下限"),
    ("散熱膏(代表)",  "散熱膏",           5.0, 0.05, "表附四-2 範圍 1–15；取系統熱場預設 k=5"),
    ("TG-ASD50AB",   "導熱凝膠",         5.0, 0.20, "T-Global；點塗，BLT 取凝膠範圍 0.2–3 下限"),
    ("TG-ASD35AB",   "導熱凝膠",         3.5, 0.20, "T-Global；點塗，BLT 取凝膠範圍 0.2–3 下限"),
    ("TG-A5000L",    "導熱矽膠片",       5.0, 0.50, "T-Global；低滲油，最薄 0.5 mm"),
    ("TG-A9000F",    "導熱矽膠片(玻纖)",  7.0, 1.00, "T-Global；≥8 kV/mm 絕緣，最薄 1.0 mm"),
    ("TG-AD30",      "超軟導熱矽膠片",    3.0, 0.50, "T-Global；Shore OO 20，最薄 0.5 mm"),
    ("TG-AD66",      "超軟導熱矽膠片",    6.5, 1.00, "T-Global；最薄 1.0 mm"),
    ("TG-AD75",      "超軟導熱矽膠片",    7.5, 1.00, "T-Global；最薄 1.0 mm"),
]

def tim_lookup(name):
    for t in TIM_CATALOG:
        if t[0].lower() == name.lower(): return t
    return None

def thermal_calc(k, d_mm, C, TH):
    """一階熱阻網路：Rth(TIM)=d/(k·A·C)（式3.5 之 TIM 項，Aeff=A×C）、
    Tcpu=Tamb+P×(Rother+Rth(TIM))（式3.6）。回傳 (Rth_TIM, Rth_total, Tcpu)。"""
    C = max(0.01, min(1.0, C))                      # 避免 C→0 發散
    a_m2 = TH["area"] * 1e-6                        # mm² → m²
    r_tim = (d_mm * 1e-3) / (k * a_m2 * C)          # °C/W
    r_tot = TH["rother"] + r_tim
    return r_tim, r_tot, TH["tamb"] + TH["power"] * r_tot

def thermal_recommend(C, TH):
    """同條件（覆蓋率 C、功耗、面積…）重算型錄各 TIM；回傳 (達標清單, 未達標清單)，
    達標＝Tcpu ≤ ttarget，依 Tcpu 升冪。"""
    ok, ng = [], []
    for name, typ, k, d, note in TIM_CATALOG:
        r_tim, r_tot, tcpu = thermal_calc(k, d, C, TH)
        rec = dict(name=name, type=typ, k=k, d=d, note=note,
                   r_tim=r_tim, r_total=r_tot, tcpu=tcpu)
        (ok if tcpu <= TH["ttarget"] else ng).append(rec)
    ok.sort(key=lambda r: r["tcpu"]); ng.sort(key=lambda r: r["tcpu"])
    return ok, ng

def print_thermal(recs, TH):
    """批次推算摘要：以最差樣本（覆蓋率最低）推估 Rth 與 Tcpu；
    超過警戒值 tlimit 時列出滿足 Tcpu ≤ ttarget 之 TIM 建議。"""
    print(f"\n── TIM 熱阻與 CPU 溫度推算（一階模型推估，非實測）──")
    print(f"條件：P={TH['power']:g} W  Tamb={TH['tamb']:g} °C  A={TH['area']:g} mm²  "
          f"Rother={TH['rother']:g} °C/W  警戒>{TH['tlimit']:g} °C  目標≤{TH['ttarget']:g} °C")
    print(f"現行 TIM：{TH['tim_name']}（k={TH['k']:g} W/m·K，BLT={TH['d']:g} mm）")
    if recs:
        worst = min(recs, key=lambda r: r["coverage"])
        cases = [("最差樣本 " + worst["path"], worst["coverage"])]
        best = max(recs, key=lambda r: r["coverage"])
        if best is not worst:
            cases.append(("最佳樣本 " + best["path"], best["coverage"]))
    else:
        cases = [(f"情境推算（--coverage）", TH["coverage"])]
    for label, C in cases:
        r_tim, r_tot, tcpu = thermal_calc(TH["k"], TH["d"], C, TH)
        mark = "⚠ 超過警戒" if tcpu > TH["tlimit"] else ("△ 介於目標與警戒間" if tcpu > TH["ttarget"] else "✓ 達標")
        print(f"{label}：C={C*100:.1f}% → Aeff={TH['area']*C:.0f} mm²  "
              f"Rth(TIM)={r_tim:.3f}  Rth(total)={r_tot:.3f} °C/W  Tcpu≈{tcpu:.1f} °C  {mark}")
    # 以最差（或情境）覆蓋率觸發建議
    C0 = cases[0][1]
    if C0 < 0.20:
        print("（注意：C < 20% 已屬一階模型之外插範圍，絕對溫度僅供相對比較；"
              "此情形應優先依 RCA 改善接觸，而非僅更換 TIM）")
    _, _, tcpu0 = thermal_calc(TH["k"], TH["d"], C0, TH)
    if tcpu0 > TH["tlimit"]:
        ok, ng = thermal_recommend(C0, TH)
        print(f"\n推估 Tcpu {tcpu0:.1f} °C 超過警戒 {TH['tlimit']:g} °C → "
              f"建議改用下列 TIM（同條件 C={C0*100:.1f}% 重算，Tcpu ≤ {TH['ttarget']:g} °C，升冪）：")
        if not ok: print("  （無型錄 TIM 達標——須先改善接觸覆蓋率／降低功耗，再行選型）")
        for i, r in enumerate(ok, 1):
            print(f"  {i}. {r['name']}（{r['type']}，k={r['k']:g}，BLT={r['d']:g} mm）→ "
                  f"Rth(TIM)={r['r_tim']:.3f} °C/W，Tcpu≈{r['tcpu']:.1f} °C ✓｜{r['note']}")
        if ng:
            print("  未達標（不建議）：" + "、".join(f"{r['name']} {r['tcpu']:.1f}°C" for r in ng))
        print("  ※ 建議優先依 RCA 改善接觸根因（覆蓋率），再以高 k、低 BLT 之 TIM 補強。")
    print("※ 模型：式(3.5) Rth=Rother+d/(k·A·C)、式(3.6) Tcpu=Tamb+P×Rth；"
          "推估值非實測，參數請依實際平台校準（預設依論文 §4.10 PTL 40 W 平台推估）。")

# ---------------- RCA 根因分析（依五區壓痕型態推論成因；沿用 A/B/C 缺陷分類） ----------------
# A=中央接觸不良、B=邊緣缺失、C=良品（沿用專案 thermal_dataset 之型態定義）。
# 根因為「依壓痕幾何型態之工程推論」，非量測歸因；實際根因仍須現場驗證。
def defect_type(res):
    """回傳缺陷型態 A/B/C：C=良品(maxBlank≤門檻)；否則 A(中央最空) / B(邊緣最空)。"""
    if res["maxBlank"] <= 0.20:
        return "C"
    worst = max(res["blanks"], key=res["blanks"].get)
    return "A" if worst == "center" else "B"

RCA_RULES = {
    "C": ("良品", "—（接觸均勻、無明顯空白）", "—"),
    "A": ("中央接觸不良", "散熱器/CPU 翹曲(凸/凹)、TIM pump-out、安裝壓力中央不足",
          "檢查接觸面平整度；扣具均勻鎖附；改用抗 pump-out 相變化 TIM(PCM)"),
    "top": ("上緣缺失", "上側扣具/螺絲壓力不足、單邊翹曲", "檢查上側扣具扭力、對角均勻鎖附"),
    "bottom": ("下緣缺失", "下側扣具/螺絲壓力不足、單邊翹曲", "檢查下側扣具扭力、對角均勻鎖附"),
    "left": ("左側缺失", "左側偏壓、扣具單邊鬆", "對角均勻鎖附、檢查左側扭力"),
    "right": ("右側缺失", "右側偏壓、扣具單邊鬆", "對角均勻鎖附、檢查右側扭力"),
}
def rca(res):
    """根因分析：回傳 dict(type, type_name, worst, cause, action)。★工程推論非量測歸因★。"""
    t = defect_type(res); worst = max(res["blanks"], key=res["blanks"].get)
    if t == "C":
        name, cause, action = RCA_RULES["C"]
    elif t == "A":
        name, cause, action = RCA_RULES["A"]
    else:
        name, cause, action = RCA_RULES[worst]
    return dict(type=t, type_name=name, worst=worst, worst_blank=res["blanks"][worst],
                cause=cause, action=action)

# ---------------- SPEC 規格判定（對標 ASTM/業界判準 + 客戶 Tcpu 規格） ----------------
SPEC_DEFAULT = dict(min_eff_contact=0.95,   # 有效接觸率 = 1−overallBlank ≥ 95%（業界判準）
                    max_zone_blank=0.20,    # 任一區空白 ≤ 20%（本研究操作點）
                    min_zone_cov=0.80,      # 五區覆蓋率皆 ≥ 80%（均勻度）
                    max_tcpu=90.0)          # 推估 Tcpu ≤ 90°C（客戶規格）
def spec_check(res, tcpu=None, spec=SPEC_DEFAULT):
    """逐項規格判定；回傳 dict(items=[(項目,量測,門檻,pass)], overall_pass)。
    tcpu 為 None 時略過溫度項。★Tcpu 為模型推估、非實測。★"""
    eff = 1 - res["overallBlank"]; min_cov = min(res["cov"].values())
    items = [("有效接觸率 ≥95%", f"{eff*100:.1f}%", "≥95%", eff >= spec["min_eff_contact"]),
             ("最大區空白 ≤20%", f"{res['maxBlank']*100:.1f}%", "≤20%", res["maxBlank"] <= spec["max_zone_blank"]),
             ("五區覆蓋率 ≥80%", f"{min_cov*100:.1f}%", "≥80%", min_cov >= spec["min_zone_cov"])]
    if tcpu is not None:
        items.append((f"推估 Tcpu ≤{spec['max_tcpu']:.0f}°C", f"{tcpu:.1f}°C", f"≤{spec['max_tcpu']:.0f}°C",
                      tcpu <= spec["max_tcpu"]))
    return dict(items=items, overall_pass=all(p for *_, p in items))

# ---------------- 瞬態熱模型（一階 RC；模擬升溫至熱平衡，★非實測★） ----------------
def thermal_transient(P, Rtotal, Tamb, Cth, t):
    """一階集總 RC 熱模型：Tcpu(t)=Tamb+P·Rtotal·(1−exp(−t/τ))，τ=Rtotal·Cth。
    回傳溫度（°C）。★此為模型模擬、非實測；Cth 為假設熱容。★"""
    tau = max(1e-6, Rtotal * Cth)
    import math as _m
    return Tamb + P * Rtotal * (1 - _m.exp(-t / tau))

# ---------------- 規則式＋Opus 邊界覆核（諮詢式；不改自動判定） ----------------
# 設計依據（251 張 benchmark 模擬）：規則式偽陽性皆落在「判 defect 且 maxBlank
# 剛越過門檻」之邊界帶。在該帶內若讓 Opus 自動翻面 defect→good，雖救回 3 張偽陽性，
# 卻會誤放 1～2 張真缺陷（新增 FN），破壞零漏檢。故本功能僅取「第二意見」供人工覆核，
# ★絕不自動更動規則式判定★，確保自動指標與純規則式完全一致（只升不降）。
REVIEW_PROMPT = (
    "這是富士感壓紙壓在 CPU 散熱介面後的壓痕影像（粉紅/紅=有接觸受壓，白=無接觸）。"
    "判定原則：接觸顯色均勻飽滿、五區（上下左右中）無明顯空白＝good；"
    "某區明顯空白/缺角/邊緣缺失/中央發白/整體偏淡＝defect。"
    "請只憑影像判定，回覆務必以單字 good 或 defect 開頭，接冒號與一句話理由。")

def opus_review(image_path, model="claude-opus-4-8"):
    """呼叫 Opus 視覺模型對單張影像取 good/defect 第二意見（諮詢用，不改自動判定）。
    需 ANTHROPIC_API_KEY 與 anthropic 套件；會將該影像送至 Anthropic API（線上）。
    回傳 (label 或 None, 文字理由/錯誤訊息)。"""
    import base64, mimetypes
    try:
        import anthropic
    except ImportError:
        return None, "未安裝 anthropic 套件（pip install anthropic）"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "未設定 ANTHROPIC_API_KEY"
    try:
        b64 = base64.standard_b64encode(open(image_path, "rb").read()).decode()
        mt = mimetypes.guess_type(image_path)[0] or "image/png"
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model, max_tokens=300,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
                {"type": "text", "text": REVIEW_PROMPT}]}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        low = text.lower()
        gi, di = low.find("good"), low.find("defect")
        if gi == -1 and di == -1: lab = None
        elif di == -1: lab = "good"
        elif gi == -1: lab = "defect"
        else: lab = "good" if gi < di else "defect"
        return lab, text[:160]
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"

# ---------------- 缺失區標註圖（與線上工具 drawMissingOverlay 同規則） ----------------
def draw_overlay(pil_img, res, blank_thr, out_path):
    img = pil_img.convert("RGB").copy()
    W, H = img.size
    d = ImageDraw.Draw(img)
    b = res["bbox"]; sx, sy = W / GRID, H / GRID
    reg = {"top": (b["x0"], b["y0"], b["x1"], b["y0"] + b["fwy"]),
           "bottom": (b["x0"], b["y1"] - b["fwy"], b["x1"], b["y1"]),
           "left": (b["x0"], b["y0"] + b["fwy"], b["x0"] + b["fwx"], b["y1"] - b["fwy"]),
           "right": (b["x1"] - b["fwx"], b["y0"] + b["fwy"], b["x1"], b["y1"] - b["fwy"]),
           "center": (b["x0"] + b["fwx"], b["y0"] + b["fwy"], b["x1"] - b["fwx"], b["y1"] - b["fwy"])}
    for k, (rx0, ry0, rx1, ry1) in reg.items():
        miss = res["blanks"][k] > blank_thr
        d.rectangle([rx0 * sx, ry0 * sy, rx1 * sx - 1, ry1 * sy - 1],
                    outline=(255, 107, 107) if miss else (140, 220, 120),
                    width=4 if miss else 2)
    txt = f"{res['label']}  maxBlank {res['maxBlank']*100:.1f}%"
    d.rectangle([0, 0, 8 + 11 * len(txt), 26], fill=(0, 0, 0))
    d.text((6, 5), txt, fill=(255, 107, 107) if res["label"] == "defect" else (140, 220, 120))
    img.save(out_path)

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(
        description="CPU 壓痕檢測器（線上工具 v2.1 之 Python CLI 移植，固定論文操作點）",
        epilog="⚠ 不提供「訓練」：自動微調參數會偏離論文操作點，故刻意不移植。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("input", nargs="?", help="影像檔或資料夾（遞迴；路徑含 defect/good 即真值）；"
                    "--thermal --coverage 情境推算或 --list-tims 時可省略")
    ap.add_argument("--version", action="version",
                    version=f"inspector.py v{__version__}（{VERSION_DATE}）｜演算法＝{ENGINE}")
    ap.add_argument("--redness", type=float, default=P_DEFAULT["redness"], help="紅度門檻")
    ap.add_argument("--darkt",   type=float, default=P_DEFAULT["darkT"],  help="暗度門檻")
    ap.add_argument("--margin",  type=float, default=P_DEFAULT["margin"], help="內容區邊界比")
    ap.add_argument("--frame",   type=float, default=P_DEFAULT["frame"],  help="外框帶寬比")
    ap.add_argument("--blank",   type=float, default=P_DEFAULT["blank"],  help="空白率判定門檻")
    ap.add_argument("--auto-bbox", action="store_true", help="自動內容區（論文設定=OFF）")
    ap.add_argument("--resample", choices=RESAMPLE, default="NEAREST",
                    help="縮放濾鏡（6 濾鏡已驗證判定相同）")
    ap.add_argument("--csv",  metavar="PATH", help="逐張結果 CSV（欄位同線上工具匯出）")
    ap.add_argument("--json", metavar="PATH", help="完整結果 JSON")
    ap.add_argument("--overlay", metavar="DIR", help="輸出缺失區標註圖之資料夾")
    ap.add_argument("--verbose", action="store_true", help="逐張列印結果")
    rv = ap.add_argument_group("Opus 邊界覆核（諮詢式；★不改自動判定、嚴格保留零漏檢★）")
    rv.add_argument("--review", action="store_true",
                    help="對規則式判 defect 且 maxBlank 落在門檻邊界帶之少數案例呼叫 Opus 取第二意見、"
                         "標記待人工覆核（會把邊界影像送 Anthropic API；需 ANTHROPIC_API_KEY）")
    rv.add_argument("--review-band", type=float, default=0.10,
                    help="邊界帶寬：覆核 maxBlank ∈ [blank, blank+band] 之 defect 案例")
    rv.add_argument("--review-model", default="claude-opus-4-8", help="覆核用視覺模型 id")
    th = ap.add_argument_group("TIM 熱阻與 CPU 溫度推算（論文式 3.5、3.6；模型推估非實測）")
    th.add_argument("--thermal", action="store_true", help="啟用 TIM 熱阻/Tcpu 推算")
    th.add_argument("--tim", metavar="NAME", help="自內建型錄選現行 TIM（見 --list-tims）")
    th.add_argument("--tim-k", type=float, default=3.5, help="現行 TIM 導熱係數 k（W/m·K）")
    th.add_argument("--tim-d", type=float, default=None,
                    help="現行 TIM 厚度/BLT（mm；預設＝型錄值，自訂時 0.40）")
    th.add_argument("--power",  type=float, default=40.0, help="CPU 功耗 P（W；§4.10 PTL 40 W 平台）")
    th.add_argument("--tamb",   type=float, default=25.0, help="環境溫度 Tamb（°C）")
    th.add_argument("--area",   type=float, default=200.0, help="名義接觸面積 A（mm²；表 2.1 晶粒量級）")
    th.add_argument("--rother", type=float, default=0.80,
                    help="非 TIM 熱阻 Rother（°C/W；由 §4.10 實測 Tc 推估）")
    th.add_argument("--tlimit",  type=float, default=95.0, help="警戒溫度（°C，超過即觸發選型建議）")
    th.add_argument("--ttarget", type=float, default=90.0, help="客戶規格目標 Tcpu ≤（°C）")
    th.add_argument("--coverage", type=float, help="情境覆蓋率 C（0–1）；無影像時必填，有影像時覆寫")
    th.add_argument("--list-tims", action="store_true", help="列出內建 TIM 型錄後結束")
    rs = ap.add_argument_group("RCA 根因分析 / SPEC 規格判定（後處理；★工程推論與模型推估、非量測★）")
    rs.add_argument("--rca", action="store_true", help="逐張輸出缺陷型態(A/B/C)與根因推論、改善建議")
    rs.add_argument("--spec", action="store_true", help="逐張輸出 SPEC 規格判定(有效接觸/空白/覆蓋率/Tcpu)")
    ap.add_argument("--robust", action="store_true",
                    help="穩健化前處理(白點白平衡+曝光正規化)再偵測；抗環境光/手機色差/曝光偏移")
    ap.add_argument("--deskew", action="store_true",
                    help="旋轉對齊(隨壓痕傾斜校正)再偵測；維持固定框與零漏檢，誤檢略降(需 cv2)")
    args = ap.parse_args()

    if args.list_tims:
        print(f"內建 TIM 型錄（論文附錄四 表附四-1/-2/-3；d=型錄最薄或類型範圍下限）：")
        for name, typ, k, d, note in TIM_CATALOG:
            print(f"  {name:14s} {typ:10s} k={k:5g} W/m·K  BLT={d:g} mm ｜{note}")
        return

    TH = None
    if args.thermal:
        if args.tim:
            t = tim_lookup(args.tim)
            if not t: ap.error(f"型錄查無 {args.tim}（--list-tims 查看名單）")
            tim_name, k = t[0], t[2]
            d = args.tim_d if args.tim_d is not None else t[3]   # 型錄 TIM 允許覆寫厚度
        else:
            tim_name, k = "自訂", args.tim_k
            d = args.tim_d if args.tim_d is not None else 0.40
        if args.tlimit < args.ttarget:
            ap.error("--tlimit 不可低於 --ttarget")
        TH = dict(tim_name=tim_name, k=k, d=d, power=args.power, tamb=args.tamb,
                  area=args.area, rother=args.rother, tlimit=args.tlimit,
                  ttarget=args.ttarget, coverage=args.coverage)

    if not args.input:
        if TH and TH["coverage"] is not None:
            print(f"inspector.py v{__version__}（{VERSION_DATE}）｜TIM 熱阻情境推算（無影像）")
            print_thermal([], TH)
            return
        ap.error("未指定影像輸入（純情境推算請加 --thermal --coverage）")

    P = dict(redness=args.redness, darkT=args.darkt, margin=args.margin,
             frame=args.frame, blank=args.blank, autoBBox=args.auto_bbox)
    resample = RESAMPLE[args.resample]
    print(f"inspector.py v{__version__}（{VERSION_DATE}）｜演算法＝{ENGINE}")

    root = Path(args.input)
    if root.is_file():
        files = [root]; rel_root = root.parent
    elif root.is_dir():
        files = sorted(p for p in root.rglob("*")
                       if p.suffix.lower() in EXTS and not p.name.startswith("."))
        rel_root = root
    else:
        ap.error(f"找不到 {root}")
    if not files:
        ap.error(f"{root} 內找不到影像（{'/'.join(sorted(EXTS))}）")

    if args.overlay: os.makedirs(args.overlay, exist_ok=True)

    recs, fails = [], []
    for i, f in enumerate(files, 1):
        rel = str(f.relative_to(rel_root))
        try:
            img = Image.open(f)
            if args.deskew: img = deskew(img, P)
            res = robust_analyze(img, P, resample) if args.robust else analyze(img, P, resample)
        except Exception as e:
            fails.append((rel, str(e))); continue
        rec = dict(name=f.name, path=rel, split=split_of(rel), truth=label_of(rel),
                   pred=res["label"], maxBlank=res["maxBlank"],
                   overallBlank=res["overallBlank"], coverage=1 - res["overallBlank"],
                   cov=res["cov"], blanks=res["blanks"])
        if TH:
            r_tim, r_tot, tcpu = thermal_calc(TH["k"], TH["d"], rec["coverage"], TH)
            rec.update(r_tim=r_tim, r_total=r_tot, tcpu=tcpu)
        recs.append(rec)
        if args.overlay:
            draw_overlay(img, res, P["blank"],
                         os.path.join(args.overlay, f"{f.stem}_overlay.png"))
        if args.rca:
            rec["rca"] = rca(res)
        if args.spec:
            rec["spec"] = spec_check(res, rec.get("tcpu"))
        if args.verbose:
            mark = ("✓" if rec["truth"] == rec["pred"] else "✗") if rec["truth"] else " "
            worst = max(rec["blanks"], key=rec["blanks"].get)
            extra = (f" Tcpu≈{rec['tcpu']:.1f}°C" if TH else "")
            print(f"  {mark} {rel}: {rec['pred']} maxBlank={rec['maxBlank']*100:.1f}%"
                  f"（最嚴重區 {ZH[worst]}）" + (f" 真值={rec['truth']}" if rec["truth"] else "")
                  + extra)
            if args.rca and rec["pred"] == "defect":
                rc = rec["rca"]; print(f"      RCA[{rc['type']}] {rc['type_name']}：成因＝{rc['cause']}；建議＝{rc['action']}")
            if args.spec:
                sp = rec["spec"]; flags = "／".join(f"{n}{'✓' if p else '✗'}" for n, _, _, p in sp["items"])
                print(f"      SPEC {'PASS' if sp['overall_pass'] else 'FAIL'}：{flags}")
        elif i % 40 == 0:
            print(f"  {i}/{len(files)} …")

    # ── Opus 邊界覆核（諮詢式；標記 borderline 並取第二意見，★不改 rec["pred"]★）──
    review_note = ""
    if args.review:
        lo, hi = P["blank"], P["blank"] + args.review_band
        for r in recs:
            r["borderline"] = (r["pred"] == "defect" and lo <= r["maxBlank"] <= hi)
        bl = [r for r in recs if r["borderline"]]
        if bl:
            print(f"\n⚠ --review：將把 {len(bl)} 張邊界影像送至 Anthropic API（線上、非離線）取第二意見…")
            for j, r in enumerate(bl, 1):
                lab, why = opus_review(str(rel_root / r["path"]), args.review_model)
                r["opus_opinion"], r["opus_reason"], r["needs_review"] = lab, why, True
                if lab is None and not review_note: review_note = why
                print(f"  覆核 {j}/{len(bl)} {r['path']} → opus={lab or 'N/A'}        ", end="\r")
            print()

    n_truth = sum(1 for r in recs if r["truth"])
    print(f"\n共 {len(recs)} 張（有真值 {n_truth}：defect "
          f"{sum(1 for r in recs if r['truth']=='defect')}、good "
          f"{sum(1 for r in recs if r['truth']=='good')}；失敗 {len(fails)}）")
    print(f"參數：redness={P['redness']:g} darkT={P['darkT']:g} margin={P['margin']:g} "
          f"frame={P['frame']:g} blank={P['blank']:g} autoBBox={'ON' if P['autoBBox'] else 'OFF'} "
          f"resample={args.resample}")
    for rel, e in fails: print(f"  !! {rel} 讀取/分析失敗：{e}")
    print(f"判定：defect {sum(1 for r in recs if r['pred']=='defect')}、"
          f"good {sum(1 for r in recs if r['pred']=='good')}")

    m = None
    if n_truth:
        labeled = [r for r in recs if r["truth"]]
        m = metrics(labeled)
        print(f"\nTP/TN/FP/FN = {m['TP']}/{m['TN']}/{m['FP']}/{m['FN']}")
        print(f"Acc {m['acc']*100:.1f}%  Prec {m['prec']*100:.1f}%  "
              f"Rec {m['rec']*100:.1f}%  F1 {m['f1']*100:.1f}%  IoU {m['iou']*100:.1f}%")
        errs = [r for r in labeled if r["pred"] != r["truth"]]
        if errs:
            print(f"誤判 {len(errs)} 張：")
            for r in errs:
                typ = "FP（良品誤檢）" if r["truth"] == "good" else "FN（缺陷漏檢）"
                worst = max(r["blanks"], key=r["blanks"].get)
                print(f"  {r['path']} {typ} maxBlank={r['maxBlank']*100:.1f}%"
                      f"（最嚴重區 {ZH[worst]}）")
        else:
            print("誤判 0 張")

    if args.review:
        lo, hi = P["blank"], P["blank"] + args.review_band
        bl = [r for r in recs if r.get("borderline")]
        print(f"\n── Opus 邊界覆核（諮詢式：{lo:.0%}≤maxBlank≤{hi:.0%} 之 defect；★自動判定不變★）──")
        if not bl:
            print("  無邊界案例需覆核。")
        else:
            called = any(r.get("opus_opinion") in ("good", "defect") for r in bl)
            print(f"  {len(bl)} 張送 Opus 取第二意見："
                  if called else
                  f"  {len(bl)} 張屬邊界、建議人工覆核（Opus 未呼叫：{review_note}）：")
            for r in bl:
                op = r.get("opus_opinion")
                flag = ("→ Opus 認為 good，建議人工確認是否為偽陽性" if op == "good"
                        else "→ Opus 亦判 defect" if op == "defect" else "")
                tv = f" 真值={r['truth']}" if r["truth"] else ""
                print(f"  {r['path']} maxBlank={r['maxBlank']*100:.1f}% 規則式=defect "
                      f"opus={op or 'N/A'}{tv} {flag}")
                if op and r.get("opus_reason"): print(f"      理由：{r['opus_reason']}")
        print("  ※ 諮詢式：自動判定維持規則式結果（零漏檢不變）；是否翻面由人工決定。")

    if TH:
        print_thermal([] if TH["coverage"] is not None else recs, TH)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fo:
            w = csv.writer(fo)
            head = ["name", "split", "truth", "prediction", "correct",
                    "maxBlank", "overallBlank", "cov_top", "cov_bottom",
                    "cov_left", "cov_right", "cov_center"]
            if TH: head += ["coverage", "Rth_TIM_CW", "Rth_total_CW", "Tcpu_C", "over_limit"]
            if args.review: head += ["borderline", "opus_opinion", "needs_review"]
            w.writerow(head)
            for r in recs:
                correct = "" if not r["truth"] else ("1" if r["truth"] == r["pred"] else "0")
                row = [r["name"], r["split"], r["truth"] or "", r["pred"], correct,
                       f"{r['maxBlank']:.4f}", f"{r['overallBlank']:.4f}",
                       *[f"{r['cov'][k]:.4f}" for k in
                         ("top", "bottom", "left", "right", "center")]]
                if TH: row += [f"{r['coverage']:.4f}", f"{r['r_tim']:.4f}",
                               f"{r['r_total']:.4f}", f"{r['tcpu']:.1f}",
                               "1" if r["tcpu"] > TH["tlimit"] else "0"]
                if args.review: row += ["1" if r.get("borderline") else "0",
                                        r.get("opus_opinion") or "",
                                        "1" if r.get("needs_review") else "0"]
                w.writerow(row)
        print(f"\nCSV：{args.csv}")
    if args.json:
        thermal = None
        if TH:
            C0 = TH["coverage"] if TH["coverage"] is not None else (
                min((r["coverage"] for r in recs), default=1.0))
            ok, ng = thermal_recommend(C0, TH)
            _, _, tcpu0 = thermal_calc(TH["k"], TH["d"], C0, TH)
            thermal = dict(params=TH, worst_coverage=C0, worst_tcpu=tcpu0,
                           over_limit=tcpu0 > TH["tlimit"],
                           recommend=ok if tcpu0 > TH["tlimit"] else [],
                           rejected=ng if tcpu0 > TH["tlimit"] else [])
        json.dump(dict(version=__version__, version_date=VERSION_DATE, engine=ENGINE,
                       n=len(recs), params=dict(P, GRID=GRID, ASZ=ASZ,
                                                resample=args.resample),
                       metrics=m, thermal=thermal, recs=recs),
                  open(args.json, "w"), ensure_ascii=False, indent=1)
        print(f"JSON：{args.json}")
    if args.overlay:
        print(f"標註圖：{args.overlay}/")

if __name__ == "__main__":
    main()
