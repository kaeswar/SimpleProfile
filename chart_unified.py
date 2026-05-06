"""Unified TradingView Candlestick + Canvas Market Profile chart.

Single QWebEngineView containing:
  - Top:    TradingView Lightweight Charts (OHLCV candlestick)
  - Bottom: HTML5 Canvas Market Profile (TPO boxes + letters + key levels)
Both share a dark theme and update together.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace

import numpy as np
import pandas as pd
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView

from engine import (
    ProfileResult, bin_counts, bin_minute_counts, bin_letters_per_bracket,
    bracket_letter, composite_letter, minute_key_levels,
)

LWC_CDN = "https://unpkg.com/lightweight-charts@4/dist/lightweight-charts.standalone.production.js"

TPO_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
    "#393b79", "#637939", "#8c6d31", "#843c39", "#7b4173",
    "#5254a3",
]

# ---------------------------------------------------------------------------
# HTML + CSS + JS  (single-page app)
# ---------------------------------------------------------------------------
_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#131722;overflow:hidden;font-family:Consolas,monospace}
#wrap{display:flex;flex-direction:column;height:100vh;width:100vw}
#candle-wrap{flex:0 0 38%;min-height:80px;position:relative}
#candle-chart{width:100%;height:100%}
#divider{height:5px;background:#2B2B43;cursor:row-resize;flex-shrink:0}
#profile-wrap{flex:1;min-height:80px;position:relative}
#profile{position:absolute;top:0;left:0;width:100%;height:100%}
</style>
</head><body>
<div id="wrap">
  <div id="candle-wrap"><div id="candle-chart"></div></div>
  <div id="divider"></div>
  <div id="profile-wrap"><canvas id="profile"></canvas></div>
</div>

<script src="__LWC_CDN__"></script>
<script>
// ===================== CANDLESTICK (LWC) =====================
const cDiv = document.getElementById('candle-chart');
const lwChart = LightweightCharts.createChart(cDiv, {
    layout:{background:{type:'solid',color:'#131722'},textColor:'#d1d4dc'},
    grid:{vertLines:{color:'#1e222d'},horzLines:{color:'#1e222d'}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:true},
    handleScale:{axisPressedMouseMove:{time:true,price:true},mouseWheel:true,pinch:true},
    rightPriceScale:{borderColor:'#2B2B43',autoScale:true},
    timeScale:{borderColor:'#2B2B43',timeVisible:true,secondsVisible:false},
});
const candleSeries = lwChart.addCandlestickSeries({
    upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,
    wickUpColor:'#26a69a',wickDownColor:'#ef5350'});
const volSeries = lwChart.addHistogramSeries({
    color:'#26a69a',priceFormat:{type:'volume'},priceScaleId:'vol'});
lwChart.priceScale('vol').applyOptions({scaleMargins:{top:0.82,bottom:0}});

let _cLines=[];
function setCandles(c,v){
    candleSeries.setData(c);
    volSeries.setData(v.map((val,i)=>({time:c[i].time,value:val,
        color:c[i].close>=c[i].open?'#26a69a80':'#ef535080'})));
    lwChart.timeScale().fitContent();
}
function setCandleLevels(poc,vah,val){
    _cLines.forEach(l=>candleSeries.removePriceLine(l));_cLines=[];
    _cLines.push(candleSeries.createPriceLine({price:poc,color:'#d62728',lineWidth:2,
        lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:'POC'}));
    _cLines.push(candleSeries.createPriceLine({price:vah,color:'#2ca02c',lineWidth:1,
        lineStyle:LightweightCharts.LineStyle.Solid,axisLabelVisible:true,title:'VAH'}));
    _cLines.push(candleSeries.createPriceLine({price:val,color:'#2ca02c',lineWidth:1,
        lineStyle:LightweightCharts.LineStyle.Solid,axisLabelVisible:true,title:'VAL'}));
}

// Resize LWC on container resize
const cObs=new ResizeObserver(()=>{lwChart.applyOptions({
    width:cDiv.clientWidth,height:cDiv.clientHeight})});
cObs.observe(cDiv);

// ===================== PROFILE (Canvas) =====================
function niceStep(range,tgt){
    if(range<=0)return 1;
    const r=range/tgt,p=Math.pow(10,Math.floor(Math.log10(r))),f=r/p;
    return (f<=1.5?1:f<=3?2:f<=7?5:10)*p;
}

const PR={
    canvas:null,ctx:null,data:null,
    vx0:0,vy0:0,vx1:10,vy1:100,
    ml:72,mr:72,mt:28,mb:36,
    mx:-1,my:-1,dragging:false,

    init(){
        this.canvas=document.getElementById('profile');
        this.ctx=this.canvas.getContext('2d');
        const c=this.canvas;
        c.addEventListener('wheel',e=>this._onWheel(e),{passive:false});
        c.addEventListener('mousedown',e=>this._onDown(e));
        c.addEventListener('mousemove',e=>this._onMove(e));
        c.addEventListener('mouseup',()=>this.dragging=false);
        c.addEventListener('mouseleave',()=>{this.mx=-1;this.my=-1;this.render()});
        c.addEventListener('dblclick',()=>this._fitView());
        new ResizeObserver(()=>this.render()).observe(c);
    },

    setData(d){this.data=d;this._fitView()},
    _fitView(){if(this.data&&this.data.bounds){const b=this.data.bounds;
        this.vx0=b.xMin;this.vx1=b.xMax;this.vy0=b.yMin;this.vy1=b.yMax}this.render()},

    // coord transforms
    W(){return this.canvas.clientWidth},
    H(){return this.canvas.clientHeight},
    pW(){return this.W()-this.ml-this.mr},
    pH(){return this.H()-this.mt-this.mb},
    px(dx){return this.ml+(dx-this.vx0)/(this.vx1-this.vx0)*this.pW()},
    py(dy){return this.mt+(1-(dy-this.vy0)/(this.vy1-this.vy0))*this.pH()},
    dx(px){return this.vx0+(px-this.ml)/this.pW()*(this.vx1-this.vx0)},
    dy(py){return this.vy0+(1-(py-this.mt)/this.pH())*(this.vy1-this.vy0)},

    render(){
        if(!this.canvas)return;
        const c=this.canvas,x=this.ctx;
        const W=c.clientWidth,H=c.clientHeight;
        if(W<1||H<1)return;
        const dpr=devicePixelRatio||1;
        c.width=W*dpr;c.height=H*dpr;
        x.setTransform(dpr,0,0,dpr,0,0);
        x.fillStyle='#131722';x.fillRect(0,0,W,H);
        if(!this.data)return;
        this._grid(W,H);this._va(W,H);this._cells(W,H);
        this._seps(W,H);this._klines(W,H);this._markers(W,H);this._counts(W,H);
        this._axes(W,H);this._cross(W,H);this._title(W,H);
    },

    // ---------- drawing helpers ----------
    _grid(W,H){
        const x=this.ctx;x.strokeStyle='#1e222d';x.lineWidth=1;
        const ys=niceStep(this.vy1-this.vy0,10);
        for(let y=Math.ceil(this.vy0/ys)*ys;y<=this.vy1;y+=ys){
            const py=this.py(y);if(py<this.mt||py>H-this.mb)continue;
            x.beginPath();x.moveTo(this.ml,py);x.lineTo(W-this.mr,py);x.stroke()}
        const xs=niceStep(this.vx1-this.vx0,8);
        for(let v=Math.ceil(this.vx0/xs)*xs;v<=this.vx1;v+=xs){
            const px=this.px(v);if(px<this.ml||px>W-this.mr)continue;
            x.beginPath();x.moveTo(px,this.mt);x.lineTo(px,H-this.mb);x.stroke()}
    },

    _va(W,H){
        const x=this.ctx,d=this.data;if(!d||!d.vaBands)return;
        for(const b of d.vaBands){
            const py0=this.py(b.y1),py1=this.py(b.y0);
            const px0=b.x0!=null?Math.max(this.px(b.x0),this.ml):this.ml;
            const px1=b.x1!=null?Math.min(this.px(b.x1),W-this.mr):W-this.mr;
            x.fillStyle='rgba(255,215,0,0.08)';
            x.fillRect(px0,Math.max(py0,this.mt),px1-px0,
                Math.min(py1,H-this.mb)-Math.max(py0,this.mt));
        }
    },

    _cells(W,H){
        const x=this.ctx,d=this.data;if(!d||!d.cells)return;
        const pal=d.palette||[];
        const plotL=this.ml,plotR=W-this.mr,plotT=this.mt,plotB=H-this.mb;
        for(const c of d.cells){
            const [cx,cy,cw,ch,ci,letter,isPoc]=c;
            const lx=this.px(cx+cw*0.03), rx=this.px(cx+cw*0.97);
            const ty=this.py(cy+ch*0.95), by=this.py(cy+ch*0.05);
            if(rx<plotL||lx>plotR||by<plotT||ty>plotB)continue;
            const rw=rx-lx,rh=by-ty;
            x.fillStyle=isPoc?'#FFD700':pal[ci%pal.length]||'#888';
            x.fillRect(lx,ty,rw,rh);
            x.strokeStyle='rgba(0,0,0,0.25)';x.lineWidth=0.5;
            x.strokeRect(lx,ty,rw,rh);
            if(letter&&rw>10&&rh>8){
                const fs=Math.min(Math.max(Math.min(rh*0.65,rw*0.8),7),14);
                x.font=`bold ${fs}px Consolas,monospace`;
                x.fillStyle=isPoc?'#000':'#fff';x.textAlign='center';x.textBaseline='middle';
                x.fillText(letter,lx+rw/2,ty+rh/2);
            }
        }
    },

    _seps(W,H){
        const x=this.ctx,d=this.data;if(!d||!d.separators)return;
        x.save();x.strokeStyle='#555';x.setLineDash([4,4]);x.lineWidth=1;
        for(const sx of d.separators){
            const px=this.px(sx);
            if(px>this.ml&&px<W-this.mr){
                x.beginPath();x.moveTo(px,this.mt);x.lineTo(px,H-this.mb);x.stroke()}
        }
        x.restore();
    },

    _klines(W,H){
        const x=this.ctx,d=this.data;if(!d||!d.keyLines)return;
        for(const l of d.keyLines){
            const py=this.py(l.y);
            if(py<this.mt-10||py>H-this.mb+10)continue;
            const px0=l.x0!=null?Math.max(this.px(l.x0),this.ml):this.ml;
            const px1=l.x1!=null?Math.min(this.px(l.x1),W-this.mr):W-this.mr;
            x.save();x.strokeStyle=l.color;x.lineWidth=l.dash?2:1;
            if(l.dash)x.setLineDash([6,4]);
            x.beginPath();x.moveTo(px0,py);x.lineTo(px1,py);x.stroke();x.restore();
            if(l.label){
                x.font='10px Consolas,monospace';x.fillStyle=l.color;
                x.textAlign='left';x.textBaseline='bottom';
                x.fillText(l.label,px0+4,py-3);
            }
        }
    },

    _markers(W,H){
        const x=this.ctx,d=this.data;if(!d||!d.markers)return;
        for(const m of d.markers){
            const py=this.py(m.y);
            if(py<this.mt-5||py>H-this.mb+5)continue;
            // px: if m.x is set, use data coordinate; else use margin side
            let px,dir;
            if(m.x!=null){
                px=this.px(m.x);dir=m.dir||1;
                if(px<this.ml-5||px>W-this.mr+5)continue;
            }else{
                px=m.side==='left'?this.ml:W-this.mr;
                dir=m.side==='left'?1:-1;
            }
            const sz=6;
            // Draw triangle
            x.save();
            x.fillStyle=m.color;
            x.beginPath();
            x.moveTo(px,py);
            x.lineTo(px+dir*sz*1.5,py-sz);
            x.lineTo(px+dir*sz*1.5,py+sz);
            x.closePath();x.fill();
            // Label
            x.font='bold 9px Consolas,monospace';x.fillStyle=m.color;
            x.textBaseline='middle';
            if(dir>0){x.textAlign='left';x.fillText(m.label,px+dir*sz*1.5+3,py)}
            else{x.textAlign='right';x.fillText(m.label,px+dir*sz*1.5-3,py)}
            x.restore();
        }
    },

    _counts(W,H){
        const x=this.ctx,d=this.data;if(!d)return;
        // Margin counts (merged/expanded)
        if(d.marginCounts){
            x.font='11px Consolas,monospace';x.fillStyle='#888';
            x.textAlign='right';x.textBaseline='middle';
            for(const [bc,cnt] of d.marginCounts){
                const py=this.py(bc);
                if(py>this.mt&&py<H-this.mb)x.fillText(String(cnt),this.ml-8,py);
            }
            if(d.marginCounts.length){
                x.font='bold 11px Consolas,monospace';x.fillStyle='#d1d4dc';
                const topY=Math.min(...d.marginCounts.map(c=>this.py(c[0])));
                x.fillText(d.countHeader||'TPO',this.ml-8,topY-14);
            }
        }
        // In-plot count labels (continuous mode)
        if(d.plotCounts){
            x.font='10px Consolas,monospace';x.fillStyle='#888';
            x.textAlign='right';x.textBaseline='middle';
            for(const [px_d,py_d,txt] of d.plotCounts){
                const px=this.px(px_d),py=this.py(py_d);
                if(px>this.ml-5&&px<W-this.mr+5&&py>this.mt&&py<H-this.mb)
                    x.fillText(txt,px,py);
            }
        }
    },

    _axes(W,H){
        const x=this.ctx;
        // Price axis (right)
        x.fillStyle='#d1d4dc';x.font='10px Consolas,monospace';
        x.textAlign='left';x.textBaseline='middle';
        const ys=niceStep(this.vy1-this.vy0,10);
        for(let y=Math.ceil(this.vy0/ys)*ys;y<=this.vy1;y+=ys){
            const py=this.py(y);
            if(py>this.mt+5&&py<H-this.mb-5)x.fillText(y.toFixed(2),W-this.mr+6,py);
        }
        // X-axis labels
        if(this.data&&this.data.xLabels){
            x.textAlign='center';x.textBaseline='top';x.fillStyle='#d1d4dc';
            x.font='10px Consolas,monospace';
            for(const[xv,lab] of this.data.xLabels){
                const px=this.px(xv);
                if(px>this.ml+10&&px<W-this.mr-10)x.fillText(lab,px,H-this.mb+5);
            }
        }
        // Borders
        x.strokeStyle='#2B2B43';x.lineWidth=1;
        x.beginPath();x.moveTo(this.ml,this.mt);x.lineTo(this.ml,H-this.mb);x.stroke();
        x.beginPath();x.moveTo(W-this.mr,this.mt);x.lineTo(W-this.mr,H-this.mb);x.stroke();
        x.beginPath();x.moveTo(this.ml,H-this.mb);x.lineTo(W-this.mr,H-this.mb);x.stroke();
    },

    _cross(W,H){
        const x=this.ctx;
        if(this.mx<this.ml||this.mx>W-this.mr||this.my<this.mt||this.my>H-this.mb)return;
        x.save();x.strokeStyle='rgba(200,200,200,0.25)';x.setLineDash([4,4]);x.lineWidth=1;
        x.beginPath();x.moveTo(this.mx,this.mt);x.lineTo(this.mx,H-this.mb);x.stroke();
        x.beginPath();x.moveTo(this.ml,this.my);x.lineTo(W-this.mr,this.my);x.stroke();
        x.restore();
        // Tooltip
        const dy=this.dy(this.my);
        let txt=`Price: ${dy.toFixed(2)}`;
        if(this.data&&this.data.binCounts){
            let best=null,bd=1e18;
            for(const[bc,cnt] of this.data.binCounts){
                const d=Math.abs(bc-dy);if(d<bd){bd=d;best=[bc,cnt]}}
            if(best)txt+=`\nBin ${best[0].toFixed(2)}: ${best[1]}`;
        }
        const lines=txt.split('\n');
        x.font='11px Consolas,monospace';
        const lh=16,mw=Math.max(...lines.map(l=>x.measureText(l).width));
        const bw=mw+16,bh=lines.length*lh+10;
        let tx=this.mx+14,ty=this.my-bh-6;
        if(tx+bw>W-8)tx=this.mx-bw-14;if(ty<8)ty=this.my+14;
        x.fillStyle='rgba(30,34,45,0.92)';x.fillRect(tx,ty,bw,bh);
        x.strokeStyle='#555';x.lineWidth=1;x.setLineDash([]);x.strokeRect(tx,ty,bw,bh);
        x.fillStyle='#d1d4dc';x.textAlign='left';x.textBaseline='top';
        for(let i=0;i<lines.length;i++)x.fillText(lines[i],tx+8,ty+5+i*lh);
    },

    _title(W,H){
        const x=this.ctx,d=this.data;if(!d||!d.title)return;
        x.font='bold 12px Consolas,monospace';x.fillStyle='#d1d4dc';
        x.textAlign='left';x.textBaseline='top';
        x.fillText(d.title,this.ml+6,6);
        // Subtitle with params
        if(d.subtitle){
            x.font='10px Consolas,monospace';x.fillStyle='#888';
            x.fillText(d.subtitle,this.ml+6,20);
        }
    },

    // ---------- events ----------
    // Detect which zone the mouse is in
    _zone(x,y){
        const W=this.W(),H=this.H();
        if(x>=this.ml&&x<=W-this.mr&&y>=this.mt&&y<=H-this.mb)return 'plot';
        if(x>W-this.mr&&y>=this.mt&&y<=H-this.mb)return 'yaxis';
        if(y>H-this.mb&&x>=this.ml&&x<=W-this.mr)return 'xaxis';
        return 'other';
    },

    _onWheel(e){
        e.preventDefault();
        const f=e.deltaY>0?1.1:0.9;
        const zone=this._zone(e.offsetX,e.offsetY);
        const dx=this.dx(this.mx),dy=this.dy(this.my);
        if(zone==='yaxis'){
            // Zoom Y only (stretch price axis)
            this.vy0=dy-(dy-this.vy0)*f;this.vy1=dy+(this.vy1-dy)*f;
        }else if(zone==='xaxis'){
            // Zoom X only (stretch time axis)
            this.vx0=dx-(dx-this.vx0)*f;this.vx1=dx+(this.vx1-dx)*f;
        }else{
            // Zoom both
            this.vx0=dx-(dx-this.vx0)*f;this.vx1=dx+(this.vx1-dx)*f;
            this.vy0=dy-(dy-this.vy0)*f;this.vy1=dy+(this.vy1-dy)*f;
        }
        this.render();
    },
    _onDown(e){
        this.dragging=true;this._dsx=e.offsetX;this._dsy=e.offsetY;
        this._dvx0=this.vx0;this._dvy0=this.vy0;
        this._dvx1=this.vx1;this._dvy1=this.vy1;
        this._dragZone=this._zone(e.offsetX,e.offsetY);
    },
    _onMove(e){
        this.mx=e.offsetX;this.my=e.offsetY;
        // Update cursor based on zone
        const zone=this._zone(e.offsetX,e.offsetY);
        this.canvas.style.cursor=this.dragging?'grabbing':
            zone==='yaxis'?'ns-resize':zone==='xaxis'?'ew-resize':
            zone==='plot'?'crosshair':'default';
        if(this.dragging){
            const ddx=(e.offsetX-this._dsx)/this.pW()*(this._dvx1-this._dvx0);
            const ddy=(e.offsetY-this._dsy)/this.pH()*(this._dvy1-this._dvy0);
            if(this._dragZone==='yaxis'){
                // Drag on price axis = stretch Y (scale around center)
                const cy=(this._dvy0+this._dvy1)/2;
                const f=1+ddy/((this._dvy1-this._dvy0)||1);
                this.vy0=cy-(cy-this._dvy0)*f;this.vy1=cy+(this._dvy1-cy)*f;
            }else if(this._dragZone==='xaxis'){
                // Drag on time axis = stretch X (scale around center)
                const cx=(this._dvx0+this._dvx1)/2;
                const f=1-ddx/((this._dvx1-this._dvx0)||1);
                this.vx0=cx-(cx-this._dvx0)*f;this.vx1=cx+(this._dvx1-cx)*f;
            }else{
                // Drag in plot area = pan
                this.vx0=this._dvx0-ddx;this.vx1=this._dvx1-ddx;
                this.vy0=this._dvy0+ddy;this.vy1=this._dvy1+ddy;
            }
        }
        this.render();
    },
};

// ===================== TOGGLE CANDLE =====================
function toggleCandle(show) {
    const cw = document.getElementById('candle-wrap');
    const dv = document.getElementById('divider');
    if (show) {
        cw.style.display = '';
        dv.style.display = '';
        cw.style.flex = '0 0 38%';
    } else {
        cw.style.display = 'none';
        dv.style.display = 'none';
    }
}
// Start hidden (default: profile only)
toggleCandle(false);

// ===================== DIVIDER (drag to resize) =====================
(function(){
    const div=document.getElementById('divider');
    const cw=document.getElementById('candle-wrap');
    const wrap=document.getElementById('wrap');
    let dragging=false,startY,startH;
    div.addEventListener('mousedown',e=>{
        dragging=true;startY=e.clientY;startH=cw.offsetHeight;
        document.body.style.cursor='row-resize';e.preventDefault();
    });
    document.addEventListener('mousemove',e=>{
        if(!dragging)return;
        const h=Math.max(80,Math.min(wrap.offsetHeight-120,startH+(e.clientY-startY)));
        cw.style.flex='0 0 '+h+'px';
    });
    document.addEventListener('mouseup',()=>{dragging=false;document.body.style.cursor=''});
})();

// ===================== INIT =====================
PR.init();
</script>
</body></html>"""


