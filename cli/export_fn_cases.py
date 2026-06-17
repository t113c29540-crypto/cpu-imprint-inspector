#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""找出『置中重裁後新增漏檢』之缺陷影像並匯出原圖/置中圖/五區疊圖對照，供研究。
★說明:置中會把邊緣空白移進固定框覆蓋區→藏掉真缺陷→漏檢。此即不採置中之實證依據。★"""
import os, sys, shutil
import numpy as np
from pathlib import Path
from PIL import Image

HERE=os.path.dirname(os.path.abspath(__file__)); OUT=os.path.join(HERE,"FN案例")
os.makedirs(OUT,exist_ok=True)
# inspector.py 與本檔同層（cli/）；若放他處，請設環境變數 INSPECTOR_DIR。
sys.path.insert(0, os.environ.get("INSPECTOR_DIR", HERE))
import inspector
# 真實影像資料集路徑請以環境變數提供（倉庫不含任何真實壓痕影像）：
#   IMPRINT_DATASETS="/path/to/set1:/path/to/set2"
_DS=[p for p in os.environ.get("IMPRINT_DATASETS","").split(os.pathsep) if p]
if len(_DS)<2:
    raise SystemExit("請設定環境變數 IMPRINT_DATASETS（至少兩個資料集資料夾，以 os.pathsep 分隔）")
DS78, DS173 = _DS[0], _DS[1]
P=inspector.P_DEFAULT; ASZ=inspector.ASZ; ZH=inspector.ZH

import matplotlib; matplotlib.use("Agg")
from matplotlib import font_manager
for c in ("PingFang TC","Heiti TC","Arial Unicode MS"):
    try: font_manager.findfont(c,fallback_to_default=False); matplotlib.rcParams["font.sans-serif"]=[c]; break
    except: continue
matplotlib.rcParams["axes.unicode_minus"]=False
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

def recenter(im):
    im=im.convert("RGB"); a=np.asarray(im,float); H,W=a.shape[:2]
    r,g,b=a[...,0],a[...,1],a[...,2]
    contact=((r-(g+b)/2>P["redness"])|((0.299*r+0.587*g+0.114*b)<P["darkT"]))
    ys,xs=np.nonzero(contact)
    if len(xs)<30: return im
    cy,cx=ys.mean(),xs.mean(); dy,dx=int(round(H/2-cy)),int(round(W/2-cx))
    src=np.full_like(a,255)
    y0=max(0,dy);y1=min(H,H+dy);x0=max(0,dx);x1=min(W,W+dx); sy0=max(0,-dy);sx0=max(0,-dx)
    src[y0:y1,x0:x1]=a[sy0:sy0+(y1-y0),sx0:sx0+(x1-x0)]
    return Image.fromarray(src.astype(np.uint8))

def draw(ax,im,res,title,tcol):
    img=im.convert("RGB").resize((48,48)); ax.imshow(np.asarray(im.convert("RGB").resize((240,240))),extent=[0,48,48,0]); ax.axis("off")
    bb=res["bbox"]; worst=max(res["blanks"],key=res["blanks"].get)
    rects={"top":(bb["x0"],bb["y0"],bb["x1"]-bb["x0"],bb["fwy"]),
           "bottom":(bb["x0"],bb["y1"]-bb["fwy"],bb["x1"]-bb["x0"],bb["fwy"]),
           "left":(bb["x0"],bb["y0"]+bb["fwy"],bb["fwx"],bb["y1"]-bb["y0"]-2*bb["fwy"]),
           "right":(bb["x1"]-bb["fwx"],bb["y0"]+bb["fwy"],bb["fwx"],bb["y1"]-bb["y0"]-2*bb["fwy"]),
           "center":(bb["x0"]+bb["fwx"],bb["y0"]+bb["fwy"],bb["x1"]-bb["x0"]-2*bb["fwx"],bb["y1"]-bb["y0"]-2*bb["fwy"])}
    for z,(rx,ry,rw,rh) in rects.items():
        bl=res["blanks"][z]; col="#B5482F" if (z==worst and bl>0.20) else ("#A66A1E" if bl>0.20 else "#2E7D32")
        ax.add_patch(Rectangle((rx,ry),rw,rh,fill=False,edgecolor=col,lw=2.2 if z==worst else 1.2))
        ax.text(rx+rw/2,ry+rh/2,f"{ZH[z]}{bl*100:.0f}",ha="center",va="center",fontsize=8,color=col,fontweight="bold")
    ax.set_title(title,fontsize=11,color=tcol,fontweight="bold")

# 掃描 251, 找 FN(原defect→置中good) 與 修好的FP(原good→置中good 但原本被誤判)
recs=[]
for ds in (DS78,DS173):
    for f in sorted(Path(ds).rglob("*")):
        if f.suffix.lower() in inspector.EXTS and not f.name.startswith("."):
            try:
                im=Image.open(f); truth=inspector.label_of(str(f.relative_to(ds)))
                if not truth: continue
                ri=recenter(im); o=inspector.analyze(im); n=inspector.analyze(ri)
                recs.append(dict(path=f,truth=truth,o=o,n=n,im=im,ri=ri))
            except Exception: continue
fn=[r for r in recs if r["truth"]=="defect" and r["o"]["label"]=="defect" and r["n"]["label"]=="good"]
print(f"置中後新增漏檢 FN = {len(fn)} 張")
for i,r in enumerate(fn,1):
    name=r["path"].name
    # 存原圖/置中圖
    shutil.copy(r["path"], os.path.join(OUT,f"FN{i}_原圖_{name}"))
    r["ri"].save(os.path.join(OUT,f"FN{i}_置中後_{name}"))
    # 對照疊圖
    fig=Figure(figsize=(9,4.8),dpi=140); fig.set_facecolor("white")
    a1=fig.add_subplot(121); draw(a1,r["im"],r["o"],f"原始裁切 → {r['o']['label'].upper()} (正確攔截)","#2E7D32")
    a2=fig.add_subplot(122); draw(a2,r["ri"],r["n"],f"置中後 → {r['n']['label'].upper()} (★漏檢★)","#B5482F")
    wo=max(r["o"]["blanks"],key=r["o"]["blanks"].get); wn=max(r["n"]["blanks"],key=r["n"]["blanks"].get)
    fig.suptitle(f"FN{i}: {name}  真值=缺陷\n原圖最空白 {ZH[wo]}{r['o']['blanks'][wo]*100:.0f}%>20%→抓到；置中後最空白 {ZH[wn]}{r['n']['blanks'][wn]*100:.0f}%→空白被移進覆蓋區、漏掉",
                 fontsize=11,fontweight="bold",color="#15294A")
    fig.savefig(os.path.join(OUT,f"FN{i}_對照_{name}.png"),facecolor="white",bbox_inches="tight")
    print(f"  FN{i}: {name}  原圖maxBlank {r['o']['maxBlank']*100:.0f}%({ZH[wo]}) → 置中後 {r['n']['maxBlank']*100:.0f}%({ZH[wn]})")
print("✓ 已匯出至 FN案例/ (原圖+置中圖+對照疊圖)")
