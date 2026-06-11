#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
複核後離線重測：以「資料夾位置＝人工複核真值」重算指標（與線上工具相互驗證）。

輸入：複核資料夾（defect/ 與 good/ 內為 512×512 裁切圖，
      檔案在哪個資料夾＝人工複核後之真值）。
判定：論文操作點（redness=18, darkT=130, margin=0.20, frame=0.30,
      blank=0.20, autoBBox=OFF），與線上工具 v2.1 完全一致；
      另以 6 種縮放濾鏡（NEAREST/BILINEAR/BICUBIC/LANCZOS/BOX/HAMMING）
      驗證判定對縮放法之穩健性。

用法：
  python3 retest_with_truth.py --review ./review --json ./結果.json --csv ./逐張結果.csv

需求：Python 3.10+、numpy、Pillow
"""
import os, glob, math, csv, json, argparse
import numpy as np
from PIL import Image

GRID, ASZ = 48, 240
P = dict(redness=18, darkT=130, margin=0.20, frame=0.30, blank=0.20)

def js_round(x): return math.floor(x + 0.5)

def analyze(pil_img, resample=Image.NEAREST):
    img = pil_img.convert("RGB").resize((ASZ, ASZ), resample)
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

FILTERS = {"NEAREST": Image.NEAREST, "BILINEAR": Image.BILINEAR, "BICUBIC": Image.BICUBIC,
           "LANCZOS": Image.LANCZOS, "BOX": Image.BOX, "HAMMING": Image.HAMMING}

def confusion(recs):
    tp = sum(1 for r in recs if r["truth"] == "defect" and r["pred"] == "defect")
    tn = sum(1 for r in recs if r["truth"] == "good"   and r["pred"] == "good")
    fp = sum(1 for r in recs if r["truth"] == "good"   and r["pred"] == "defect")
    fn = sum(1 for r in recs if r["truth"] == "defect" and r["pred"] == "good")
    return tp, tn, fp, fn

def metrics(tp, tn, fp, fn):
    n = tp + tn + fp + fn
    acc  = (tp + tn) / n if n else 0
    prec = tp / (tp + fp) if (tp + fp) else 0
    rec  = tp / (tp + fn) if (tp + fn) else 0
    f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
    iou  = tp / (tp + fp + fn) if (tp + fp + fn) else 0
    return acc, prec, rec, f1, iou

def main():
    ap = argparse.ArgumentParser(description="複核後離線重測（論文操作點＋6 濾鏡）")
    ap.add_argument("--review", default="./review", help="複核資料夾（含 defect/ good/）")
    ap.add_argument("--json", default="./重測結果.json")
    ap.add_argument("--csv", default="./重測逐張結果.csv")
    args = ap.parse_args()

    items = []
    for truth in ["defect", "good"]:
        for p in sorted(glob.glob(os.path.join(args.review, truth, "*.png"))):
            items.append(dict(path=p, name=os.path.splitext(os.path.basename(p))[0],
                              truth=truth))
    if not items:
        ap.error(f"{args.review}/defect|good/ 內找不到 png")
    print(f"載入：defect {sum(1 for i in items if i['truth']=='defect')}、"
          f"good {sum(1 for i in items if i['truth']=='good')}（共 {len(items)}）")

    per_filter = {}
    for fname, f in FILTERS.items():
        recs = []
        for it in items:
            res = analyze(Image.open(it["path"]), f)
            recs.append(dict(name=it["name"], truth=it["truth"], pred=res["label"],
                             maxBlank=res["maxBlank"], blanks=res["blanks"]))
        per_filter[fname] = recs
        tp, tn, fp, fn = confusion(recs)
        acc, prec, rec_, f1, iou = metrics(tp, tn, fp, fn)
        print(f"{fname:10} TP/TN/FP/FN={tp}/{tn}/{fp}/{fn}  "
              f"Acc {acc*100:.1f}%  Prec {prec*100:.1f}%  Rec {rec_*100:.1f}%  "
              f"F1 {f1*100:.1f}%  IoU {iou*100:.1f}%")
    same = all(all(per_filter[f][i]["pred"] == per_filter["NEAREST"][i]["pred"]
                   for f in FILTERS) for i in range(len(items)))
    print(f"\n6 種縮放濾鏡逐張判定是否完全相同：{'是' if same else '否'}")

    recs = per_filter["NEAREST"]
    tp, tn, fp, fn = confusion(recs)
    errs = [r for r in recs if r["pred"] != r["truth"]]
    print(f"誤判 {len(errs)} 張：")
    for r in errs:
        typ = "FP（良品誤檢）" if r["truth"] == "good" else "FN（缺陷漏檢）"
        worst = max(r["blanks"], key=r["blanks"].get)
        print(f"  {r['name']} {typ} maxBlank={r['maxBlank']*100:.1f}%（最嚴重區 {worst}）")

    json.dump(dict(n=len(items), params=dict(P, GRID=GRID, ASZ=ASZ),
                   filters_all_same=same, recs=recs,
                   confusion=dict(tp=tp, tn=tn, fp=fp, fn=fn)),
              open(args.json, "w"), ensure_ascii=False, indent=1)
    ZH = {"top": "上", "bottom": "下", "left": "左", "right": "右", "center": "中"}
    with open(args.csv, "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.writer(fo)
        w.writerow(["影像", "真值(複核後)", "系統判定", "類別", "maxBlank",
                    "上", "下", "左", "右", "中", "最嚴重區", "結果"])
        for r in sorted(recs, key=lambda x: x["maxBlank"]):
            typ = ("TP" if r["truth"] == "defect" and r["pred"] == "defect" else
                   "TN" if r["truth"] == "good" and r["pred"] == "good" else
                   "FP" if r["truth"] == "good" else "FN")
            worst = max(r["blanks"], key=r["blanks"].get)
            w.writerow([r["name"], r["truth"], r["pred"], typ,
                        f"{r['maxBlank']*100:.1f}%",
                        *[f"{r['blanks'][k]*100:.1f}%" for k in ["top", "bottom", "left", "right", "center"]],
                        ZH[worst], "正確" if r["pred"] == r["truth"] else "誤判"])
    print(f"\n已輸出：{args.json}")
    print(f"已輸出：{args.csv}")

if __name__ == "__main__":
    main()
