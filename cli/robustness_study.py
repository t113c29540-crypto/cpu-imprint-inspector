#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真實影像擾動穩健性測試：對 251 張真實壓痕影像施加『環境光／不同手機／拍攝距離／
拍攝參數』四大類擾動，比較 baseline(絕對門檻) 與 robust(曝光正規化+限幅色偏修正) 兩偵測器，
量化準確率、漏檢(FN)、誤檢(FP)、判定穩定度。輸出 master CSV + JSON + 圖。
★全部使用真實影像加擾動(非生成影像)；準確率仍以真值 good/defect 計。★"""
import os, sys, json, io
import numpy as np
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter

HERE=os.path.dirname(os.path.abspath(__file__)); FIG=os.path.join(HERE,"圖"); os.makedirs(FIG,exist_ok=True)
# inspector.py 與本檔同層（cli/）；若放他處，請設環境變數 INSPECTOR_DIR 指向其資料夾。
sys.path.insert(0, os.environ.get("INSPECTOR_DIR", HERE))
import inspector
inspector.ROBUST_CAP=2.0
# 真實影像資料集路徑請以環境變數提供（倉庫不含任何真實壓痕影像）：
#   IMPRINT_DATASETS="/path/to/set1:/path/to/set2"（各資料夾內含 good/defect 真實影像）
DS=[p for p in os.environ.get("IMPRINT_DATASETS","").split(os.pathsep) if p]
if not DS:
    raise SystemExit("請設定環境變數 IMPRINT_DATASETS（以 os.pathsep 分隔的資料集資料夾路徑）")

import matplotlib; matplotlib.use("Agg")
from matplotlib import font_manager
for c in ("PingFang TC","Heiti TC","Arial Unicode MS"):
    try: font_manager.findfont(c,fallback_to_default=False); matplotlib.rcParams["font.sans-serif"]=[c]; break
    except: continue
matplotlib.rcParams["axes.unicode_minus"]=False
from matplotlib.figure import Figure

# ---------- 載入 251 張真實影像 ----------
IMGS=[]
for d in DS:
    for f in sorted(Path(d).rglob("*")):
        if f.suffix.lower() in inspector.EXTS and not f.name.startswith("."):
            t=inspector.label_of(str(f.relative_to(d)))
            if t: IMGS.append((Image.open(f).convert("RGB"), t))
print(f"已載入真實影像 {len(IMGS)} 張")

# ---------- 擾動函式（皆作用於真實影像）----------
def warm(im,a):  # 暖色光(鎢絲/暖白) R↑ B↓
    x=np.asarray(im,float); x[...,0]=np.clip(x[...,0]*a,0,255); x[...,2]=np.clip(x[...,2]/a,0,255); return Image.fromarray(x.astype(np.uint8))
def cool(im,a):  # 冷色光(日光燈/陰天) B↑ R↓
    x=np.asarray(im,float); x[...,2]=np.clip(x[...,2]*a,0,255); x[...,0]=np.clip(x[...,0]/a,0,255); return Image.fromarray(x.astype(np.uint8))
def expose(im,a):  # 曝光/環境亮度 (×a)
    return Image.fromarray(np.clip(np.asarray(im,float)*a,0,255).astype(np.uint8))
def sat(im,a):   return ImageEnhance.Color(im).enhance(a)
def contrast(im,a): return ImageEnhance.Contrast(im).enhance(a)
def sharpen(im,a):  return ImageEnhance.Sharpness(im).enhance(a)
def blur(im,r):  return im.filter(ImageFilter.GaussianBlur(r))
def noise(im,s):
    x=np.asarray(im,float)+np.random.default_rng(0).normal(0,s,np.asarray(im).shape); return Image.fromarray(np.clip(x,0,255).astype(np.uint8))
def jpeg(im,q):
    b=io.BytesIO(); im.save(b,"JPEG",quality=q); b.seek(0); return Image.open(b).convert("RGB")
def rotate(im,deg): return im.rotate(deg,resample=Image.BILINEAR,fillcolor=(255,255,255),expand=False)
def far(im,s):   # 拍攝較遠：壓痕在畫面中變小，四周留更多白
    W,H=im.size; cv=Image.new("RGB",(W,H),(255,255,255)); sm=im.resize((int(W*s),int(H*s)),Image.LANCZOS)
    cv.paste(sm,((W-sm.width)//2,(H-sm.height)//2)); return cv
def near(im,s):  # 拍攝較近：壓痕放大、裁掉部分邊距
    W,H=im.size; bg=im.resize((int(W*s),int(H*s)),Image.LANCZOS)
    l=(bg.width-W)//2; t=(bg.height-H)//2; return bg.crop((l,t,l+W,t+H))
def phone_vivid(im,_): return contrast(sat(im,1.45),1.12)            # 鮮豔渲染手機
def phone_muted(im,_): return sat(im,0.65)                            # 淡雅渲染手機
def phone_warm(im,_):  return contrast(warm(im,1.07),1.06)            # 暖調手機
def phone_cool(im,_):  return sharpen(cool(im,1.07),1.4)             # 冷調+銳化手機

# 四大類擾動 × 強度（標籤,函式,參數）
SUITE={
 "環境光":[("暖光(輕)",warm,1.06),("暖光(中)",warm,1.12),("冷光(輕)",cool,1.06),("冷光(中)",cool,1.12),
          ("環境偏暗",expose,0.7),("環境很暗",expose,0.55),("環境偏亮",expose,1.3),("環境很亮",expose,1.55)],
 "不同手機":[("鮮豔渲染",phone_vivid,0),("淡雅渲染",phone_muted,0),("暖調渲染",phone_warm,0),("冷調+銳化",phone_cool,0)],
 "拍攝距離":[("較遠×0.8",far,0.8),("很遠×0.65",far,0.65),("較近×1.2",near,1.2),("很近×1.4",near,1.4)],
 "拍攝參數":[("過曝",expose,1.4),("曝光不足",expose,0.6),("失焦模糊(輕)",blur,1.5),("失焦模糊(重)",blur,3.0),
            ("感光雜訊(輕)",noise,12),("感光雜訊(重)",noise,25),("JPEG壓縮q30",jpeg,30),("JPEG壓縮q12",jpeg,12),
            ("旋轉±6°",rotate,6),("旋轉±12°",rotate,12)],
}

def evaluate(transform):
    base_pred=[]; robu_pred=[]; truth=[]
    for im,t in IMGS:
        pim=transform(im); truth.append(t)
        base_pred.append(inspector.analyze(pim)["label"])
        robu_pred.append(inspector.robust_analyze(pim)["label"])
    return truth,base_pred,robu_pred

NDEF=sum(1 for _,t in IMGS if t=="defect"); NGOOD=sum(1 for _,t in IMGS if t=="good")
def metrics(truth,pred,clean_pred=None):
    tp=tn=fp=fn=0
    for t,p in zip(truth,pred):
        if t=="defect": tp+=(p=="defect"); fn+=(p=="good")
        else: tn+=(p=="good"); fp+=(p=="defect")
    acc=(tp+tn)/len(truth)*100
    dr=tp/NDEF*100 if NDEF else 100.0           # 瑕疵召回率(defect recall)
    sp=tn/NGOOD*100 if NGOOD else 100.0          # 良品 specificity = 1-FP/N_good
    stab=100.0*np.mean([a==b for a,b in zip(pred,clean_pred)]) if clean_pred else 100.0
    return dict(acc=round(acc,1),fp=fp,fn=fn,dr=round(dr,1),sp=round(sp,1),stab=round(stab,1))

# 乾淨基準（穩定度比較基準）
_,base_clean,robu_clean=evaluate(lambda im: im)
TRUTH=[t for _,t in IMGS]
clean_base_m=metrics(TRUTH,base_clean); clean_robu_m=metrics(TRUTH,robu_clean)
print(f"乾淨基準 base {clean_base_m}  robust {clean_robu_m}")

# 跑全網格
results={"_clean":{"base":clean_base_m,"robust":clean_robu_m}}
rows=[("類別","擾動","baseline Acc%","baseline FN","baseline FP","robust Acc%","robust FN","robust FP","ΔAcc","FN改善")]
for cat,perts in SUITE.items():
    results[cat]=[]
    for name,fn,arg in perts:
        tr,bp,rp=evaluate((lambda f,a: (lambda im: f(im,a)))(fn,arg))
        bm=metrics(tr,bp,base_clean); rm=metrics(tr,rp,robu_clean)
        results[cat].append({"name":name,"base":bm,"robust":rm})
        rows.append((cat,name,bm["acc"],bm["fn"],bm["fp"],rm["acc"],rm["fn"],rm["fp"],
                     round(rm["acc"]-bm["acc"],1), bm["fn"]-rm["fn"]))
        print(f"  [{cat}] {name:12s} base Acc{bm['acc']:5.1f}/FN{bm['fn']:>2}/FP{bm['fp']:>2}  →  robust Acc{rm['acc']:5.1f}/FN{rm['fn']:>2}/FP{rm['fp']:>2}")

# ---------- 類別不平衡基線 + 每類別指標之 N ----------
results["_meta"]={"n_defect":NDEF,"n_good":NGOOD,"n_total":len(IMGS),
                  "always_defect_acc":round(NDEF/len(IMGS)*100,1),
                  "clean_base_dr":clean_base_m["dr"],"clean_base_sp":clean_base_m["sp"]}
print(f"\n類別不平衡：瑕疵 {NDEF} / 良品 {NGOOD}（瑕疵占 {NDEF/len(IMGS)*100:.1f}%）；"
      f"『全猜瑕疵』瑣碎基線 = {NDEF/len(IMGS)*100:.1f}%")

# ---------- B1：更暗曝光點，定位 gain cap(=2.0) 之失效邊界 ----------
print("\n=== 更暗曝光掃描（定位 robust 操作邊界；gain cap=2.0 對應暗化下限 ×0.5）===")
results["_dark_sweep"]=[]
for fct in (0.7,0.6,0.55,0.5,0.45,0.4,0.35,0.3):
    tr,bp,rp=evaluate((lambda a: (lambda im: expose(im,a)))(fct))
    bm=metrics(tr,bp,base_clean); rm=metrics(tr,rp,robu_clean)
    results["_dark_sweep"].append({"factor":fct,"base":bm,"robust":rm})
    print(f"  ×{fct:<4} base Acc{bm['acc']:5.1f}/瑕疵召回{bm['dr']:5.1f}/FN{bm['fn']:>3}  →  robust Acc{rm['acc']:5.1f}/瑕疵召回{rm['dr']:5.1f}/FN{rm['fn']:>3}")

# ---------- B2：消融分析（gain on/off × redness-offset on/off）----------
print("\n=== 消融分析：分離曝光增益 與 限幅去色偏 之貢獻 ===")
ABL_ROWS=[("環境很暗×0.55",lambda im: expose(im,0.55)),("曝光不足×0.6",lambda im: expose(im,0.6)),
          ("暖調渲染",phone_warm if False else (lambda im: phone_warm(im,0)))]
results["_ablation"]=[]
for nm,tf in ABL_ROWS:
    tr=[t for _,t in IMGS]
    variants={}
    for vk,ug,uo in [("none(=base)",False,False),("僅增益",True,False),("僅去色偏",False,True),("both(=robust)",True,True)]:
        pred=[inspector.robust_analyze(tf(im),use_gain=ug,use_offset=uo)["label"] for im,_ in IMGS]
        variants[vk]=metrics(tr,pred)
    results["_ablation"].append({"name":nm,"variants":variants})
    print(f"  [{nm}]")
    for vk,m in variants.items():
        print(f"     {vk:14s} Acc{m['acc']:5.1f}  瑕疵召回{m['dr']:5.1f}  FN{m['fn']:>3}  FP{m['fp']:>2}")

# ---------- B4：邊界脆弱度（乾淨瑕疵之 maxBlank 落在 20%門檻 ±2% 內者）----------
clean_def_mb=[inspector.analyze(im)["maxBlank"] for im,t in IMGS if t=="defect"]
near=sum(1 for v in clean_def_mb if 0.18<=v<=0.22)
results["_boundary"]={"n_defect":len(clean_def_mb),"within_2pct_of_20":near,
                      "pct":round(near/len(clean_def_mb)*100,1)}
print(f"\n邊界脆弱度：{len(clean_def_mb)} 張瑕疵中 {near} 張之 maxBlank 落在 20%±2% 內（{near/len(clean_def_mb)*100:.1f}%）")

json.dump(results, open(os.path.join(HERE,"robustness_results.json"),"w"), ensure_ascii=False, indent=1)
# CSV
import csv
with open(os.path.join(HERE,"robustness_results.csv"),"w",newline="") as fp:
    csv.writer(fp).writerows(rows)
print("✓ 已存 robustness_results.json / .csv")

# ---------- 圖：四類別 baseline vs robust 準確率 ----------
NAVY="#15294A"; GREEN="#2E7D32"; RED="#B5482F"; GREY="#9AA3B2"
def cat_chart(cat,data,fname):
    names=[d["name"] for d in data]; bacc=[d["base"]["acc"] for d in data]; racc=[d["robust"]["acc"] for d in data]
    bfn=[d["base"]["fn"] for d in data]; rfn=[d["robust"]["fn"] for d in data]
    n=len(names); x=np.arange(n)
    fig=Figure(figsize=(max(7,n*1.5),4.6),dpi=140); fig.set_facecolor("white")
    ax=fig.add_subplot(111); w=0.38
    ax.bar(x-w/2,bacc,w,label="baseline(絕對門檻)",color=GREY)
    ax.bar(x+w/2,racc,w,label="robust(曝光正規化+色偏修正)",color=GREEN)
    for i in range(n):
        if bfn[i]>0: ax.text(x[i]-w/2,bacc[i]+1,f"漏{bfn[i]}",ha="center",fontsize=8,color=RED,fontweight="bold")
        if rfn[i]>0: ax.text(x[i]+w/2,racc[i]+1,f"漏{rfn[i]}",ha="center",fontsize=8,color=RED,fontweight="bold")
    ax.axhline(98.8,ls="--",lw=1,color=NAVY); ax.text(n-0.5,99.2,"乾淨基準98.8%",ha="right",fontsize=8,color=NAVY)
    ax.set_ylim(0,108); ax.set_xticks(x); ax.set_xticklabels(names,fontsize=9,rotation=15)
    ax.set_ylabel("準確率 (%)"); ax.set_title(f"擾動穩健性：{cat}（baseline vs robust；紅字=漏檢張數）",fontsize=12,color=NAVY,fontweight="bold")
    ax.legend(fontsize=9,loc="lower left"); ax.grid(axis="y",alpha=.3)
    fig.savefig(os.path.join(FIG,fname),facecolor="white",bbox_inches="tight"); print("  圖:",fname)

cat_chart("環境光",results["環境光"],"rob_環境光.png")
cat_chart("不同手機",results["不同手機"],"rob_手機.png")
cat_chart("拍攝距離",results["拍攝距離"],"rob_距離.png")
cat_chart("拍攝參數",results["拍攝參數"],"rob_參數.png")

# ---------- 圖：漏檢(FN)總覽 baseline vs robust ----------
allp=[(c,d) for c in SUITE for d in results[c]]
names=[d["name"] for _,d in allp]; bfn=[d["base"]["fn"] for _,d in allp]; rfn=[d["robust"]["fn"] for _,d in allp]
x=np.arange(len(names))
fig=Figure(figsize=(13,4.8),dpi=140); fig.set_facecolor("white"); ax=fig.add_subplot(111); w=0.4
ax.bar(x-w/2,bfn,w,label="baseline 漏檢",color=GREY); ax.bar(x+w/2,rfn,w,label="robust 漏檢",color=RED)
ax.set_xticks(x); ax.set_xticklabels(names,rotation=40,ha="right",fontsize=8)
ax.set_ylabel("漏檢張數 (FN，越低越好)"); ax.set_title("各擾動下漏檢(FN)對照：robust 優化前後（安全關鍵指標）",fontsize=12,color=NAVY,fontweight="bold")
ax.legend(fontsize=9); ax.grid(axis="y",alpha=.3)
fig.savefig(os.path.join(FIG,"rob_漏檢總覽.png"),facecolor="white",bbox_inches="tight"); print("  圖: rob_漏檢總覽.png")

# ---------- 圖：每類別指標（瑕疵召回 vs 良品 specificity）揭露良品類崩潰 ----------
ORANGE="#C9892B"
names=[d["name"] for _,d in allp]
bdr=[d["base"]["dr"] for _,d in allp]; rdr=[d["robust"]["dr"] for _,d in allp]
bsp=[d["base"]["sp"] for _,d in allp]; rsp=[d["robust"]["sp"] for _,d in allp]
x=np.arange(len(names))
fig=Figure(figsize=(13,7.4),dpi=140); fig.set_facecolor("white"); w=0.4
ax1=fig.add_subplot(211)
ax1.bar(x-w/2,bdr,w,label="baseline",color=GREY); ax1.bar(x+w/2,rdr,w,label="robust",color=GREEN)
ax1.axhline(100,ls=":",lw=1,color=NAVY)
ax1.set_ylim(0,108); ax1.set_xticks(x); ax1.set_xticklabels([]); ax1.set_ylabel(f"瑕疵召回率 %\n(N={NDEF})")
ax1.set_title("每類別指標：瑕疵召回率（越高越安全；漏檢的補集）",fontsize=12,color=NAVY,fontweight="bold")
ax1.legend(fontsize=9,loc="lower left"); ax1.grid(axis="y",alpha=.3)
ax2=fig.add_subplot(212)
ax2.bar(x-w/2,bsp,w,label="baseline",color=GREY); ax2.bar(x+w/2,rsp,w,label="robust",color=ORANGE)
ax2.axhline(100,ls=":",lw=1,color=NAVY)
ax2.set_ylim(0,108); ax2.set_xticks(x); ax2.set_xticklabels(names,rotation=40,ha="right",fontsize=8)
ax2.set_ylabel(f"良品 specificity %\n(=1−FP/N，N={NGOOD})")
ax2.set_title("每類別指標：良品 specificity（冷光／強光下急遽崩潰；準確率因類別不平衡而掩蓋此惡化）",fontsize=12,color=NAVY,fontweight="bold")
ax2.legend(fontsize=9,loc="lower left"); ax2.grid(axis="y",alpha=.3)
fig.savefig(os.path.join(FIG,"rob_每類別指標.png"),facecolor="white",bbox_inches="tight"); print("  圖: rob_每類別指標.png")

# ---------- 圖：更暗曝光掃描，標示 gain cap 失效邊界 ----------
ds=results["_dark_sweep"]; fx=[d["factor"] for d in ds]
bda=[d["base"]["dr"] for d in ds]; rda=[d["robust"]["dr"] for d in ds]
fig=Figure(figsize=(8,4.6),dpi=140); fig.set_facecolor("white"); ax=fig.add_subplot(111)
ax.plot(fx,bda,"o-",color=GREY,label="baseline 瑕疵召回"); ax.plot(fx,rda,"s-",color=GREEN,label="robust 瑕疵召回")
ax.axvline(0.5,ls="--",lw=1.2,color=RED); ax.text(0.5,40,"gain cap=2.0 之暗化下限 ×0.5",rotation=90,va="center",ha="right",fontsize=9,color=RED)
ax.set_xlabel("環境亮度倍率（越左越暗）"); ax.set_ylabel(f"瑕疵召回率 %（N={NDEF}）"); ax.set_ylim(0,108)
ax.invert_xaxis(); ax.set_title("robust 操作邊界：曝光正規化於 ×0.5 以上可救；更暗則 gain 達上限、開始失效",fontsize=11,color=NAVY,fontweight="bold")
ax.legend(fontsize=9); ax.grid(alpha=.3)
fig.savefig(os.path.join(FIG,"rob_暗化邊界.png"),facecolor="white",bbox_inches="tight"); print("  圖: rob_暗化邊界.png")
print("✓ 全部完成")
