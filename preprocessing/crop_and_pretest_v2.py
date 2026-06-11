#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPU 壓痕照片自動前處理 v2：拍照原圖 → 論文構圖 512×512 裁切＋離線預判定。

適用情境：
  現場拍攝之「未裁切」感壓紙照片（壓痕非主體、含藍／青色背景紙、
  白平衡可能偏冷）不可直接餵給檢測器——構圖與白平衡皆不符演算法前提。
  本腳本全自動將原圖對齊論文 78 張手動裁切之構圖規格
  （壓痕長邊佔畫面比 0.834、置中、轉正、等比無拉伸）。

管線（v2，2026-06-11）：
  1. EXIF 方向校正
  2. HSV 紙偵測（青色色相 75–115、飽和度 >60，抗白平衡漂移；
     合併面積 ≥ 最大塊 25% 之紙元件，容忍疊紙／折痕分裂）
  3. 壓痕定位——於「原始（未白平衡）」影像上偵測（redness>8、
     CLOSE35/OPEN7、分數合併斷段、元件紅密度 ≥ 最佳元件 30% 過濾、
     1–99 百分位 bbox、框外殘紅 >40% 則整紙 fallback）。
     注意：偵測不可在白平衡後做——R 增益會讓紙面粉霧過門檻、bbox 被撐大。
  4. 白平衡（von Kries：紙內非壓痕區中位色 → 235，全圖一致，增益記錄於 CSV）
  5. 背景填紙白（235）：紙外一律填白，方形裁切超出紙界亦不含背景紙
  6. 旋轉轉正：壓痕點集 minAreaRect 角度（fallback 用紙輪廓），
     僅於長寬比 ≥1.15 且 1°≤|角|≤20° 時套用，方向以軸對齊 bbox 面積最小化選定
  7. 等比方形裁切：邊長＝壓痕長邊 ÷ 0.834，中心＝壓痕 bbox 中心，
     超出影像處以紙白補齊——維持原始比例、無任何拉伸
  8. LANCZOS 縮放輸出 512×512，依「論文操作點」離線預判定分至 defect|good/

用法：
  python3 crop_and_pretest_v2.py --src ./photos --out ./review --csv ./預判定報告.csv

之後的正確流程：
  (1) 人工複核 review/defect|good/ 之檔案歸屬（⚠ 預判定不是真值，判錯就搬到另一夾）
  (2) 於線上工具以「上傳資料夾」載入 review/（路徑含 defect/good 即真值）
  (3) 參數保持預設按「執行」；⚠ 勿按「訓練」（會自動微調參數偏離論文操作點）

