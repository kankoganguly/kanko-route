#!/usr/bin/env python3
"""
WMS - Inventory Management System
Modules: Warehouse Locations, Item Master, Inventory, Cycle Count
Local web app — no external packages required.
"""

import json
import os
import sys
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path

PORT      = int(os.environ.get("PORT", 7825))
DATA_FILE = Path(__file__).parent / "wms_data.json"


# ── Data ──────────────────────────────────────────────────────────────────────

def load():
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
            d.setdefault("locations", [])
            d.setdefault("items", [])
            d.setdefault("inventory", {})
            d.setdefault("cycle_counts", [])
            return d
        except Exception:
            pass
    return {"locations": [], "items": [], "inventory": {}, "cycle_counts": []}

def save(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def today():
    return date.today().isoformat()

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── Locations ─────────────────────────────────────────────────────────────────

def get_locations():
    return load()["locations"]

def create_location(body):
    data  = load()
    aisle = str(body.get("aisle", "")).strip().upper()
    row   = str(body.get("row",   "")).strip()
    rack  = str(body.get("rack",  "")).strip()
    bin_  = str(body.get("bin",   "")).strip().upper()
    if not all([aisle, row, rack, bin_]):
        return {"error": "All four fields are required"}, 400
    label = f"{aisle}-{row}-{rack}-{bin_}"
    if any(f"{l['aisle']}-{l['row']}-{l['rack']}-{l['bin']}" == label for l in data["locations"]):
        return {"error": f"Location {label} already exists"}, 409
    loc = {"id": str(uuid.uuid4())[:8], "aisle": aisle, "row": row, "rack": rack, "bin": bin_}
    data["locations"].append(loc)
    save(data)
    return loc, 201

def update_location(loc_id, body):
    data = load()
    for loc in data["locations"]:
        if loc["id"] == loc_id:
            loc["aisle"] = str(body.get("aisle", loc["aisle"])).strip().upper()
            loc["row"]   = str(body.get("row",   loc["row"])).strip()
            loc["rack"]  = str(body.get("rack",  loc["rack"])).strip()
            loc["bin"]   = str(body.get("bin",   loc["bin"])).strip().upper()
            loc["label"] = str(body.get("label", loc.get("label", ""))).strip()
            loc["notes"] = str(body.get("notes", loc.get("notes", ""))).strip()
            save(data)
            return loc, 200
    return {"error": "Location not found"}, 404

def delete_location(loc_id):
    data = load()
    if any(i.get("location_id") == loc_id for i in data["items"]):
        return {"error": "Location is assigned to one or more SKUs"}, 409
    before = len(data["locations"])
    data["locations"] = [l for l in data["locations"] if l["id"] != loc_id]
    if len(data["locations"]) == before:
        return {"error": "Location not found"}, 404
    save(data)
    return {"deleted": loc_id}, 200


# ── Items ─────────────────────────────────────────────────────────────────────

def get_items(search=None):
    items = load()["items"]
    if search:
        s = search.lower()
        items = [i for i in items if
                 s in i.get("sku", "").lower() or
                 s in i.get("name", "").lower() or
                 s in i.get("supplier", "").lower()]
    return items

def create_item(body):
    data = load()
    if len(data["items"]) >= 1000:
        return {"error": "Maximum of 1000 SKUs reached"}, 400
    sku  = str(body.get("sku",  "")).strip().upper()
    name = str(body.get("name", "")).strip()
    if not sku or not name:
        return {"error": "SKU and Name are required"}, 400
    if any(i["sku"] == sku for i in data["items"]):
        return {"error": f"SKU '{sku}' already exists"}, 409
    item = {
        "sku":         sku,
        "name":        name,
        "supplier":    str(body.get("supplier",   "")).strip(),
        "reorder_qty": int(body.get("reorder_qty", 0)),
        "min_stock":   int(body.get("min_stock",   0)),
        "max_stock":   int(body.get("max_stock",   0)),
        "location_id": str(body.get("location_id", "")),
        "uom":         str(body.get("uom", "EA")).strip() or "EA",
        "created":     today(),
    }
    data["items"].append(item)
    data["inventory"][sku] = {"onhand": 0, "last_counted": None, "last_adjusted": None, "history": []}
    save(data)
    return item, 201

def update_item(sku, body):
    data = load()
    for item in data["items"]:
        if item["sku"] == sku:
            item["name"]        = str(body.get("name",        item["name"])).strip()
            item["supplier"]    = str(body.get("supplier",    item["supplier"])).strip()
            item["reorder_qty"] = int(body.get("reorder_qty", item["reorder_qty"]))
            item["min_stock"]   = int(body.get("min_stock",   item["min_stock"]))
            item["max_stock"]   = int(body.get("max_stock",   item["max_stock"]))
            item["location_id"] = str(body.get("location_id", item.get("location_id", "")))
            item["uom"]         = str(body.get("uom",         item.get("uom", "EA"))).strip() or "EA"
            save(data)
            return item, 200
    return {"error": "SKU not found"}, 404

def delete_item(sku):
    data   = load()
    before = len(data["items"])
    data["items"] = [i for i in data["items"] if i["sku"] != sku]
    if len(data["items"]) == before:
        return {"error": "SKU not found"}, 404
    data["inventory"].pop(sku, None)
    save(data)
    return {"deleted": sku}, 200


# ── Inventory ─────────────────────────────────────────────────────────────────

def _loc_map(data):
    return {l["id"]: f"{l['aisle']}-{l['row']}-{l['rack']}-{l['bin']}"
            for l in data["locations"]}

def get_inventory():
    data    = load()
    inv     = data["inventory"]
    lm      = _loc_map(data)
    result  = []
    for item in data["items"]:
        sku    = item["sku"]
        rec    = inv.get(sku, {})
        onhand = rec.get("onhand", 0)
        min_s  = item.get("min_stock", 0)
        max_s  = item.get("max_stock", 0)
        if max_s > 0 and onhand > max_s:
            status = "overstock"
        elif onhand <= min_s:
            status = "low"
        else:
            status = "ok"
        result.append({
            "sku":          sku,
            "name":         item["name"],
            "supplier":     item.get("supplier", ""),
            "onhand":       onhand,
            "min_stock":    min_s,
            "max_stock":    max_s,
            "reorder_qty":  item.get("reorder_qty", 0),
            "location":     lm.get(item.get("location_id", ""), ""),
            "status":       status,
            "last_counted": rec.get("last_counted"),
            "last_adjusted":rec.get("last_adjusted"),
        })
    return result

def adjust_inventory(sku, body):
    data = load()
    if not any(i["sku"] == sku for i in data["items"]):
        return {"error": "SKU not found"}, 404
    data["inventory"].setdefault(
        sku, {"onhand": 0, "last_counted": None, "last_adjusted": None, "history": []})
    adj_type  = body.get("type", "adjust")   # "set" or "adjust"
    qty       = int(body.get("qty", 0))
    reason    = str(body.get("reason", "Manual adjustment")).strip()
    inv       = data["inventory"][sku]
    old       = inv.get("onhand", 0)
    new       = max(0, qty) if adj_type == "set" else max(0, old + qty)
    inv["onhand"]        = new
    inv["last_adjusted"] = ts()
    inv.setdefault("history", [])
    inv["history"].append({"date": ts(), "type": adj_type, "qty": qty,
                           "old": old, "new": new, "reason": reason})
    inv["history"] = inv["history"][-50:]
    save(data)
    return {"sku": sku, "old": old, "onhand": new}, 200


# ── Cycle count ───────────────────────────────────────────────────────────────

def get_cycle_count_today():
    data = load()
    inv  = data["inventory"]
    lm   = _loc_map(data)
    rows = []
    for item in data["items"]:
        sku    = item["sku"]
        onhand = inv.get(sku, {}).get("onhand", 0)
        rows.append({
            "sku":       sku,
            "name":      item["name"],
            "location":  lm.get(item.get("location_id", ""), ""),
            "onhand":    onhand,
            "min_stock": item.get("min_stock", 0),
            "max_stock": item.get("max_stock", 0),
        })
    top10       = sorted(rows, key=lambda x: x["onhand"], reverse=True)[:10]
    today_done  = [c for c in data["cycle_counts"] if c.get("date") == today()]
    return {
        "date":      today(),
        "items":     top10,
        "completed": bool(today_done),
        "last_count": today_done[-1] if today_done else None,
    }

def submit_cycle_count(body):
    data   = load()
    counts = body.get("counts", [])
    if not counts:
        return {"error": "No counts provided"}, 400
    record = {"date": today(), "timestamp": ts(), "items": []}
    for c in counts:
        sku     = c.get("sku", "")
        counted = int(c.get("counted", 0))
        expected = data["inventory"].get(sku, {}).get("onhand", 0)
        record["items"].append({"sku": sku, "expected": expected,
                                "counted": counted, "variance": counted - expected})
        if sku in data["inventory"]:
            data["inventory"][sku]["onhand"]       = counted
            data["inventory"][sku]["last_counted"] = ts()
    data["cycle_counts"].append(record)
    data["cycle_counts"] = data["cycle_counts"][-90:]
    save(data)
    return record, 201

def get_cycle_count_history():
    return list(reversed(load()["cycle_counts"][-10:]))


# ── Layout ────────────────────────────────────────────────────────────────────

def get_layout():
    data    = load()
    inv     = data["inventory"]
    loc_sku = {i.get("location_id", ""): i["sku"]
               for i in data["items"] if i.get("location_id")}
    item_map = {i["sku"]: i for i in data["items"]}

    tree   = {}
    aisles = []
    for loc in sorted(data["locations"],
                      key=lambda l: (l["aisle"], l["row"], l["rack"], l["bin"])):
        a, r, rk, b = loc["aisle"], loc["row"], loc["rack"], loc["bin"]
        if a not in tree:
            tree[a] = {}
            aisles.append(a)
        if r not in tree[a]:
            tree[a][r] = {}
        if rk not in tree[a][r]:
            tree[a][r][rk] = []

        sku    = loc_sku.get(loc["id"], "")
        onhand = inv.get(sku, {}).get("onhand", 0) if sku else 0
        item   = item_map.get(sku, {})
        min_s  = item.get("min_stock", 0)
        max_s  = item.get("max_stock", 0)
        if sku:
            if max_s > 0 and onhand > max_s:
                status = "overstock"
            elif onhand <= min_s:
                status = "low"
            else:
                status = "ok"
        else:
            status = "empty"

        tree[a][r][rk].append({
            "id":     loc["id"],
            "aisle":  a,
            "row":    r,
            "rack":   rk,
            "bin":    b,
            "label":  loc.get("label", ""),
            "notes":  loc.get("notes", ""),
            "sku":    sku,
            "onhand": onhand,
            "status": status,
        })

    return {"aisles": aisles, "tree": tree}


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Inventory Management</title>
<style>
:root {
  --bg:#0d0d0f; --surface:#131316; --surface2:#1a1a1f;
  --border:#252528; --text:#f0f0f0; --muted:#999;
  --accent:#6366f1; --success:#22c55e; --warn:#f59e0b; --error:#ef4444;
  --sidebar:220px; --hdr:52px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column}

/* Header */
.hdr{height:var(--hdr);background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:12px;flex-shrink:0;z-index:100}
.hdr-logo{font-size:1rem;font-weight:800;color:var(--accent);letter-spacing:-.02em}
.hdr-sep{color:#333;font-size:1.1rem}
.hdr-mod{font-size:0.85rem;color:#bbb;font-weight:500}
.hdr-right{margin-left:auto;font-size:0.72rem;color:#444}

/* Layout */
.body{display:flex;flex:1;overflow:hidden}
.sidebar{width:var(--sidebar);flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:10px 8px;overflow-y:auto}
.main{flex:1;overflow-y:auto;padding:24px}
.main::-webkit-scrollbar{width:5px}
.main::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:3px}

/* Nav */
.nav-lbl{font-size:0.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#3a3a3a;padding:14px 10px 5px}
.nav-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:7px;cursor:pointer;font-size:0.84rem;color:var(--muted);transition:background .1s,color .1s;user-select:none}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:rgba(99,102,241,.18);color:#a5b4fc}
.nav-ic{width:18px;text-align:center;flex-shrink:0;font-size:.95rem}

/* Page */
.pg-title{font-size:1.1rem;font-weight:700;margin-bottom:18px;color:#fff}
.pg-title span{font-size:.75rem;color:var(--muted);font-weight:400;margin-left:8px}

/* Stat grid */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px}
.stat-v{font-size:2rem;font-weight:800;line-height:1;margin-bottom:5px}
.stat-l{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.c-accent .stat-v{color:#818cf8}
.c-ok    .stat-v{color:var(--success)}
.c-warn  .stat-v{color:var(--warn)}
.c-err   .stat-v{color:var(--error)}

/* Toolbar */
.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.search{flex:1;min-width:160px;max-width:300px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:.84rem;outline:none;transition:border-color .15s}
.search::placeholder{color:#555}
.search:focus{border-color:#444}

/* Pills */
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{padding:5px 12px;border-radius:20px;font-size:.73rem;font-weight:600;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--muted);transition:all .1s}
.pill:hover{border-color:#444;color:var(--text)}
.pill.on{background:var(--accent);border-color:var(--accent);color:#fff}

/* Buttons */
.btn{padding:8px 16px;border:none;border-radius:7px;font-size:.84rem;font-weight:500;cursor:pointer;transition:opacity .15s;white-space:nowrap}
.btn:hover:not(:disabled){opacity:.85}
.btn:disabled{opacity:.35;cursor:default}
.btn-p{background:var(--accent);color:#fff}
.btn-ok{background:#14532d;color:#4ade80}
.btn-d{background:#3f0909;color:#f87171}
.btn-g{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-sm{padding:5px 11px;font-size:.76rem}

/* Table */
.tbl-wrap{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:.82rem}
thead tr{background:var(--surface2);border-bottom:1px solid var(--border)}
th{padding:9px 14px;text-align:left;font-size:.68rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#666;white-space:nowrap}
td{padding:9px 14px;border-bottom:1px solid #1c1c20;color:var(--text);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:"Consolas","Courier New",monospace;font-size:.79rem}
.td-m{color:var(--muted)}

/* Badges */
.bdg{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.67rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.bdg-ok{background:#052e16;color:#22c55e}
.bdg-low{background:#431407;color:#fb923c}
.bdg-over{background:#172554;color:#60a5fa}
.bdg-zero{background:#1c1917;color:#a8a29e}

/* Actions */
.acts{display:flex;gap:6px}

/* Pagination */
.pag{display:flex;align-items:center;gap:8px;padding:10px 14px;border-top:1px solid var(--border);font-size:.78rem;color:var(--muted)}
.pag-btn{padding:4px 10px;border:1px solid var(--border);border-radius:5px;background:var(--surface2);color:var(--text);cursor:pointer;font-size:.76rem}
.pag-btn:hover:not(:disabled){border-color:#444}
.pag-btn:disabled{opacity:.3;cursor:default}
.pag-info{flex:1;text-align:center}

/* Empty */
.empty{padding:44px 24px;text-align:center;color:#444;font-size:.88rem}
.empty-ic{font-size:2.2rem;margin-bottom:10px;opacity:.4;display:block}

/* Section card */
.sc{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:20px;overflow:hidden}
.sc-hdr{padding:13px 18px;border-bottom:1px solid var(--border);font-size:.82rem;font-weight:600;color:var(--text);display:flex;align-items:center;justify-content:space-between}
.sc-body{padding:16px 18px}

/* Modal */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:500;align-items:center;justify-content:center}
.overlay.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;width:460px;max-width:96vw;max-height:90vh;overflow-y:auto;padding:24px;box-shadow:0 24px 64px rgba(0,0,0,.55)}
.m-title{font-size:.95rem;font-weight:700;margin-bottom:18px;display:flex;justify-content:space-between;align-items:center;color:#fff}
.m-close{background:none;border:none;color:var(--muted);font-size:1.2rem;cursor:pointer;padding:2px 7px;border-radius:4px}
.m-close:hover{background:var(--surface2);color:var(--text)}
.m-foot{display:flex;justify-content:flex-end;gap:10px;margin-top:18px;padding-top:14px;border-top:1px solid var(--border)}

/* Form */
.fg{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.fg1{grid-template-columns:1fr}
.f{display:flex;flex-direction:column;gap:5px}
.f.s2{grid-column:1/-1}
.f label{font-size:.68rem;font-weight:700;color:#aaa;letter-spacing:.05em;text-transform:uppercase}
.f input,.f select{padding:8px 11px;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:.86rem;outline:none;transition:border-color .15s}
.f input:focus,.f select:focus{border-color:var(--accent)}
.f select option{background:#1a1a1f}
.f-hint{font-size:.68rem;color:#555;margin-top:2px}

/* Cycle count */
.cc-count-inp{width:88px;padding:6px 9px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:.84rem;text-align:right}
.cc-count-inp:focus{outline:none;border-color:var(--accent)}
.v-pos{color:var(--success);font-weight:700}
.v-neg{color:var(--error);font-weight:700}
.v-zero{color:#444}

/* Banner */
.banner-ok{background:#052e16;border:1px solid #14532d;border-radius:8px;padding:11px 16px;margin-bottom:16px;color:#4ade80;font-size:.83rem}
.banner-warn{background:#431407;border:1px solid #7c2d12;border-radius:8px;padding:11px 16px;margin-bottom:16px;color:#fb923c;font-size:.83rem}

/* Toast */
.toast{position:fixed;bottom:22px;right:22px;background:#1a1a1f;border:1px solid var(--border);border-radius:8px;padding:11px 18px;font-size:.84rem;box-shadow:0 8px 28px rgba(0,0,0,.45);z-index:1000;display:none;max-width:320px}
.toast.show{display:block;animation:ti .18s ease}
.toast.ok{border-left:3px solid var(--success)}
.toast.err{border-left:3px solid var(--error)}
@keyframes ti{from{transform:translateX(16px);opacity:0}to{transform:translateX(0);opacity:1}}

/* Divider */
.div{height:1px;background:var(--border);margin:4px 0}

/* Layout visualization */
.lay-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
.lay-tab{padding:7px 18px;border-radius:7px;font-size:.84rem;font-weight:600;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--muted);transition:all .1s;user-select:none}
.lay-tab:hover{border-color:#444;color:var(--text)}
.lay-tab.active{background:var(--accent);border-color:var(--accent);color:#fff}
.lay-row-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:14px;overflow:hidden}
.lay-row-hdr{padding:10px 16px;background:var(--surface2);border-bottom:1px solid var(--border);font-size:.75rem;font-weight:700;color:#aaa;letter-spacing:.07em;text-transform:uppercase}
.lay-row-body{padding:14px 16px;display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start}
.lay-rack-col{display:flex;flex-direction:column;gap:6px;min-width:76px}
.lay-rack-lbl{font-size:.6rem;font-weight:700;color:#555;letter-spacing:.08em;text-transform:uppercase;text-align:center;padding-bottom:5px;border-bottom:1px solid var(--border);margin-bottom:2px}
.bin-cell{width:76px;min-height:56px;border-radius:7px;border:1px solid var(--border);background:var(--surface2);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;transition:border-color .12s,transform .12s;padding:5px 4px;text-align:center}
.bin-cell:hover{border-color:#555;transform:scale(1.05)}
.bin-cell .bc-name{font-size:.65rem;font-weight:700;color:#bbb;line-height:1}
.bin-cell .bc-label{font-size:.56rem;color:#666;line-height:1;max-width:68px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bin-cell .bc-qty{font-size:.82rem;font-weight:800;line-height:1.1}
.bin-cell .bc-sku{font-size:.56rem;color:#555;line-height:1;max-width:68px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bin-cell.st-ok{border-color:#14532d;background:#052e16}.bin-cell.st-ok .bc-qty{color:var(--success)}
.bin-cell.st-low{border-color:#7c2d12;background:#431407}.bin-cell.st-low .bc-qty{color:var(--warn)}
.bin-cell.st-over{border-color:#1d4ed8;background:#172554}.bin-cell.st-over .bc-qty{color:#60a5fa}
.bin-cell.st-empty{opacity:.5}
.lay-add{width:76px;min-height:56px;border-radius:7px;border:1px dashed #2a2a2a;background:transparent;cursor:pointer;display:flex;align-items:center;justify-content:center;color:#333;font-size:1.4rem;transition:all .12s}
.lay-add:hover{border-color:#555;color:#666}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <span class="hdr-logo">WMS</span>
  <span class="hdr-sep">/</span>
  <span class="hdr-mod" id="hdr-mod">Dashboard</span>
  <span class="hdr-right" id="hdr-right"></span>
</div>

<div class="body">

  <!-- Sidebar -->
  <nav class="sidebar">
    <div class="nav-lbl">Inventory Module</div>
    <div class="nav-item active" data-m="dashboard"  onclick="nav('dashboard')">
      <span class="nav-ic">&#9632;</span> Dashboard
    </div>
    <div class="nav-lbl">Master Data</div>
    <div class="nav-item" data-m="locations" onclick="nav('locations')">
      <span class="nav-ic">&#9698;</span> Warehouse
    </div>
    <div class="nav-item" data-m="items" onclick="nav('items')">
      <span class="nav-ic">&#9873;</span> Item Master
    </div>
    <div class="nav-lbl">Operations</div>
    <div class="nav-item" data-m="inventory" onclick="nav('inventory')">
      <span class="nav-ic">&#9726;</span> Inventory
    </div>
    <div class="nav-item" data-m="cyclecount" onclick="nav('cyclecount')">
      <span class="nav-ic">&#10003;</span> Cycle Count
    </div>
    <div class="nav-lbl">Visualization</div>
    <div class="nav-item" data-m="layout" onclick="nav('layout')">
      <span class="nav-ic">&#9638;</span> Layout
    </div>
  </nav>

  <!-- Main content -->
  <main class="main">
    <div id="content"><div class="empty"><span class="empty-ic">&#9632;</span>Loading...</div></div>
  </main>
</div>

<!-- Modal: Location -->
<div class="overlay" id="loc-ov">
  <div class="modal">
    <div class="m-title"><span id="loc-m-title">Add Location</span><button class="m-close" onclick="closeM('loc-ov')">&#215;</button></div>
    <div class="fg">
      <div class="f"><label>Aisle</label><input id="lf-aisle" placeholder="e.g. A" maxlength="5"></div>
      <div class="f"><label>Row</label><input id="lf-row" placeholder="e.g. 01" maxlength="5"></div>
      <div class="f"><label>Rack</label><input id="lf-rack" placeholder="e.g. 1" maxlength="5"></div>
      <div class="f"><label>Bin</label><input id="lf-bin" placeholder="e.g. A" maxlength="5"></div>
    </div>
    <div class="m-foot">
      <button class="btn btn-g" onclick="closeM('loc-ov')">Cancel</button>
      <button class="btn btn-p" onclick="saveLoc()">Save</button>
    </div>
  </div>
</div>

<!-- Modal: Item -->
<div class="overlay" id="item-ov">
  <div class="modal">
    <div class="m-title"><span id="item-m-title">Add SKU</span><button class="m-close" onclick="closeM('item-ov')">&#215;</button></div>
    <div class="fg">
      <div class="f"><label>SKU *</label><input id="if-sku" placeholder="e.g. WDG-001" style="text-transform:uppercase"></div>
      <div class="f"><label>UOM</label><input id="if-uom" placeholder="EA" maxlength="8"></div>
      <div class="f s2"><label>Item Name *</label><input id="if-name" placeholder="Full item description"></div>
      <div class="f s2"><label>Supplier</label><input id="if-sup" placeholder="Supplier name"></div>
      <div class="f"><label>Min Stock</label><input id="if-min" type="number" min="0" placeholder="0"></div>
      <div class="f"><label>Max Stock</label><input id="if-max" type="number" min="0" placeholder="0"></div>
      <div class="f"><label>Reorder Qty</label><input id="if-reorder" type="number" min="0" placeholder="0"></div>
      <div class="f"><label>Location</label><select id="if-loc"><option value="">— None —</option></select></div>
    </div>
    <div class="m-foot">
      <button class="btn btn-g" onclick="closeM('item-ov')">Cancel</button>
      <button class="btn btn-p" onclick="saveItem()">Save</button>
    </div>
  </div>
</div>

<!-- Modal: Adjust inventory -->
<div class="overlay" id="adj-ov">
  <div class="modal" style="width:380px">
    <div class="m-title"><span>Adjust Inventory</span><button class="m-close" onclick="closeM('adj-ov')">&#215;</button></div>
    <div style="margin-bottom:16px">
      <div style="font-size:.9rem;font-weight:600;color:#fff;margin-bottom:3px" id="adj-sku-lbl"></div>
      <div style="font-size:.78rem;color:var(--muted)" id="adj-oh-lbl"></div>
    </div>
    <div class="fg fg1">
      <div class="f">
        <label>Adjustment Type</label>
        <select id="adj-type">
          <option value="adjust">Add / Remove (relative)</option>
          <option value="set">Set Exact Quantity</option>
        </select>
      </div>
      <div class="f">
        <label>Quantity</label>
        <input id="adj-qty" type="number" placeholder="e.g. -5 to remove, +10 to add">
        <span class="f-hint" id="adj-hint">Use negative to remove stock</span>
      </div>
      <div class="f">
        <label>Reason</label>
        <input id="adj-reason" placeholder="e.g. Receiving, Damage, Recount">
      </div>
    </div>
    <div class="m-foot">
      <button class="btn btn-g" onclick="closeM('adj-ov')">Cancel</button>
      <button class="btn btn-ok" onclick="saveAdj()">Apply</button>
    </div>
  </div>
</div>

<!-- Modal: Bin detail/edit -->
<div class="overlay" id="bin-ov">
  <div class="modal" style="width:430px">
    <div class="m-title">
      <span id="bin-m-title">Bin</span>
      <button class="m-close" onclick="closeM('bin-ov')">&#215;</button>
    </div>
    <div style="margin-bottom:14px">
      <span style="font-family:monospace;background:#1e1e28;border:1px solid #2a2a38;padding:3px 10px;border-radius:5px;font-size:.85rem;color:#a5b4fc" id="bin-code-lbl"></span>
    </div>
    <div class="fg fg1">
      <div class="f">
        <label>Custom Label</label>
        <input id="bf-label" placeholder="e.g. Heavy Parts, Returns Bay">
      </div>
      <div class="f">
        <label>Notes</label>
        <input id="bf-notes" placeholder="Optional notes about this bin">
      </div>
      <div class="f">
        <label>Assigned SKU</label>
        <select id="bf-sku" onchange="onBinSkuChange()">
          <option value="">&#8212; Unassigned &#8212;</option>
        </select>
      </div>
      <div class="f" id="bf-qty-wrap" style="display:none">
        <label>On-Hand Quantity</label>
        <input id="bf-qty" type="number" min="0" placeholder="0">
        <span class="f-hint">Sets the inventory record for this SKU</span>
      </div>
    </div>
    <div class="m-foot">
      <button class="btn btn-g" onclick="closeM('bin-ov')">Cancel</button>
      <button class="btn btn-p" onclick="saveBin()">Save</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  locations: [], items: [], locs_map: {},
  page: {items: 1, inv: 1},
  filter: {inv: 'all'},
  search: {locs: '', items: '', inv: ''},
  edit: null,
  ccItems: [],
};
const PS = 25;

// Registries (avoid JSON in onclick attrs)
const LREG = {};  // loc id  -> location obj
const IREG = {};  // sku     -> item obj
const NREG = {};  // sku     -> inventory row obj

// ── API ───────────────────────────────────────────────────────────────────────
const api = {
  async get(p)       { return (await fetch(p)).json(); },
  async post(p, b)   { const r = await fetch(p,{method:'POST',  headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return [r.status, await r.json()]; },
  async put(p, b)    { const r = await fetch(p,{method:'PUT',   headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return [r.status, await r.json()]; },
  async del(p)       { const r = await fetch(p,{method:'DELETE'}); return [r.status, await r.json()]; },
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function v(id){ return document.getElementById(id)?.value??''; }
function sv(id,val){ const e=document.getElementById(id); if(e) e.value=val??''; }
function el(id){ return document.getElementById(id); }

let _tt;
function toast(msg, type='ok'){
  const t=el('toast'); t.textContent=msg; t.className='toast show '+type;
  clearTimeout(_tt); _tt=setTimeout(()=>t.classList.remove('show'),3200);
}

function openM(id){ el(id).classList.add('open'); }
function closeM(id){ el(id).classList.remove('open'); }
document.addEventListener('keydown', e=>{ if(e.key==='Escape') document.querySelectorAll('.overlay.open').forEach(m=>m.classList.remove('open')); });

function setHdrRight(txt){ el('hdr-right').textContent = txt; }

// ── Navigation ────────────────────────────────────────────────────────────────
const MOD_TITLES = {dashboard:'Dashboard', locations:'Warehouse', items:'Item Master', inventory:'Inventory', cyclecount:'Cycle Count', layout:'Warehouse Layout'};
async function nav(mod){
  document.querySelectorAll('.nav-item').forEach(e=>e.classList.toggle('active', e.dataset.m===mod));
  el('hdr-mod').textContent = MOD_TITLES[mod]||mod;
  el('content').innerHTML = '<div class="empty"><span class="empty-ic">&#8987;</span>Loading...</div>';
  if      (mod==='dashboard')  await loadDash();
  else if (mod==='locations')  await loadLocs();
  else if (mod==='items')      await loadItems();
  else if (mod==='inventory')  await loadInv();
  else if (mod==='cyclecount') await loadCC();
  else if (mod==='layout')     await loadLayout();
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
async function loadDash(){
  const [locs, items, inv] = await Promise.all([
    api.get('/api/locations'), api.get('/api/items'), api.get('/api/inventory')
  ]);
  const low  = inv.filter(i=>i.status==='low').length;
  const over = inv.filter(i=>i.status==='overstock').length;
  const total= inv.reduce((s,i)=>s+i.onhand, 0);
  const zero = inv.filter(i=>i.onhand===0).length;
  setHdrRight(`${items.length} SKUs · ${locs.length} locations`);

  el('content').innerHTML = `
  <div class="pg-title">Dashboard</div>
  <div class="stat-grid">
    <div class="stat-card c-accent"><div class="stat-v">${items.length}</div><div class="stat-l">Total SKUs</div></div>
    <div class="stat-card c-accent"><div class="stat-v">${locs.length}</div><div class="stat-l">Locations</div></div>
    <div class="stat-card ${low>0?'c-warn':'c-ok'}"><div class="stat-v">${low}</div><div class="stat-l">Low Stock</div></div>
    <div class="stat-card ${over>0?'c-err':'c-ok'}"><div class="stat-v">${over}</div><div class="stat-l">Overstock</div></div>
  </div>

  ${low>0 ? `
  <div class="sc">
    <div class="sc-hdr">Low Stock Alerts <span style="color:var(--warn);font-size:.75rem">${low} item${low>1?'s':''} at or below minimum</span></div>
    <table>
      <thead><tr><th>SKU</th><th>Name</th><th>Supplier</th><th>On-Hand</th><th>Min Stock</th><th>Reorder Qty</th><th>Location</th></tr></thead>
      <tbody>${inv.filter(i=>i.status==='low').map(i=>`
        <tr>
          <td class="mono" style="font-weight:700">${esc(i.sku)}</td>
          <td>${esc(i.name)}</td>
          <td class="td-m">${esc(i.supplier)||'—'}</td>
          <td style="color:var(--warn);font-weight:700">${i.onhand}</td>
          <td class="td-m">${i.min_stock}</td>
          <td style="color:#818cf8">${i.reorder_qty}</td>
          <td class="mono td-m" style="font-size:.76rem">${esc(i.location)||'—'}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>` : ''}

  <div class="sc">
    <div class="sc-hdr">Inventory Summary</div>
    <div class="sc-body" style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px">
      <div><div style="font-size:1.5rem;font-weight:800;color:#fff">${total.toLocaleString()}</div><div style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-top:3px">Total On-Hand Units</div></div>
      <div><div style="font-size:1.5rem;font-weight:800;color:var(--success)">${inv.filter(i=>i.status==='ok').length}</div><div style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-top:3px">OK Status</div></div>
      <div><div style="font-size:1.5rem;font-weight:800;color:#60a5fa">${over}</div><div style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-top:3px">Overstock</div></div>
      <div><div style="font-size:1.5rem;font-weight:800;color:#a8a29e">${zero}</div><div style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-top:3px">Zero Stock</div></div>
    </div>
  </div>`;
}

// ── Locations ─────────────────────────────────────────────────────────────────
async function loadLocs(){
  const locs = await api.get('/api/locations');
  S.locations = locs;
  locs.forEach(l=>LREG[l.id]=l);
  const q = S.search.locs;
  const fl = q ? locs.filter(l=>(l.aisle+l.row+l.rack+l.bin).toLowerCase().includes(q.toLowerCase())) : locs;
  setHdrRight(`${locs.length} locations`);

  el('content').innerHTML = `
  <div class="pg-title">Warehouse Locations <span>Aisle / Row / Rack / Bin</span></div>
  <div class="toolbar">
    <input class="search" placeholder="Filter locations..." value="${esc(q)}" oninput="S.search.locs=this.value;loadLocs()">
    <button class="btn btn-p" onclick="openAddLoc()">+ Add Location</button>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Aisle</th><th>Row</th><th>Rack</th><th>Bin</th><th>Location Code</th><th>Actions</th></tr></thead>
    <tbody>${fl.length===0?`<tr><td colspan="6"><div class="empty"><span class="empty-ic">&#9698;</span>No locations yet. Click Add Location to get started.</div></td></tr>`:
    fl.map(l=>`<tr>
      <td class="mono" style="font-weight:600">${esc(l.aisle)}</td>
      <td class="mono">${esc(l.row)}</td>
      <td class="mono">${esc(l.rack)}</td>
      <td class="mono">${esc(l.bin)}</td>
      <td><span style="font-family:monospace;background:#1e1e28;border:1px solid #2a2a38;padding:2px 9px;border-radius:5px;font-size:.79rem;color:#a5b4fc">${esc(l.aisle)}-${esc(l.row)}-${esc(l.rack)}-${esc(l.bin)}</span></td>
      <td><div class="acts">
        <button class="btn btn-g btn-sm" onclick="openEditLoc('${l.id}')">Edit</button>
        <button class="btn btn-d btn-sm" onclick="delLoc('${l.id}')">Delete</button>
      </div></td>
    </tr>`).join('')}
    </tbody>
  </table></div>`;
}

function openAddLoc(){
  S.edit=null;
  ['lf-aisle','lf-row','lf-rack','lf-bin'].forEach(id=>sv(id,''));
  el('loc-m-title').textContent='Add Location';
  openM('loc-ov'); el('lf-aisle').focus();
}
function openEditLoc(id){
  const l=LREG[id]; if(!l) return;
  S.edit=l;
  sv('lf-aisle',l.aisle); sv('lf-row',l.row); sv('lf-rack',l.rack); sv('lf-bin',l.bin);
  el('loc-m-title').textContent='Edit Location';
  openM('loc-ov');
}
async function saveLoc(){
  const body={aisle:v('lf-aisle'),row:v('lf-row'),rack:v('lf-rack'),bin:v('lf-bin')};
  const [s,d] = S.edit ? await api.put(`/api/locations/${S.edit.id}`,body) : await api.post('/api/locations',body);
  if(s>=400){ toast(d.error||'Error','err'); return; }
  closeM('loc-ov'); toast(S.edit?'Location updated':'Location added'); loadLocs();
}
async function delLoc(id){
  if(!confirm('Delete this location?')) return;
  const [s,d] = await api.del(`/api/locations/${id}`);
  if(s>=400){ toast(d.error||'Cannot delete','err'); return; }
  toast('Deleted'); loadLocs();
}

// ── Items ─────────────────────────────────────────────────────────────────────
async function loadItems(){
  const [items, locs] = await Promise.all([
    api.get('/api/items'+(S.search.items?`?q=${encodeURIComponent(S.search.items)}`:'')),
    api.get('/api/locations')
  ]);
  S.items=items; S.locations=locs;
  S.locs_map={}; locs.forEach(l=>{ LREG[l.id]=l; S.locs_map[l.id]=`${l.aisle}-${l.row}-${l.rack}-${l.bin}`; });
  items.forEach(i=>IREG[i.sku]=i);
  const pg=S.page.items, tot=items.length;
  const paged=items.slice((pg-1)*PS, pg*PS);
  setHdrRight(`${tot}/1000 SKUs`);

  el('content').innerHTML = `
  <div class="pg-title">Item Master <span>${tot}/1000 SKUs defined</span></div>
  <div class="toolbar">
    <input class="search" placeholder="Search SKU, name, supplier..." value="${esc(S.search.items)}" oninput="S.search.items=this.value;S.page.items=1;loadItems()">
    <button class="btn btn-p" onclick="openAddItem()">+ Add SKU</button>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>SKU</th><th>Name</th><th>Supplier</th><th>Location</th><th>Min</th><th>Max</th><th>Reorder Qty</th><th>UOM</th><th>Added</th><th>Actions</th></tr></thead>
    <tbody>${paged.length===0?`<tr><td colspan="10"><div class="empty"><span class="empty-ic">&#9873;</span>No SKUs defined yet.</div></td></tr>`:
    paged.map(i=>`<tr>
      <td class="mono" style="font-weight:700;color:#a5b4fc">${esc(i.sku)}</td>
      <td style="max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(i.name)}</td>
      <td class="td-m">${esc(i.supplier)||'—'}</td>
      <td class="mono td-m" style="font-size:.76rem">${esc(S.locs_map[i.location_id]||'—')}</td>
      <td class="td-m">${i.min_stock}</td>
      <td class="td-m">${i.max_stock}</td>
      <td class="td-m">${i.reorder_qty}</td>
      <td class="td-m">${esc(i.uom||'EA')}</td>
      <td class="td-m" style="font-size:.74rem">${i.created||'—'}</td>
      <td><div class="acts">
        <button class="btn btn-g btn-sm" onclick="openEditItem('${esc(i.sku)}')">Edit</button>
        <button class="btn btn-d btn-sm" onclick="delItem('${esc(i.sku)}')">Delete</button>
      </div></td>
    </tr>`).join('')}
    </tbody>
  </table>
  ${tot>PS?`<div class="pag">
    <button class="pag-btn" onclick="S.page.items--;loadItems()" ${pg<=1?'disabled':''}>&#8592; Prev</button>
    <span class="pag-info">Page ${pg} of ${Math.ceil(tot/PS)} &middot; ${tot} items</span>
    <button class="pag-btn" onclick="S.page.items++;loadItems()" ${pg>=Math.ceil(tot/PS)?'disabled':''}>Next &#8594;</button>
  </div>`:''}
  </div>`;
}

function _locOpts(selId=''){
  return `<option value="">— No Location —</option>`+
    S.locations.map(l=>`<option value="${l.id}"${l.id===selId?' selected':''}>${l.aisle}-${l.row}-${l.rack}-${l.bin}</option>`).join('');
}
async function openAddItem(){
  const locs = await api.get('/api/locations'); S.locations=locs;
  S.edit=null;
  ['if-sku','if-name','if-sup','if-reorder','if-min','if-max'].forEach(id=>sv(id,''));
  sv('if-uom','EA');
  el('if-sku').disabled=false;
  el('if-loc').innerHTML=_locOpts();
  el('item-m-title').textContent='Add SKU';
  openM('item-ov'); el('if-sku').focus();
}
async function openEditItem(sku){
  const i=IREG[sku]; if(!i) return;
  const locs = await api.get('/api/locations'); S.locations=locs;
  S.edit=i;
  sv('if-sku',i.sku); sv('if-name',i.name); sv('if-sup',i.supplier);
  sv('if-reorder',i.reorder_qty); sv('if-min',i.min_stock); sv('if-max',i.max_stock);
  sv('if-uom',i.uom||'EA');
  el('if-sku').disabled=true;
  el('if-loc').innerHTML=_locOpts(i.location_id);
  el('item-m-title').textContent='Edit SKU';
  openM('item-ov');
}
async function saveItem(){
  const body={
    sku:v('if-sku').toUpperCase(), name:v('if-name'), supplier:v('if-sup'),
    reorder_qty:parseInt(v('if-reorder')||0), min_stock:parseInt(v('if-min')||0),
    max_stock:parseInt(v('if-max')||0), uom:v('if-uom')||'EA', location_id:v('if-loc')
  };
  const [s,d] = S.edit ? await api.put(`/api/items/${encodeURIComponent(S.edit.sku)}`,body)
                       : await api.post('/api/items',body);
  if(s>=400){ toast(d.error||'Error','err'); return; }
  closeM('item-ov'); toast(S.edit?'SKU updated':'SKU added'); loadItems();
}
async function delItem(sku){
  if(!confirm(`Delete SKU ${sku}? This will also remove its inventory record.`)) return;
  const [s,d]=await api.del(`/api/items/${encodeURIComponent(sku)}`);
  if(s>=400){ toast(d.error||'Error','err'); return; }
  toast('SKU deleted'); loadItems();
}

// ── Inventory ─────────────────────────────────────────────────────────────────
async function loadInv(){
  const inv = await api.get('/api/inventory');
  inv.forEach(i=>NREG[i.sku]=i);
  const fi=S.filter.inv, q=S.search.inv;
  let fl=inv;
  if(fi!=='all') fl=fl.filter(i=>i.status===fi);
  if(q){ const s=q.toLowerCase(); fl=fl.filter(i=>i.sku.toLowerCase().includes(s)||i.name.toLowerCase().includes(s)); }
  const pg=S.page.inv, tot=fl.length;
  const paged=fl.slice((pg-1)*PS,pg*PS);
  const cnt={all:inv.length, ok:inv.filter(i=>i.status==='ok').length,
             low:inv.filter(i=>i.status==='low').length, overstock:inv.filter(i=>i.status==='overstock').length};
  setHdrRight(`${inv.reduce((s,i)=>s+i.onhand,0).toLocaleString()} total units`);

  el('content').innerHTML = `
  <div class="pg-title">Inventory <span>On-Hand Quantities</span></div>
  <div class="toolbar">
    <input class="search" placeholder="Search SKU or name..." value="${esc(q)}" oninput="S.search.inv=this.value;S.page.inv=1;loadInv()">
    <div class="pills">
      <div class="pill ${fi==='all'?'on':''}" onclick="S.filter.inv='all';S.page.inv=1;loadInv()">All (${cnt.all})</div>
      <div class="pill ${fi==='ok'?'on':''}"  onclick="S.filter.inv='ok';S.page.inv=1;loadInv()">OK (${cnt.ok})</div>
      <div class="pill ${fi==='low'?'on':''}" onclick="S.filter.inv='low';S.page.inv=1;loadInv()">Low (${cnt.low})</div>
      <div class="pill ${fi==='overstock'?'on':''}" onclick="S.filter.inv='overstock';S.page.inv=1;loadInv()">Overstock (${cnt.overstock})</div>
    </div>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>SKU</th><th>Name</th><th>Supplier</th><th>On-Hand</th><th>Min</th><th>Max</th><th>Reorder Qty</th><th>Status</th><th>Location</th><th>Last Counted</th><th>Last Adjusted</th><th>Actions</th></tr></thead>
    <tbody>${paged.length===0?`<tr><td colspan="12"><div class="empty"><span class="empty-ic">&#9726;</span>No inventory records match.</div></td></tr>`:
    paged.map(i=>{
      const bdg = i.status==='ok'?'bdg-ok':i.status==='low'?'bdg-low':i.onhand===0?'bdg-zero':'bdg-over';
      const lbl = i.status==='overstock'?'Overstock':i.status==='low'?'Low Stock':i.onhand===0?'Zero':'OK';
      return `<tr>
        <td class="mono" style="font-weight:700;color:#a5b4fc">${esc(i.sku)}</td>
        <td>${esc(i.name)}</td>
        <td class="td-m">${esc(i.supplier)||'—'}</td>
        <td style="font-weight:700;font-size:1rem">${i.onhand.toLocaleString()}</td>
        <td class="td-m">${i.min_stock}</td>
        <td class="td-m">${i.max_stock}</td>
        <td style="color:#818cf8">${i.reorder_qty}</td>
        <td><span class="bdg ${bdg}">${lbl}</span></td>
        <td class="mono td-m" style="font-size:.74rem">${esc(i.location)||'—'}</td>
        <td class="td-m" style="font-size:.74rem">${i.last_counted||'—'}</td>
        <td class="td-m" style="font-size:.74rem">${i.last_adjusted||'—'}</td>
        <td><button class="btn btn-g btn-sm" onclick="openAdj('${esc(i.sku)}')">Adjust</button></td>
      </tr>`;}).join('')}
    </tbody>
  </table>
  ${tot>PS?`<div class="pag">
    <button class="pag-btn" onclick="S.page.inv--;loadInv()" ${pg<=1?'disabled':''}>&#8592; Prev</button>
    <span class="pag-info">Page ${pg} of ${Math.ceil(tot/PS)} &middot; ${tot} items</span>
    <button class="pag-btn" onclick="S.page.inv++;loadInv()" ${pg>=Math.ceil(tot/PS)?'disabled':''}>Next &#8594;</button>
  </div>`:''}
  </div>`;
}

function openAdj(sku){
  const i=NREG[sku]; if(!i) return;
  S.edit=i;
  el('adj-sku-lbl').textContent=`${i.sku} — ${i.name}`;
  el('adj-oh-lbl').textContent=`Current on-hand: ${i.onhand.toLocaleString()} ${i.uom||''}`;
  sv('adj-type','adjust'); sv('adj-qty',''); sv('adj-reason','');
  el('adj-hint').textContent='Use negative to remove stock';
  openM('adj-ov'); el('adj-qty').focus();
}
el('adj-type').addEventListener('change', function(){
  el('adj-hint').textContent = this.value==='set'
    ? 'Enter the exact on-hand quantity to set'
    : 'Use negative to remove stock, positive to add';
});
async function saveAdj(){
  const body={type:v('adj-type'), qty:parseInt(v('adj-qty')||0), reason:v('adj-reason')||'Manual adjustment'};
  const [s,d]=await api.post(`/api/inventory/${encodeURIComponent(S.edit.sku)}/adjust`,body);
  if(s>=400){ toast(d.error||'Error','err'); return; }
  closeM('adj-ov'); toast(`Updated: ${d.old} \u2192 ${d.onhand} units`); loadInv();
}

// ── Cycle Count ───────────────────────────────────────────────────────────────
async function loadCC(){
  const [cc, hist] = await Promise.all([
    api.get('/api/cyclecount/today'),
    api.get('/api/cyclecount/history')
  ]);
  S.ccItems = cc.items;
  setHdrRight(cc.date);

  const dateStr = new Date(cc.date+'T12:00:00').toLocaleDateString('en-US',{weekday:'long',year:'numeric',month:'long',day:'numeric'});

  el('content').innerHTML = `
  <div class="pg-title">Cycle Count <span>${dateStr}</span></div>

  ${cc.completed ? `<div class="banner-ok">&#10003; Today's cycle count has been submitted. You can recount if needed.</div>` : `<div class="banner-warn">&#9888; Today's count has not been submitted yet.</div>`}

  <div class="sc" style="margin-bottom:20px">
    <div class="sc-hdr">
      Top 10 Items by On-Hand Quantity
      <span style="font-size:.72rem;color:var(--muted)">Enter physical count &rarr; Submit</span>
    </div>
    ${cc.items.length===0?`<div class="empty"><span class="empty-ic">&#10003;</span>No items in inventory to count. Add SKUs first.</div>` : `
    <table>
      <thead><tr>
        <th>#</th><th>SKU</th><th>Name</th><th>Location</th>
        <th style="text-align:right">Expected</th>
        <th style="text-align:right">Counted</th>
        <th style="text-align:right">Variance</th>
      </tr></thead>
      <tbody>
      ${cc.items.map((item,i)=>`<tr>
        <td class="td-m">${i+1}</td>
        <td class="mono" style="font-weight:700;color:#a5b4fc">${esc(item.sku)}</td>
        <td>${esc(item.name)}</td>
        <td class="mono td-m" style="font-size:.76rem">${esc(item.location||'—')}</td>
        <td style="text-align:right;font-weight:600">${item.onhand.toLocaleString()}</td>
        <td style="text-align:right">
          <input class="cc-count-inp" type="number" min="0" id="cc-${esc(item.sku)}"
                 placeholder="${item.onhand}"
                 oninput="updVar('${esc(item.sku)}',${item.onhand},this.value)">
        </td>
        <td style="text-align:right" id="var-${esc(item.sku)}">
          <span class="v-zero">—</span>
        </td>
      </tr>`).join('')}
      </tbody>
    </table>
    <div style="padding:12px 16px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:.75rem;color:var(--muted)">Submitting will update on-hand quantities to your counted values.</span>
      <div style="display:flex;gap:10px">
        <button class="btn btn-g" onclick="clearCC()">Clear</button>
        <button class="btn btn-ok" onclick="submitCC()">Submit Count</button>
      </div>
    </div>`}
  </div>

  ${hist.length>0?`
  <div class="sc">
    <div class="sc-hdr">Recent Count History <span style="font-size:.72rem;color:var(--muted)">Last ${hist.length} count sessions</span></div>
    ${hist.map(h=>`
    <div style="padding:12px 18px;border-bottom:1px solid var(--border)">
      <div style="font-size:.78rem;font-weight:600;color:#aaa;margin-bottom:7px">${h.date} &nbsp;&#183;&nbsp; ${h.timestamp||''} &nbsp;&#183;&nbsp; ${h.items.length} items counted</div>
      <div style="display:flex;flex-wrap:wrap;gap:7px">
        ${h.items.map(it=>{
          const cls = it.variance>0?'v-pos':it.variance<0?'v-neg':'v-zero';
          const sign = it.variance>=0?'+':'';
          return `<span style="font-size:.73rem;background:var(--surface2);border:1px solid var(--border);border-radius:5px;padding:3px 10px">
            <span class="mono">${esc(it.sku)}</span>&nbsp;
            <span style="color:var(--muted)">${it.expected}&rarr;${it.counted}</span>&nbsp;
            <span class="${cls}">${sign}${it.variance}</span>
          </span>`;}).join('')}
      </div>
    </div>`).join('')}
  </div>` : ''}`;
}

function updVar(sku, expected, val){
  const e=el(`var-${sku}`); if(!e) return;
  const n=parseInt(val);
  if(isNaN(n)||val===''){e.innerHTML='<span class="v-zero">—</span>';return;}
  const v2=n-expected, cls=v2>0?'v-pos':v2<0?'v-neg':'v-zero', sign=v2>=0?'+':'';
  e.innerHTML=`<span class="${cls}">${sign}${v2}</span>`;
}
function clearCC(){
  S.ccItems.forEach(item=>{
    const inp=el(`cc-${item.sku}`); if(inp) inp.value='';
    const ve=el(`var-${item.sku}`); if(ve) ve.innerHTML='<span class="v-zero">—</span>';
  });
}
async function submitCC(){
  const counts=[];
  for(const item of S.ccItems){
    const inp=el(`cc-${item.sku}`);
    if(!inp||inp.value==='') continue;
    counts.push({sku:item.sku, counted:parseInt(inp.value)});
  }
  if(!counts.length){toast('Enter at least one count value','err');return;}
  const [s,d]=await api.post('/api/cyclecount',{counts});
  if(s>=400){toast(d.error||'Error','err');return;}
  toast(`Count submitted — ${d.items.length} item${d.items.length>1?'s':''} updated`);
  loadCC();
}

// ── Layout ────────────────────────────────────────────────────────────────────
const BREG = {};  // loc_id -> bin obj from layout response
let _layData    = null;
let _activeAisle = null;

async function loadLayout(){
  const [data, items] = await Promise.all([
    api.get('/api/layout'),
    api.get('/api/items'),
  ]);
  _layData = data;
  S.items  = items;
  items.forEach(i => { IREG[i.sku] = i; });

  // Build locs_map in case user lands here without visiting Items first
  const locs = await api.get('/api/locations');
  S.locations = locs;
  S.locs_map  = {};
  locs.forEach(l => { LREG[l.id] = l; S.locs_map[l.id] = `${l.aisle}-${l.row}-${l.rack}-${l.bin}`; });

  // Register bins
  Object.values(data.tree).forEach(rows =>
    Object.values(rows).forEach(racks =>
      Object.values(racks).forEach(bins =>
        bins.forEach(b => { BREG[b.id] = b; }))));

  const aisles = data.aisles;
  if(!_activeAisle || !aisles.includes(_activeAisle)) _activeAisle = aisles[0] || null;
  setHdrRight(`${aisles.length} aisle${aisles.length !== 1 ? 's' : ''} \u00b7 ${locs.length} locations`);

  el('content').innerHTML = `
  <div class="pg-title">Warehouse Layout <span>Click any bin to name it or assign a SKU</span></div>
  ${aisles.length === 0
    ? `<div class="empty"><span class="empty-ic">&#9698;</span>No locations defined yet. Add locations in Warehouse master data first.</div>`
    : `<div class="lay-tabs" id="lay-tabs">
        ${aisles.map(a => `<div class="lay-tab${a===_activeAisle?' active':''}" onclick="selectAisle('${esc(a)}')">${esc(a)}</div>`).join('')}
       </div>
       <div id="lay-aisle-body"></div>`}`;

  if(_activeAisle) renderAisle(_activeAisle);
}

function selectAisle(a){
  _activeAisle = a;
  document.querySelectorAll('.lay-tab').forEach(t => t.classList.toggle('active', t.textContent.trim() === a));
  renderAisle(a);
}

function renderAisle(aisle){
  const rows = _layData && _layData.tree[aisle] ? _layData.tree[aisle] : {};
  const body = el('lay-aisle-body');
  if(!body) return;
  const rowKeys = Object.keys(rows).sort();
  if(rowKeys.length === 0){
    body.innerHTML = `<div class="empty"><span class="empty-ic">&#9698;</span>No bins in aisle ${esc(aisle)} yet.</div>`;
    return;
  }
  body.innerHTML = rowKeys.map(row => {
    const racks   = rows[row];
    const rackKeys = Object.keys(racks).sort((a, b) => a.localeCompare(b, undefined, {numeric: true}));
    return `<div class="lay-row-card">
      <div class="lay-row-hdr">Row ${esc(row)}</div>
      <div class="lay-row-body">
        ${rackKeys.map(rack => {
          const bins = racks[rack];
          bins.forEach(b => { BREG[b.id] = b; });
          return `<div class="lay-rack-col">
            <div class="lay-rack-lbl">Rack ${esc(rack)}</div>
            ${bins.map(b => renderBinCell(b)).join('')}
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
}

function renderBinCell(b){
  const cls = b.status==='ok' ? 'st-ok'
            : b.status==='low' ? 'st-low'
            : b.status==='overstock' ? 'st-over'
            : 'st-empty';
  const qty  = b.sku ? `<div class="bc-qty">${b.onhand.toLocaleString()}</div><div class="bc-sku">${esc(b.sku)}</div>` : '';
  const lbl  = b.label ? `<div class="bc-label">${esc(b.label)}</div>` : '';
  return `<div class="bin-cell ${cls}" onclick="openBinModal('${b.id}')">
    <div class="bc-name">${esc(b.bin)}</div>
    ${lbl}${qty}
  </div>`;
}

async function openBinModal(locId){
  const b = BREG[locId]; if(!b) return;
  S.edit = b;
  el('bin-m-title').textContent = `${b.aisle}-${b.row}-${b.rack}-${b.bin}`;
  el('bin-code-lbl').textContent = `${b.aisle}-${b.row}-${b.rack}-${b.bin}`;
  sv('bf-label', b.label || '');
  sv('bf-notes', b.notes || '');

  // Build SKU dropdown; items already in S.items
  const currentSku = b.sku || '';
  el('bf-sku').innerHTML = '<option value="">&#8212; Unassigned &#8212;</option>' +
    S.items.map(i => {
      const other = i.location_id && i.location_id !== locId;
      const tag   = other ? ` (${S.locs_map[i.location_id] || i.location_id})` : '';
      const style = other ? ' style="color:#777"' : '';
      return `<option value="${esc(i.sku)}"${i.location_id===locId?' selected':''}${style}>${esc(i.sku)} \u2014 ${esc(i.name)}${esc(tag)}</option>`;
    }).join('');

  const hasSkuNow = !!currentSku;
  el('bf-qty-wrap').style.display = hasSkuNow ? '' : 'none';
  if(hasSkuNow && NREG[currentSku] != null) sv('bf-qty', NREG[currentSku].onhand);
  else sv('bf-qty', b.onhand || '');

  openM('bin-ov');
}

function onBinSkuChange(){
  const sku = v('bf-sku');
  el('bf-qty-wrap').style.display = sku ? '' : 'none';
  if(sku && NREG[sku] != null) sv('bf-qty', NREG[sku].onhand);
  else sv('bf-qty', '');
}

async function saveBin(){
  const b = S.edit; if(!b) return;
  const label = v('bf-label');
  const notes = v('bf-notes');
  const newSku = v('bf-sku');
  const qty    = v('bf-qty');
  const oldSku = b.sku || '';

  // 1. Update location label/notes
  const [s1, d1] = await api.put(`/api/locations/${b.id}`, {
    aisle: b.aisle, row: b.row, rack: b.rack, bin: b.bin,
    label, notes,
  });
  if(s1 >= 400){ toast(d1.error || 'Error saving bin', 'err'); return; }

  // 2. Unassign previous SKU from this location (if changed)
  if(oldSku && oldSku !== newSku && IREG[oldSku]){
    await api.put(`/api/items/${encodeURIComponent(oldSku)}`, {
      ...IREG[oldSku], location_id: '',
    });
  }
  // 3. Assign new SKU to this location (if changed)
  if(newSku && newSku !== oldSku && IREG[newSku]){
    await api.put(`/api/items/${encodeURIComponent(newSku)}`, {
      ...IREG[newSku], location_id: b.id,
    });
  }
  // 4. Set inventory if a SKU is selected and qty given
  if(newSku && qty !== ''){
    await api.post(`/api/inventory/${encodeURIComponent(newSku)}/adjust`, {
      type: 'set', qty: parseInt(qty) || 0, reason: 'Layout bin edit',
    });
  }

  closeM('bin-ov');
  toast('Bin saved');
  await loadLayout();
}

// ── Init ──────────────────────────────────────────────────────────────────────
nav('dashboard');
</script>
</body>
</html>
"""


# ── HTTP server ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                pass
        return {}

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        parsed = urlparse(self.path)
        parts  = [p for p in parsed.path.strip("/").split("/") if p]
        qs     = parse_qs(parsed.query)
        return parts, qs

    def do_GET(self):
        parts, qs = self._route()
        if not parts:
            self._html(); return
        if parts[0] != "api":
            self._json(404, {"error": "not found"}); return
        if len(parts) < 2:
            self._json(404, {"error": "not found"}); return

        r = parts[1]
        if r == "layout":
            self._json(200, get_layout())
        elif r == "locations":
            self._json(200, get_locations())
        elif r == "items":
            q = qs.get("q", [""])[0]
            self._json(200, get_items(q or None))
        elif r == "inventory":
            self._json(200, get_inventory())
        elif r == "cyclecount":
            sub = parts[2] if len(parts) > 2 else ""
            if sub == "today":
                self._json(200, get_cycle_count_today())
            elif sub == "history":
                self._json(200, get_cycle_count_history())
            else:
                self._json(404, {"error": "not found"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parts, _ = self._route()
        body     = self._body()
        if not parts or parts[0] != "api":
            self._json(404, {"error": "not found"}); return
        r = parts[1] if len(parts) > 1 else ""
        if r == "locations" and len(parts) == 2:
            res, code = create_location(body); self._json(code, res)
        elif r == "items" and len(parts) == 2:
            res, code = create_item(body); self._json(code, res)
        elif r == "inventory" and len(parts) == 4 and parts[3] == "adjust":
            sku = unquote(parts[2])
            res, code = adjust_inventory(sku, body); self._json(code, res)
        elif r == "cyclecount" and len(parts) == 2:
            res, code = submit_cycle_count(body); self._json(code, res)
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        parts, _ = self._route()
        body     = self._body()
        if not parts or parts[0] != "api":
            self._json(404, {"error": "not found"}); return
        r = parts[1] if len(parts) > 1 else ""
        if r == "locations" and len(parts) == 3:
            res, code = update_location(parts[2], body); self._json(code, res)
        elif r == "items" and len(parts) == 3:
            res, code = update_item(unquote(parts[2]), body); self._json(code, res)
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        parts, _ = self._route()
        if not parts or parts[0] != "api":
            self._json(404, {"error": "not found"}); return
        r = parts[1] if len(parts) > 1 else ""
        if r == "locations" and len(parts) == 3:
            res, code = delete_location(parts[2]); self._json(code, res)
        elif r == "items" and len(parts) == 3:
            res, code = delete_item(unquote(parts[2])); self._json(code, res)
        else:
            self._json(404, {"error": "not found"})


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"WMS Inventory -> http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