class UnifiedChart(QWebEngineView):
    """Single web-view containing TradingView candlestick + Canvas profile."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready = False
        self._pending: list[str] = []
        html = _HTML.replace("__LWC_CDN__", LWC_CDN)
        self.loadFinished.connect(self._on_load)
        self.setHtml(html, QUrl("https://unpkg.com/"))

    def _on_load(self, ok: bool):
        self._ready = ok
        for js in self._pending:
            self.page().runJavaScript(js)
        self._pending.clear()

    def _js(self, code: str):
        if self._ready:
            self.page().runJavaScript(code)
        else:
            self._pending.append(code)

    # ------------------------------------------------------------------ public
    def toggle_candle(self, show: bool):
        self._js(f"toggleCandle({'true' if show else 'false'});")

    def render(self, r: ProfileResult, bin_size: float,
               candle_df: pd.DataFrame | None = None,
               show_letters: bool = True, view_mode: str = "merged",
               count_metric: str = "tpo", style: str = "merged",
               visibility: dict | None = None, ib_minutes: int = 60):
        vis = visibility or {"poc": True, "vah": True, "val": True,
                             "open": True, "close": True, "mid": True,
                             "ib_high": True, "ib_low": True}
        # Override key levels for minute metric
        if count_metric == "minute" and r.minute_counts:
            lvl = minute_key_levels(r)
            r = replace(r, poc=lvl["poc"], vah=lvl["vah"],
                        val=lvl["val"], total_tpo=lvl["total_tpo"])

        # Candlestick
        if candle_df is not None and not candle_df.empty:
            self._js("toggleCandle(true);")
            self._send_candles(candle_df, r)
        else:
            self._js("toggleCandle(false);")

        # Profile
        self._vis = vis
        self._ib_minutes = ib_minutes
        if view_mode == "continuous" and r.components:
            expanded = (style == "expanded")
            data = self._prep_continuous(r, bin_size, show_letters, count_metric, expanded)
        elif view_mode == "merged":
            data = self._prep_merged(r, bin_size, show_letters, count_metric)
        elif r.components:
            data = self._prep_composite(r, bin_size, show_letters, count_metric)
        else:
            data = self._prep_expanded(r, bin_size, show_letters, count_metric)

        if data:
            # Filter key lines and markers based on visibility
            data["keyLines"] = self._filter_kl(data.get("keyLines", []))
            data["markers"] = self._filter_markers(data.get("markers", []))
            if not vis.get("vah", True) or not vis.get("val", True):
                data["vaBands"] = []
            self._js(f"PR.setData({json.dumps(data)});")

    # --------------------------------------------------------- visibility filters
    def _filter_kl(self, klines: list) -> list:
        """Filter key lines based on visibility settings."""
        vis = self._vis
        out = []
        for kl in klines:
            lbl = kl.get("label", "")
            if lbl.startswith("POC") and not vis.get("poc", True):
                continue
            if lbl.startswith("VAH") and not vis.get("vah", True):
                continue
            if lbl.startswith("VAL") and not vis.get("val", True):
                continue
            if lbl.startswith("Mid") and not vis.get("mid", True):
                continue
            if lbl.startswith("IB H") and not vis.get("ib_high", True):
                continue
            if lbl.startswith("IB L") and not vis.get("ib_low", True):
                continue
            out.append(kl)
        return out

    def _filter_markers(self, markers: list) -> list:
        """Filter markers (O/C) based on visibility settings."""
        vis = self._vis
        out = []
        for m in markers:
            lbl = m.get("label", "")
            if lbl.startswith("O") and not vis.get("open", True):
                continue
            if lbl.startswith("C") and not vis.get("close", True):
                continue
            out.append(m)
        return out

    # --------------------------------------------------------- candle helpers
    def _send_candles(self, df: pd.DataFrame, r: ProfileResult):
        candles, vols = [], []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            if isinstance(ts, str):
                ts = pd.Timestamp(ts)
            t = int(ts.timestamp())
            candles.append({"time": t, "open": float(row["open"]),
                            "high": float(row["high"]), "low": float(row["low"]),
                            "close": float(row["close"])})
            vols.append(float(row.get("volume", 0)))
        self._js(f"setCandles({json.dumps(candles)},{json.dumps(vols)});")
        self._js(f"setCandleLevels({r.poc},{r.vah},{r.val});")

    # -------------------------------------------------------- profile helpers
    @staticmethod
    def _label_counts(r: ProfileResult, bin_size: float,
                      starts: list, count_metric: str) -> list[int]:
        if count_metric == "minute" and r.minute_counts:
            m_starts, m_counts = bin_minute_counts(r, bin_size)
        else:
            m_starts, m_counts = bin_counts(r, bin_size)
        m = dict(zip(m_starts, m_counts))
        return [m.get(s, 0) for s in starts]

    @staticmethod
    def _bin_poc(r: ProfileResult, bin_size: float) -> float:
        """Return the bin-start price of the bin with the highest count (true bin-level POC)."""
        starts, counts = bin_counts(r, bin_size)
        if not starts:
            return round(np.floor(r.poc / bin_size) * bin_size, 8)
        max_idx = max(range(len(counts)), key=lambda i: counts[i])
        return round(starts[max_idx], 8)

    @staticmethod
    def _meta(r: ProfileResult, bin_size: float) -> dict:
        return {
            "subtitle": (f"period={r.period_minutes}m  tick={r.tick_size}  "
                         f"bin={bin_size}  VA={r.value_area_pct*100:.0f}%"),
            "palette": TPO_PALETTE,
            "binSize": bin_size,
            "pocBins": [],  # filled by each _prep method
        }

    @staticmethod
    def _ocm_markers(r: ProfileResult, side: str = "right",
                     x0=None, x1=None) -> tuple[list, list]:
        """Return (key_lines, markers) for Open / Close / Mid / IB."""
        klines = [
            {"y": r.mid_price, "color": "#ff9800", "dash": True,
             "label": f"Mid {r.mid_price:.2f}", "x0": x0, "x1": x1},
            {"y": r.ib_high, "color": "#9c27b0", "dash": False,
             "label": f"IB H {r.ib_high:.2f}", "x0": x0, "x1": x1},
            {"y": r.ib_low, "color": "#9c27b0", "dash": False,
             "label": f"IB L {r.ib_low:.2f}", "x0": x0, "x1": x1},
        ]
        markers = [
            {"y": r.open_price, "color": "#2196f3", "label": f"O {r.open_price:.2f}",
             "side": side},
            {"y": r.close_price, "color": "#ff5722", "label": f"C {r.close_price:.2f}",
             "side": side},
        ]
        return klines, markers

    def _global_kl(self, r: ProfileResult, bin_size: float = None) -> tuple[list, list]:
        if bin_size:
            poc_bin = self._bin_poc(r, bin_size)
            poc_y = poc_bin + bin_size / 2
            # VAH/VAL: snap to bin center
            vah_y = round(np.floor(r.vah / bin_size) * bin_size, 8) + bin_size / 2
            val_y = round(np.floor(r.val / bin_size) * bin_size, 8) + bin_size / 2
        else:
            poc_y, vah_y, val_y = r.poc, r.vah, r.val
        kl = [
            {"y": poc_y, "color": "#d62728", "dash": True,
             "label": f"POC {poc_y:.2f}", "x0": None, "x1": None},
            {"y": vah_y, "color": "#2ca02c", "dash": False,
             "label": f"VAH {vah_y:.2f}", "x0": None, "x1": None},
            {"y": val_y, "color": "#2ca02c", "dash": False,
             "label": f"VAL {val_y:.2f}", "x0": None, "x1": None},
        ]
        va = [{"y0": val_y, "y1": vah_y, "x0": None, "x1": None}]
        return kl, va

    # ============================================================== MERGED
    def _prep_merged(self, r: ProfileResult, bin_size: float,
                     show_letters: bool, count_metric: str) -> dict:
        sources = r.components if r.components else [r]
        day_offsets, running = [], 0
        for src in sources:
            day_offsets.append(running)
            running += max(len(src.bracket_starts), 1)
        is_comp = len(sources) > 1

        # bin_start -> set of global bracket indices
        bsets: dict[float, set] = defaultdict(set)
        for di, src in enumerate(sources):
            off = day_offsets[di]
            for price, idxs in src.bracket_visits.items():
                bs = round(np.floor(price / bin_size) * bin_size, 8)
                bsets[bs].update(off + i for i in idxs)

        if not bsets:
            return None
        starts = sorted(bsets.keys())
        letter_fn = composite_letter if is_comp else bracket_letter
        poc_bin = self._bin_poc(r, bin_size)

        cells, max_col = [], 0
        for bs in starts:
            gidxs = sorted(bsets[bs])
            is_poc = bool(bs == poc_bin)
            for pos, gidx in enumerate(gidxs):
                cells.append([pos, bs, 1.0, bin_size, gidx,
                              letter_fn(gidx) if show_letters else "", is_poc])
                if pos > max_col:
                    max_col = pos

        lc = self._label_counts(r, bin_size, starts, count_metric)
        centers = [s + bin_size / 2 for s in starts]
        kl, va = self._global_kl(r, bin_size)
        ocm_kl, ocm_mk = self._ocm_markers(r)
        kl.extend(ocm_kl)
        unit = max(max_col + 1, 20)

        return {
            **self._meta(r, bin_size),
            "title": r.title or "Market Profile",
            "cells": cells,
            "pocBins": [self._bin_poc(r, bin_size)],
            "marginCounts": list(zip(centers, lc)),
            "countHeader": "Min" if count_metric == "minute" else "TPO",
            "plotCounts": None,
            "binCounts": list(zip(centers, lc)),
            "keyLines": kl, "vaBands": va, "markers": ocm_mk,
            "xLabels": [], "separators": [],
            "bounds": {"xMin": -unit * 0.18, "xMax": unit * 1.05,
                       "yMin": min(starts) - bin_size * 1.5,
                       "yMax": max(starts) + bin_size * 2.5},
        }

    # ============================================================= EXPANDED
    def _prep_expanded(self, r: ProfileResult, bin_size: float,
                       show_letters: bool, count_metric: str) -> dict:
        bl = bin_letters_per_bracket(r, bin_size)
        if not bl:
            return None
        poc_bin = self._bin_poc(r, bin_size)
        starts_set: set[float] = set()
        cells = []
        for (bs, idx), _ in bl.items():
            starts_set.add(bs)
            cells.append([idx, bs, 1.0, bin_size, idx,
                          bracket_letter(idx) if show_letters else "",
                          bool(bs == poc_bin)])
        starts = sorted(starts_set)
        centers = [s + bin_size / 2 for s in starts]
        lc = self._label_counts(r, bin_size, starts, count_metric)
        kl, va = self._global_kl(r, bin_size)
        ocm_kl, ocm_mk = self._ocm_markers(r)
        kl.extend(ocm_kl)
        nb = len(r.bracket_starts)
        xlabels = [[i + 0.5, b.strftime("%H:%M")] for i, b in enumerate(r.bracket_starts)]

        return {
            **self._meta(r, bin_size),
            "title": r.title or "Market Profile",
            "cells": cells,
            "pocBins": [self._bin_poc(r, bin_size)],
            "marginCounts": list(zip(centers, lc)),
            "countHeader": "Min" if count_metric == "minute" else "TPO",
            "plotCounts": None,
            "binCounts": list(zip(centers, lc)),
            "keyLines": kl, "vaBands": va, "markers": ocm_mk,
            "xLabels": xlabels, "separators": [],
            "bounds": {"xMin": -max(nb, 1) * 0.18, "xMax": nb + 0.5,
                       "yMin": min(starts) - bin_size * 1.5,
                       "yMax": max(starts) + bin_size * 2.5},
        }

    # ========================================================== COMPOSITE
    def _prep_composite(self, r: ProfileResult, bin_size: float,
                        show_letters: bool, count_metric: str) -> dict:
        comps = r.components
        if not comps:
            return None
        day_offsets, running = [], 0
        for day in comps:
            day_offsets.append(running)
            running += max(len(day.bracket_starts), 1)

        poc_bin = self._bin_poc(r, bin_size)
        cells, starts_set = [], set()
        for di, day in enumerate(comps):
            off = day_offsets[di]
            for (bs, idx), _ in bin_letters_per_bracket(day, bin_size).items():
                g = off + idx
                starts_set.add(bs)
                cells.append([g, bs, 1.0, bin_size, g,
                              composite_letter(g) if show_letters else "",
                              bool(bs == poc_bin)])

        starts = sorted(starts_set)
        centers = [s + bin_size / 2 for s in starts]
        lc = self._label_counts(r, bin_size, starts, count_metric)
        kl, va = self._global_kl(r, bin_size)
        ocm_kl, ocm_mk = self._ocm_markers(r)
        kl.extend(ocm_kl)

        xlabels, seps = [], []
        for di, day in enumerate(comps):
            n = max(len(day.bracket_starts), 1)
            mid = day_offsets[di] + n / 2
            if day.components and day.title:
                label = day.title  # e.g. "W 22-Apr→28-Apr"
            elif day.session_date:
                label = day.session_date.strftime("%d-%b")
            else:
                label = f"D{di+1}"
            xlabels.append([mid, label])
            if di > 0:
                seps.append(day_offsets[di])

        return {
            **self._meta(r, bin_size),
            "title": r.title or "Composite",
            "cells": cells,
            "pocBins": [self._bin_poc(r, bin_size)],
            "marginCounts": list(zip(centers, lc)),
            "countHeader": "Min" if count_metric == "minute" else "TPO",
            "plotCounts": None,
            "binCounts": list(zip(centers, lc)),
            "keyLines": kl, "vaBands": va, "markers": ocm_mk,
            "xLabels": xlabels, "separators": seps,
            "bounds": {"xMin": -running * 0.12, "xMax": running + 0.5,
                       "yMin": min(starts) - bin_size * 1.5,
                       "yMax": max(starts) + bin_size * 2.5},
        }

    # ========================================================= CONTINUOUS
    def _prep_continuous(self, r: ProfileResult, bin_size: float,
                         show_letters: bool, count_metric: str,
                         expanded: bool = False) -> dict:
        comps = r.components
        if not comps:
            return None

        COUNT_W, GAP = 3.0, 2.0
        all_cells, plot_counts = [], []
        klines, vabands = [], []
        xlabels, seps = [], []
        all_markers = []
        all_poc_bins = []
        all_starts: set[float] = set()
        x_cursor = 0.0

        for di, day in enumerate(comps):
            # per-day key levels
            if count_metric == "minute" and day.minute_counts:
                lvl = minute_key_levels(day)
                poc, vah, val = lvl["poc"], lvl["vah"], lvl["val"]
            else:
                poc, vah, val = day.poc, day.vah, day.val

            all_poc_bins.append(self._bin_poc(day, bin_size))

            bl = bin_letters_per_bracket(day, bin_size)
            per_bin: dict[float, list] = defaultdict(list)
            for (bs, idx), _ in bl.items():
                per_bin[bs].append(idx)
            for v in per_bin.values():
                v.sort()

            starts = sorted(per_bin.keys())
            n_brackets = max(len(day.bracket_starts), 1)

            if expanded:
                # Expanded: each cell at its bracket index (time slot)
                band_w = n_brackets
            else:
                # Merged: cells stacked by count
                band_w = max((len(v) for v in per_bin.values()), default=1)

            x0 = x_cursor + COUNT_W
            band_end = x0 + max(band_w, 1) + 0.5

            # cells
            day_poc_bin = all_poc_bins[-1]
            if expanded:
                for (bs, idx), _ in bl.items():
                    all_starts.add(bs)
                    all_cells.append([x0 + idx, bs, 1.0, bin_size, idx,
                                      bracket_letter(idx) if show_letters else "",
                                      bool(bs == day_poc_bin)])
            else:
                for bs, idxs in per_bin.items():
                    all_starts.add(bs)
                    for pos, idx in enumerate(idxs):
                        all_cells.append([x0 + pos, bs, 1.0, bin_size, idx,
                                          bracket_letter(idx) if show_letters else "",
                                          bool(bs == day_poc_bin)])

            # count column (in-plot)
            centers = [s + bin_size / 2 for s in starts]
            lc = self._label_counts(day, bin_size, starts, count_metric)
            cnt_x = x_cursor + COUNT_W * 0.85
            for c, v in zip(centers, lc):
                plot_counts.append([cnt_x, c, str(v)])

            # key levels bounded to day band — use bin centers for alignment
            poc_bc = all_poc_bins[-1] + bin_size / 2
            vah_bc = round(np.floor(vah / bin_size) * bin_size, 8) + bin_size / 2
            val_bc = round(np.floor(val / bin_size) * bin_size, 8) + bin_size / 2
            for yv, col, dsh, lbl in [
                (poc_bc, "#d62728", True, f"POC {poc_bc:.2f}"),
                (vah_bc, "#2ca02c", False, f"VAH {vah_bc:.2f}"),
                (val_bc, "#2ca02c", False, f"VAL {val_bc:.2f}"),
            ]:
                klines.append({"y": yv, "color": col, "dash": dsh,
                               "label": lbl, "x0": x_cursor, "x1": band_end})
            vabands.append({"y0": val, "y1": vah, "x0": x_cursor, "x1": band_end})

            # Per-day O/C/Mid
            ocm_kl, _ = self._ocm_markers(day, x0=x_cursor, x1=band_end)
            klines.extend(ocm_kl)

            # O/C markers at band edges
            all_markers.append({"y": day.open_price, "color": "#2196f3",
                                "label": "O", "x": x_cursor + 0.3, "dir": 1})
            all_markers.append({"y": day.close_price, "color": "#ff5722",
                                "label": "C", "x": band_end - 0.3, "dir": -1})

            mid = (x_cursor + band_end) / 2
            if day.components and day.title:
                label = day.title  # e.g. "W 22-Apr→28-Apr"
            elif day.session_date:
                label = day.session_date.strftime("%d-%b")
            else:
                label = f"D{di+1}"
            xlabels.append([mid, label])
            if di > 0:
                seps.append(x_cursor)

            x_cursor = band_end + GAP

        if not all_starts:
            return None
        slist = sorted(all_starts)

        # merged counts for tooltip
        merged_starts, merged_counts = (
            bin_minute_counts(r, bin_size) if count_metric == "minute" and r.minute_counts
            else bin_counts(r, bin_size))
        mcenters = [s + bin_size / 2 for s in merged_starts]

        return {
            **self._meta(r, bin_size),
            "title": r.title or "Continuous",
            "cells": all_cells,
            "pocBins": all_poc_bins,
            "marginCounts": None,
            "plotCounts": plot_counts,
            "countHeader": "Min" if count_metric == "minute" else "TPO",
            "binCounts": list(zip(mcenters, merged_counts)),
            "keyLines": klines, "vaBands": vabands, "markers": all_markers,
            "xLabels": xlabels, "separators": seps,
            "bounds": {"xMin": -1, "xMax": x_cursor,
                       "yMin": min(slist) - bin_size * 1.5,
                       "yMax": max(slist) + bin_size * 2.5},
        }