需求：Python 3.10+、numpy、opencv-python、Pillow
"""
import os, glob, math, csv, argparse
import numpy as np
import cv2
from PIL import Image, ImageOps

GRID, ASZ = 48, 240
P = dict(redness=18, darkT=130, margin=0.20, frame=0.30, blank=0.20)  # 論文操作點
TARGET_FRAC = 0.834        # 壓痕長邊佔方形畫面比（論文 78 張手動裁切之中位數）
PAPER_WHITE = 235.0
ROT_MIN, ROT_MAX = 1.0, 20.0
ROT_ASPECT_MIN = 1.15      # minAreaRect 長寬比低於此值時角度不可信，不旋轉

# ---------------- 論文操作點 analyze（與線上工具 v2.1 一致） ----------------
def js_round(x): return math.floor(x + 0.5)

def analyze(pil_img):
    img = pil_img.convert("RGB").resize((ASZ, ASZ), Image.NEAREST)
    a = np.asarray(img, dtype=np.float64)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    contact = (((r - (g + b) / 2.0) > P["redness"]) |
               ((0.299 * r + 0.587 * g + 0.114 * b) < P["darkT"])).astype(np.float64)
    dens = contact.reshape(GRID, 5, GRID, 5).mean(axis=(1, 3))
    x0 = math.floor(GRID * P["margin"]); x1 = math.ceil(GRID * (1 - P["margin"]))
    y0, y1 = x0, x1
    cw, ch = x1 - x0, y1 - y0
    fwx = max(1, js_round(cw * P["frame"])); fwy = max(1, js_round(ch * P["frame"]))
    def rmean(rx0, ry0, rx1, ry1):
        sub = dens[ry0:ry1, rx0:rx1]
        return float(sub.mean()) if sub.size else 0.0
    R = {"top": rmean(x0, y0, x1, y0 + fwy), "bottom": rmean(x0, y1 - fwy, x1, y1),
         "left": rmean(x0, y0 + fwy, x0 + fwx, y1 - fwy),
         "right": rmean(x1 - fwx, y0 + fwy, x1, y1 - fwy),
         "center": rmean(x0 + fwx, y0 + fwy, x1 - fwx, y1 - fwy)}
    blanks = {k: 1 - v for k, v in R.items()}
    maxBlank = max(blanks.values())
    return dict(blanks=blanks, maxBlank=maxBlank,
                label="defect" if maxBlank > P["blank"] else "good")

# ---------------- 幾何輔助 ----------------
def norm_angle(rw, rh, ang):
    """minAreaRect 角度 → 長軸相對 x 軸之角，正規化至 [-45, 45]。"""
    if rw < rh:
        ang += 90.0
    while ang > 45.0: ang -= 90.0
    while ang < -45.0: ang += 90.0
    return ang

def pick_rotation(pts, ang):
    """以「旋轉後軸對齊 bbox 面積最小」決定旋轉方向（不依賴角度正負號慣例）。"""
    best_ang, best_area = 0.0, None
    c = pts.mean(axis=0)
    for a in (ang, -ang):
        th = math.radians(a)
        R = np.array([[math.cos(th), math.sin(th)], [-math.sin(th), math.cos(th)]])
        q = (pts - c) @ R.T
        area = float(np.ptp(q[:, 0])) * float(np.ptp(q[:, 1]))
        if best_area is None or area < best_area:
            best_area, best_ang = area, a
    return best_ang

# ---------------- v2 自動裁切 ----------------
def crop_imprint_v2(path):
    """回傳 (512×512 PIL 影像, 診斷 dict)。等比方形＋旋轉＋背景填白。"""
    pil = ImageOps.exif_transpose(Image.open(path))
    rgb = np.asarray(pil.convert("RGB"))
    H, W = rgb.shape[:2]

    # 1) 紙偵測（HSV 青藍背景；合併 ≥25% 最大塊之元件）
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    Hh, Ss = hsv[..., 0].astype(np.int16), hsv[..., 1].astype(np.int16)
    bg = ((Hh >= 75) & (Hh <= 115) & (Ss > 60)).astype(np.uint8)
    paper = cv2.morphologyEx(1 - bg, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(paper, 8)
    if n < 2:
        raise RuntimeError("找不到紙（背景偵測失敗）")
    areas = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(areas))
    paper_keep = np.zeros_like(paper)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 0.25 * stats[main, cv2.CC_STAT_AREA]:
            paper_keep[lab == i] = 1
    ys, xs = np.nonzero(paper_keep)
    px0, px1 = int(xs.min()), int(xs.max())
    py0, py1 = int(ys.min()), int(ys.max())
    pw, ph = px1 - px0, py1 - py0

    # 2) 壓痕定位（於原始影像；門檻 redness>8）
    Rc, Gc, Bc = [rgb[..., i].astype(np.int16) for i in range(3)]
    redness = Rc - (Gc + Bc) // 2
    er = int(0.06 * max(pw, ph))
    inner = cv2.erode(paper_keep, np.ones((max(3, er), max(3, er)), np.uint8))
    red_raw = ((redness > 8) & (inner > 0))
    red = red_raw.astype(np.uint8)
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((35, 35), np.uint8))
    red = cv2.morphologyEx(red, cv2.MORPH_OPEN,  np.ones((7, 7),  np.uint8))

    nR, labR, statsR, centR = cv2.connectedComponentsWithStats(red, 8)
    cx0, cy0 = px0 + pw / 2, py0 + ph / 2
    diag = math.hypot(pw, ph)
    best, best_score = None, -1
    for i in range(1, nR):
        area = statsR[i, cv2.CC_STAT_AREA]
        if area < 0.0008 * pw * ph:
            continue
        d = math.hypot(centR[i][0] - cx0, centR[i][1] - cy0) / (diag / 2)
        score = area * math.exp(-2.0 * d)
        if score > best_score:
            best_score, best = score, i

    mode = "imprint"
    keep = None
    if best is not None:
        # 元件「紅密度」＝CLOSE/OPEN 後元件範圍內之原始紅像素比例。
        # 稀疏遠端斑點經 CLOSE 聚成大面積低密度元件會把 bbox 撐大 →
        # 加「密度 ≥ 最佳元件 30%」過濾；真實斷段壓痕（雙帶型）皆高密度，不受影響。
        def comp_density(i):
            m = (labR == i)
            return red_raw[m].sum() / max(1, int(m.sum()))
        best_density = comp_density(best)
        keep = np.zeros_like(red)
        for i in range(1, nR):
            area = statsR[i, cv2.CC_STAT_AREA]
            if area < 0.0008 * pw * ph: continue
            d = math.hypot(centR[i][0] - cx0, centR[i][1] - cy0) / (diag / 2)
            if (area * math.exp(-2.0 * d) >= 0.15 * best_score and
                    comp_density(i) >= 0.30 * best_density):
                keep[labR == i] = 1
        kys, kxs = np.nonzero(keep)
        bx0, bx1 = np.percentile(kxs, 1), np.percentile(kxs, 99)
        by0, by1 = np.percentile(kys, 1), np.percentile(kys, 99)
        in_box = int(red_raw[int(by0):int(by1) + 1, int(bx0):int(bx1) + 1].sum())
        out_box = int(red_raw.sum()) - in_box
        if out_box > 0.40 * max(in_box, 1):
            mode = "fallback_partial_imprint"   # 框僅含壓痕局部 → 整紙保守評估
    else:
        mode = "fallback_paper"                 # 無紅區 → 整紙保守評估

    # 3) 白平衡（紙內非壓痕區中位色 → 235，僅用於輸出影像）
    red_d = cv2.dilate(red, np.ones((41, 41), np.uint8))
    sel = (paper_keep > 0) & (red_d == 0)
    wp = np.median(rgb[sel].reshape(-1, 3), axis=0) if sel.sum() > 1000 else \
         np.percentile(rgb[paper_keep > 0].reshape(-1, 3), 90, axis=0)
    gain = PAPER_WHITE / np.maximum(wp, 1.0)
    img = np.clip(rgb.astype(np.float64) * gain, 0, 255).astype(np.uint8)

    # 4) 背景填紙白
    keep_d = cv2.dilate(paper_keep, np.ones((7, 7), np.uint8))
    img[keep_d == 0] = int(PAPER_WHITE)

    # 5) 旋轉角估計
    if mode == "imprint":
        sys_, sxs_ = np.nonzero(keep)
    else:
        sys_, sxs_ = np.nonzero(paper_keep)
    samp = np.random.default_rng(0).choice(len(sxs_), min(len(sxs_), 30000), replace=False)
    pts = np.stack([sxs_[samp], sys_[samp]], 1).astype(np.float32)
    (rcx, rcy), (rw, rh), raw_ang = cv2.minAreaRect(pts)
    rect_aspect = max(rw, rh) / max(1.0, min(rw, rh))
    ang = norm_angle(rw, rh, raw_ang)
    apply_ang = 0.0
    if rect_aspect >= ROT_ASPECT_MIN and ROT_MIN <= abs(ang) <= ROT_MAX:
        apply_ang = pick_rotation(pts.astype(np.float64), ang)

    # 6) 旋轉（繞目標中心，邊界補紙白）＋ mask 同步旋轉
    if mode == "imprint":
        ccx, ccy = (bx0 + bx1) / 2, (by0 + by1) / 2
        target_mask = keep
    else:
        ccx, ccy = cx0, cy0
        target_mask = paper_keep
    if apply_ang != 0.0:
        M = cv2.getRotationMatrix2D((ccx, ccy), apply_ang, 1.0)
        img_r = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_CONSTANT,
                               borderValue=(int(PAPER_WHITE),) * 3)
        mask_r = cv2.warpAffine(target_mask, M, (W, H), flags=cv2.INTER_NEAREST,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    else:
        img_r, mask_r = img, target_mask

    # 7) 旋轉後重算 bbox → 等比方形（邊長 = 長邊 / TARGET_FRAC）
    mys, mxs = np.nonzero(mask_r)
    if mode == "imprint":
        qx0, qx1 = np.percentile(mxs, 1), np.percentile(mxs, 99)
        qy0, qy1 = np.percentile(mys, 1), np.percentile(mys, 99)
        side = max(qx1 - qx0, qy1 - qy0) / TARGET_FRAC
    else:
        qx0, qx1 = float(mxs.min()), float(mxs.max())
        qy0, qy1 = float(mys.min()), float(mys.max())
        side = max(qx1 - qx0, qy1 - qy0) * 0.92   # 整紙模式：紙長邊略內縮
    scx, scy = (qx0 + qx1) / 2, (qy0 + qy1) / 2
    side = int(round(side))
    sx0, sy0 = int(round(scx - side / 2)), int(round(scy - side / 2))

    # 8) 取方形（超出影像處以紙白補齊 → 等比、無拉伸）
    canvas = np.full((side, side, 3), int(PAPER_WHITE), np.uint8)
    ix0, iy0 = max(0, sx0), max(0, sy0)
    ix1, iy1 = min(W, sx0 + side), min(H, sy0 + side)
    if ix1 > ix0 and iy1 > iy0:
        canvas[iy0 - sy0:iy1 - sy0, ix0 - sx0:ix1 - sx0] = img_r[iy0:iy1, ix0:ix1]
    out = Image.fromarray(canvas).resize((512, 512), Image.LANCZOS)

    frac = max(qx1 - qx0, qy1 - qy0) / side
    return out, dict(mode=mode, angle=round(float(apply_ang), 2),
                     square=(sx0, sy0, side), frac=round(float(frac), 3),
                     whitepoint=tuple(round(v, 1) for v in wp),
                     gain=tuple(round(g, 3) for g in gain))

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="CPU 壓痕照片自動前處理 v2（裁切＋預判定）")
    ap.add_argument("--src", default="./photos", help="原始照片資料夾（jpg/jpeg/png）")
    ap.add_argument("--out", default="./review", help="輸出複核資料夾（自動建立 defect/ good/）")
    ap.add_argument("--csv", default="./預判定報告.csv", help="逐張預判定 CSV 路徑")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "defect"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "good"), exist_ok=True)
    # set 去重：大小寫不敏感檔案系統（macOS 預設）上 *.jpg 與 *.JPG 會重複匹配
    files = sorted({p for ext in ("jpg", "jpeg", "png", "JPG", "JPEG", "PNG")
                    for p in glob.glob(os.path.join(args.src, f"*.{ext}"))})
    if not files:
        ap.error(f"{args.src} 內找不到照片")
    print(f"共 {len(files)} 張原始照片")

    rows, n_def, n_good, n_rot, n_fb = [], 0, 0, 0, 0
    for i, f in enumerate(files, 1):
        stem = os.path.splitext(os.path.basename(f))[0]
        try:
            img, diag = crop_imprint_v2(f)
        except Exception as e:
            print(f"  !! {os.path.basename(f)} 裁切失敗：{e}")
            rows.append([os.path.basename(f), "CROP_FAIL"] + [""] * 12)
            continue
        res = analyze(img)
        sub = res["label"]
        img.save(os.path.join(args.out, sub, f"{stem}_cropped.png"))
        n_def += sub == "defect"; n_good += sub == "good"
        n_rot += diag["angle"] != 0.0; n_fb += diag["mode"] != "imprint"
        b = res["blanks"]
        rows.append([os.path.basename(f), sub, f"{res['maxBlank']:.4f}",
                     f"{b['top']:.3f}", f"{b['bottom']:.3f}", f"{b['left']:.3f}",
                     f"{b['right']:.3f}", f"{b['center']:.3f}",
                     diag["mode"], diag["angle"], diag["frac"],
                     "/".join(str(v) for v in diag["square"]),
                     "/".join(str(v) for v in diag["whitepoint"]),
                     "/".join(str(g) for g in diag["gain"])])
        if i % 40 == 0: print(f"  {i}/{len(files)} …")

    with open(args.csv, "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.writer(fo)
        w.writerow(["原始檔名", "預判定", "maxBlank", "上", "下", "左", "右", "中",
                    "裁切模式", "旋轉角(度)", "壓痕長邊佔比", "方形框x/y/邊長",
                    "白點RGB", "白平衡增益RGB"])
        w.writerows(rows)
    print(f"\n預判定：defect {n_def}、good {n_good}（旋轉 {n_rot}、fallback {n_fb}）")
    print(f"CSV：{args.csv}")
    print(f"複核資料夾：{args.out}/defect|good/")
    print("⚠ 預判定不是真值——請人工複核後再上傳線上工具；參數保持預設、勿按「訓練」。")

if __name__ == "__main__":
    main()
