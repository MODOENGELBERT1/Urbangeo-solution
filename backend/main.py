"""
UrbanSanity v12.5 — World Bank Pitch-Ready Waste Planning Tool
Backend API (FastAPI)
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import httpx
import json
import math
import time
import io
import asyncio
from datetime import datetime

# ReportLab
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

app = FastAPI(title="UrbanSanity API", version="13.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VERSION = "13.0"

# ── Cache OSM ──────────────────────────────────────────────────────────────
_osm_cache: Dict[str, Any] = {}
CACHE_TTL = 600  # 10 min

# ── Models ─────────────────────────────────────────────────────────────────
class BBox(BaseModel):
    south: float
    west:  float
    north: float
    east:  float

class FetchOSMRequest(BaseModel):
    bbox: BBox
    mode: str = "quick"  # "quick" | "accurate"
    aoi: Optional[Dict[str, Any]] = None

class AnalyzeRequest(BaseModel):
    osm_data: Dict[str, Any]
    bbox: BBox
    params: Optional[Dict[str, Any]] = None
    aoi: Optional[Dict[str, Any]] = None

class ReportRequest(BaseModel):
    analysis: Dict[str, Any]
    bbox: Optional[BBox] = None          # optional — not used in PDF generation
    city_name: Optional[str] = "Zone analysée"
    report_lang: Optional[str] = "fr"   # "fr" | "en"
    params: Optional[Dict[str, Any]] = None
    aoi: Optional[Dict[str, Any]] = None
    manual_check_result: Optional[Dict[str, Any]] = None

# ── OSM Fetch ──────────────────────────────────────────────────────────────
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

async def _overpass_query(query: str) -> dict:
    last_err = None
    for ep in OVERPASS_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.post(ep, data={"data": query})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_err = e
            continue
    raise HTTPException(status_code=503, detail=f"Overpass unavailable: {last_err}")

def _safe_fc(elements, geom_types=("way", "relation", "node")):
    """Convert OSM elements to GeoJSON FeatureCollection safely."""
    features = []
    for el in elements:
        if el.get("type") not in geom_types:
            continue
        tags = el.get("tags", {})
        # Node → Point
        if el["type"] == "node" and "lat" in el:
            feat = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                "properties": tags
            }
            features.append(feat)
        # Way → LineString or Polygon
        elif el["type"] == "way" and "geometry" in el:
            coords = [[g["lon"], g["lat"]] for g in el["geometry"]]
            if len(coords) >= 3 and coords[0] == coords[-1]:
                geom = {"type": "Polygon", "coordinates": [coords]}
            else:
                geom = {"type": "LineString", "coordinates": coords}
            feat = {"type": "Feature", "geometry": geom, "properties": tags}
            features.append(feat)
    return {"type": "FeatureCollection", "features": features}

def _quick_osm_url(bbox: BBox) -> str:
    return (
        f"https://api.openstreetmap.org/api/0.6/map"
        f"?bbox={bbox.west},{bbox.south},{bbox.east},{bbox.north}"
    )

def _parse_map_api(data: dict):
    """Parse OSM Map API response into layer dict."""
    elements = data.get("elements", [])
    buildings, roads, schools, hospitals, hydro, waste_bins = [], [], [], [], [], []

    for el in elements:
        tags = el.get("tags", {})
        t = el.get("type")
        if t == "node":
            geom = {"type": "Point", "coordinates": [el.get("lon", 0), el.get("lat", 0)]}
        elif t == "way":
            nd_refs = el.get("nodes", [])
            # We don't have node coords in map API response easily; skip geometry
            geom = None
        else:
            geom = None

        if not geom:
            continue

        feat = {"type": "Feature", "geometry": geom, "properties": tags}

        if tags.get("building"):
            buildings.append(feat)
        elif tags.get("highway"):
            roads.append(feat)
        elif tags.get("amenity") in ("school", "college", "university"):
            schools.append(feat)
        elif tags.get("amenity") in ("hospital", "clinic", "doctors"):
            hospitals.append(feat)
        elif tags.get("waterway") or tags.get("natural") in ("water", "wetland"):
            hydro.append(feat)
        elif tags.get("amenity") == "waste_basket" or tags.get("amenity") == "recycling":
            waste_bins.append(feat)

    return {
        "buildings": {"type": "FeatureCollection", "features": buildings},
        "roads":     {"type": "FeatureCollection", "features": roads},
        "schools":   {"type": "FeatureCollection", "features": schools},
        "hospitals": {"type": "FeatureCollection", "features": hospitals},
        "hydro":     {"type": "FeatureCollection", "features": hydro},
        "waste_bins":{"type": "FeatureCollection", "features": waste_bins},
    }

@app.post("/api/fetch_osm")
async def fetch_osm(req: FetchOSMRequest):
    bbox = req.bbox
    aoi_ring = _extract_outer_ring(req.aoi)
    aoi_sig = '' if not aoi_ring else ':' + str(round(sum(pt[0] + pt[1] for pt in aoi_ring), 6)) + ':' + str(len(aoi_ring))
    cache_key = f"{bbox.south:.4f},{bbox.west:.4f},{bbox.north:.4f},{bbox.east:.4f}:{req.mode}{aoi_sig}"
    now = time.time()
    if cache_key in _osm_cache:
        cached = _osm_cache[cache_key]
        if now - cached["ts"] < CACHE_TTL:
            result = dict(cached["data"])
            result["cached"] = True
            return result

    if req.mode == "quick":
        # Overpass quick query with geometry
        query = f"""
[out:json][timeout:30];
(
  way["building"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["highway"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  node["amenity"~"school|college|university|hospital|clinic|doctors|waste_basket|recycling|waste_disposal"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["waterway"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["natural"~"water|wetland"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
);
out geom;
"""
        raw = await _overpass_query(query)
        elements = raw.get("elements", [])

        buildings, roads, schools, hospitals, hydro, waste_bins = [], [], [], [], [], []
        for el in elements:
            tags = el.get("tags", {})
            if el["type"] == "node" and "lat" in el:
                pt = {"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                      "properties": tags}
                amenity = tags.get("amenity", "")
                if amenity in ("school", "college", "university"):
                    schools.append(pt)
                elif amenity in ("hospital", "clinic", "doctors"):
                    hospitals.append(pt)
                elif amenity in ("waste_basket", "recycling", "waste_disposal"):
                    waste_bins.append(pt)
            elif el["type"] == "way" and "geometry" in el:
                coords = [[g["lon"], g["lat"]] for g in el["geometry"]]
                if not coords:
                    continue
                if tags.get("building"):
                    if len(coords) >= 3 and coords[0] == coords[-1]:
                        geom = {"type": "Polygon", "coordinates": [coords]}
                    else:
                        geom = {"type": "LineString", "coordinates": coords}
                    buildings.append({"type": "Feature", "geometry": geom, "properties": tags})
                elif tags.get("highway"):
                    roads.append({"type": "Feature",
                                  "geometry": {"type": "LineString", "coordinates": coords},
                                  "properties": tags})
                elif tags.get("waterway") or tags.get("natural") in ("water", "wetland"):
                    geom_type = "Polygon" if (len(coords) >= 3 and coords[0] == coords[-1]) else "LineString"
                    c = [coords] if geom_type == "Polygon" else coords
                    hydro.append({"type": "Feature",
                                  "geometry": {"type": geom_type, "coordinates": c},
                                  "properties": tags})

        layers = {
            "buildings": {"type": "FeatureCollection", "features": buildings},
            "roads":     {"type": "FeatureCollection", "features": roads},
            "schools":   {"type": "FeatureCollection", "features": schools},
            "hospitals": {"type": "FeatureCollection", "features": hospitals},
            "hydro":     {"type": "FeatureCollection", "features": hydro},
            "waste_bins":{"type": "FeatureCollection", "features": waste_bins},
            "source": "Overpass API (Quick)",
            "cached": False,
        }
    else:
        # Accurate mode — detailed overpass
        query = f"""
[out:json][timeout:60];
(
  way["building"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  relation["building"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["highway"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  node["amenity"~"school|college|university"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  node["amenity"~"hospital|clinic|doctors"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  node["amenity"~"waste_basket|recycling|waste_disposal"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["waterway"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["natural"~"water|wetland"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
  way["amenity"~"school|hospital"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});
);
out geom;
"""
        raw = await _overpass_query(query)
        elements = raw.get("elements", [])
        # same parsing as quick
        buildings, roads, schools, hospitals, hydro, waste_bins = [], [], [], [], [], []
        for el in elements:
            tags = el.get("tags", {})
            if el["type"] == "node" and "lat" in el:
                pt = {"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                      "properties": tags}
                amenity = tags.get("amenity", "")
                if amenity in ("school", "college", "university"):
                    schools.append(pt)
                elif amenity in ("hospital", "clinic", "doctors"):
                    hospitals.append(pt)
                elif amenity in ("waste_basket", "recycling", "waste_disposal"):
                    waste_bins.append(pt)
            elif el["type"] == "way" and "geometry" in el:
                coords = [[g["lon"], g["lat"]] for g in el["geometry"]]
                if not coords:
                    continue
                if tags.get("building"):
                    geom_t = "Polygon" if (len(coords) >= 3 and coords[0] == coords[-1]) else "LineString"
                    g_coords = [coords] if geom_t == "Polygon" else coords
                    buildings.append({"type": "Feature",
                                      "geometry": {"type": geom_t, "coordinates": g_coords},
                                      "properties": tags})
                elif tags.get("highway"):
                    roads.append({"type": "Feature",
                                  "geometry": {"type": "LineString", "coordinates": coords},
                                  "properties": tags})
                elif tags.get("waterway") or tags.get("natural") in ("water", "wetland"):
                    geom_t = "Polygon" if (len(coords) >= 3 and coords[0] == coords[-1]) else "LineString"
                    g_coords = [coords] if geom_t == "Polygon" else coords
                    hydro.append({"type": "Feature",
                                  "geometry": {"type": geom_t, "coordinates": g_coords},
                                  "properties": tags})
                elif tags.get("amenity") in ("school",):
                    schools.append({"type": "Feature",
                                    "geometry": {"type": "LineString", "coordinates": coords},
                                    "properties": tags})
                elif tags.get("amenity") in ("hospital",):
                    hospitals.append({"type": "Feature",
                                      "geometry": {"type": "LineString", "coordinates": coords},
                                      "properties": tags})

        layers = {
            "buildings": {"type": "FeatureCollection", "features": buildings},
            "roads":     {"type": "FeatureCollection", "features": roads},
            "schools":   {"type": "FeatureCollection", "features": schools},
            "hospitals": {"type": "FeatureCollection", "features": hospitals},
            "hydro":     {"type": "FeatureCollection", "features": hydro},
            "waste_bins":{"type": "FeatureCollection", "features": waste_bins},
            "source": "Overpass API (Accurate)",
            "cached": False,
        }

    if aoi_ring:
        for k in ("buildings", "roads", "schools", "hospitals", "hydro", "waste_bins"):
            layers[k] = _filter_fc_to_aoi(_normalize_fc(layers.get(k)), aoi_ring)
    layers["message"] = (
        "No existing waste bins found in the selected area. Analysis will continue using accessibility, safety and waste-demand criteria."
        if len((layers.get("waste_bins") or {}).get("features", [])) == 0
        else None
    )
    _osm_cache[cache_key] = {"ts": now, "data": layers}
    return layers

# ── Analysis ────────────────────────────────────────────────────────────────
def _haversine(lon1, lat1, lon2, lat2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def _bbox_area_km2(bbox: BBox):
    w = _haversine(bbox.west, bbox.south, bbox.east, bbox.south) / 1000
    h = _haversine(bbox.west, bbox.south, bbox.west, bbox.north) / 1000
    return w * h

def _coverage_pop(cells, points, radius_m):
    if not points:
        return 0.0
    total = 0.0
    for cell in cells:
        if any(_haversine(cell["lon"], cell["lat"], pt["geometry"]["coordinates"][0], pt["geometry"]["coordinates"][1]) <= radius_m for pt in points if pt.get("geometry",{}).get("type")=="Point"):
            total += float(cell.get("population", 0) or 0)
    return total

def _coverage_pop_proposed(cells, bins, radius_m):
    if not bins:
        return 0.0
    total = 0.0
    for cell in cells:
        if any(_haversine(cell["lon"], cell["lat"], b["lon"], b["lat"]) <= radius_m for b in bins):
            total += float(cell.get("population", 0) or 0)
    return total


def _extract_outer_ring(geojson):
    if not geojson:
        return None
    obj = geojson
    if obj.get("type") == "FeatureCollection":
        for feat in obj.get("features", []):
            ring = _extract_outer_ring(feat)
            if ring:
                return ring
        return None
    if obj.get("type") == "Feature":
        obj = obj.get("geometry") or {}
    gtype = obj.get("type")
    coords = obj.get("coordinates") or []
    if gtype == "Polygon" and coords:
        return coords[0]
    if gtype == "MultiPolygon" and coords and coords[0]:
        return coords[0][0]
    return None

def _segment_intersects(a1, a2, b1, b2):
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    def on_seg(p, q, r):
        return min(p[0], r[0]) - 1e-12 <= q[0] <= max(p[0], r[0]) + 1e-12 and min(p[1], r[1]) - 1e-12 <= q[1] <= max(p[1], r[1]) + 1e-12
    o1 = orient(a1, a2, b1)
    o2 = orient(a1, a2, b2)
    o3 = orient(b1, b2, a1)
    o4 = orient(b1, b2, a2)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
        return True
    if abs(o1) < 1e-12 and on_seg(a1, b1, a2): return True
    if abs(o2) < 1e-12 and on_seg(a1, b2, a2): return True
    if abs(o3) < 1e-12 and on_seg(b1, a1, b2): return True
    if abs(o4) < 1e-12 and on_seg(b1, a2, b2): return True
    return False

def _feature_intersects_ring(feature, ring):
    if not ring:
        return True
    geom = feature.get("geometry", {})
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Point" and coords:
        return _point_in_ring(coords[0], coords[1], ring)
    if gtype == "LineString" and coords:
        if any(_point_in_ring(pt[0], pt[1], ring) for pt in coords):
            return True
        ring_edges = list(zip(ring[:-1], ring[1:]))
        for i in range(len(coords)-1):
            a1, a2 = coords[i], coords[i+1]
            for b1, b2 in ring_edges:
                if _segment_intersects(a1, a2, b1, b2):
                    return True
        return False
    if gtype == "Polygon" and coords:
        poly = coords[0]
        if any(_point_in_ring(pt[0], pt[1], ring) for pt in poly):
            return True
        if any(_point_in_ring(pt[0], pt[1], poly) for pt in ring):
            return True
        ring_edges = list(zip(ring[:-1], ring[1:]))
        poly_edges = list(zip(poly[:-1], poly[1:]))
        for a1, a2 in poly_edges:
            for b1, b2 in ring_edges:
                if _segment_intersects(a1, a2, b1, b2):
                    return True
        return False
    return False

def _filter_fc_to_aoi(fc, ring):
    if not ring or not fc:
        return fc or {"type": "FeatureCollection", "features": []}
    return {"type": "FeatureCollection", "features": [f for f in (fc.get("features") or []) if _feature_intersects_ring(f, ring)]}

def _nearest_feature_distance_m(lon, lat, features):
    best = float("inf")
    for feat in features:
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        if gtype == "Point" and coords:
            best = min(best, _haversine(lon, lat, coords[0], coords[1]))
        elif gtype == "LineString" and coords:
            for i in range(len(coords)-1):
                best = min(best, _distance_point_to_segment_m(lon, lat, coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1]))
        elif gtype == "Polygon" and coords and coords[0]:
            ring = coords[0]
            if _point_in_ring(lon, lat, ring):
                return 0.0
            for i in range(len(ring)-1):
                best = min(best, _distance_point_to_segment_m(lon, lat, ring[i][0], ring[i][1], ring[i+1][0], ring[i+1][1]))
    return best

def _normalize_fc(obj):
    if not obj or not isinstance(obj, dict):
        return {"type": "FeatureCollection", "features": []}
    if obj.get("type") == "FeatureCollection":
        obj["features"] = obj.get("features") or []
        return obj
    return {"type": "FeatureCollection", "features": []}

def _centroid(feature):
    geom = feature.get("geometry", {})
    t = geom.get("type")
    coords = geom.get("coordinates", [])
    if t == "Point":
        return coords[0], coords[1]
    elif t == "LineString" and coords:
        mid = len(coords) // 2
        return coords[mid][0], coords[mid][1]
    elif t == "Polygon" and coords:
        ring = coords[0]
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        return cx, cy
    return None, None

def _nearest_road_dist(lon, lat, roads):
    min_d = float("inf")
    for feat in roads:
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if not coords:
            continue
        for pt in coords:
            d = _haversine(lon, lat, pt[0], pt[1])
            if d < min_d:
                min_d = d
    return min_d


def _feature_bbox(feature):
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [])
    points = []
    if geom.get("type") == "Point" and coords:
        points = [coords]
    elif geom.get("type") == "LineString":
        points = coords
    elif geom.get("type") == "Polygon" and coords:
        points = coords[0]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))

def _point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        intersects = ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi)
        if intersects:
            inside = not inside
        j = i
    return inside

def _distance_point_to_segment_m(lon, lat, ax, ay, bx, by):
    mean_lat = math.radians((lat + ay + by) / 3)
    kx = 111320 * math.cos(mean_lat)
    ky = 111320
    px, py = lon * kx, lat * ky
    ax2, ay2 = ax * kx, ay * ky
    bx2, by2 = bx * kx, by * ky
    dx, dy = bx2 - ax2, by2 - ay2
    if dx == 0 and dy == 0:
        return math.hypot(px - ax2, py - ay2)
    t = ((px - ax2) * dx + (py - ay2) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    qx, qy = ax2 + t * dx, ay2 + t * dy
    return math.hypot(px - qx, py - qy)

def _candidate_on_building(lon, lat, building_features, clearance_m=6.0):
    for feat in building_features:
        geom = feat.get("geometry", {})
        if geom.get("type") != "Polygon":
            continue
        bbox = feat.get("_bbox")
        if bbox:
            minx, miny, maxx, maxy = bbox
            pad_lon = clearance_m / (111320 * max(math.cos(math.radians(lat)), 0.2))
            pad_lat = clearance_m / 111320
            if lon < minx - pad_lon or lon > maxx + pad_lon or lat < miny - pad_lat or lat > maxy + pad_lat:
                continue
        ring = (geom.get("coordinates") or [[]])[0]
        if len(ring) < 3:
            continue
        if _point_in_ring(lon, lat, ring):
            return True
        for i in range(len(ring) - 1):
            if _distance_point_to_segment_m(lon, lat, ring[i][0], ring[i][1], ring[i+1][0], ring[i+1][1]) < clearance_m:
                return True
    return False

def _nearest_road_point(lon, lat, roads):
    best_point = (lon, lat)
    best_feat = None
    best_dist = float("inf")
    for feat in roads:
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") != "LineString" or len(coords) < 1:
            continue
        for pt in coords:
            d = _haversine(lon, lat, pt[0], pt[1])
            if d < best_dist:
                best_dist = d
                best_point = (pt[0], pt[1])
                best_feat = feat
    return best_point[0], best_point[1], best_dist, best_feat

def _in_exclusion(lon, lat, excl_zones):
    for (ex, ey, er) in excl_zones:
        if _haversine(lon, lat, ex, ey) < er:
            return True
    return False

@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    p = req.params or {}
    bbox = req.bbox
    aoi_ring = _extract_outer_ring(req.aoi)

    # Parameters
    pph        = float(p.get("pph", 5.0))
    waste_kg   = float(p.get("waste_kg", 0.42))
    grid_m     = float(p.get("grid_m", 200))
    r1_m       = float(p.get("r1_m", 150))
    r2_m       = float(p.get("r2_m", 300))
    r3_m       = float(p.get("r3_m", 500))
    w1 = float(p.get("w1", 0.6))
    w2 = float(p.get("w2", 0.3))
    w3 = float(p.get("w3", 0.1))
    ring_sum = max(w1 + w2 + w3, 1e-9)
    w1, w2, w3 = [w / ring_sum for w in (w1, w2, w3)]
    fill_thr   = float(p.get("fill_threshold", 0.8))
    truck_access_m  = float(p.get("truck_access_m", 50))
    truck_min_w     = float(p.get("truck_min_road_w", 3.5))
    tricycle_access_m = float(p.get("tricycle_access_m", 100))
    tricycle_min_w    = float(p.get("tricycle_min_road_w", 2.0))
    truck_bin_kg   = float(p.get("truck_bin_kg", 240))
    tricycle_bin_kg = float(p.get("tricycle_bin_kg", 80))
    foot_capacity_kg = float(p.get("foot_capacity_kg", 4))
    max_bins   = int(p.get("max_bins", 30))
    min_school_m = float(p.get("min_school_m", 20))
    min_hospital_m = float(p.get("min_hospital_m", 20))
    min_hydro_m = float(p.get("min_hydro_m", 15))
    weight_waste = float(p.get("weight_waste", 0.45))
    weight_access = float(p.get("weight_access", 0.25))
    weight_sensitive = float(p.get("weight_sensitive", 0.15))
    weight_hydro = float(p.get("weight_hydro", 0.15))
    total_w = max(weight_waste + weight_access + weight_sensitive + weight_hydro, 1e-9)
    weight_waste, weight_access, weight_sensitive, weight_hydro = [w/total_w for w in (weight_waste, weight_access, weight_sensitive, weight_hydro)]

    # Spatial service coverage optimization controls
    min_bin_spacing_m = float(p.get("min_bin_spacing_m", max(0.85 * r1_m, 75)))
    sel_weight_local = float(p.get("sel_weight_local", 0.35))
    sel_weight_coverage = float(p.get("sel_weight_coverage", 0.35))
    sel_weight_waste = float(p.get("sel_weight_waste", 0.20))
    sel_penalty_overlap = float(p.get("sel_penalty_overlap", 0.10))
    adaptive_mode = bool(p.get("adaptive_mode", False))
    sel_sum = max(sel_weight_local + sel_weight_coverage + sel_weight_waste, 1e-9)
    sel_weight_local, sel_weight_coverage, sel_weight_waste = [w / sel_sum for w in (sel_weight_local, sel_weight_coverage, sel_weight_waste)]

    osm = req.osm_data or {}
    buildings_fc = _normalize_fc(osm.get("buildings"))
    roads_fc = _normalize_fc(osm.get("roads"))
    schools_fc = _normalize_fc(osm.get("schools"))
    hospitals_fc = _normalize_fc(osm.get("hospitals"))
    hydro_fc = _normalize_fc(osm.get("hydro"))
    waste_bins_fc = _normalize_fc(osm.get("waste_bins"))

    if aoi_ring:
        buildings_fc = _filter_fc_to_aoi(buildings_fc, aoi_ring)
        roads_fc = _filter_fc_to_aoi(roads_fc, aoi_ring)
        schools_fc = _filter_fc_to_aoi(schools_fc, aoi_ring)
        hospitals_fc = _filter_fc_to_aoi(hospitals_fc, aoi_ring)
        hydro_fc = _filter_fc_to_aoi(hydro_fc, aoi_ring)
        waste_bins_fc = _filter_fc_to_aoi(waste_bins_fc, aoi_ring)

    buildings = buildings_fc.get("features", [])
    for feat in buildings:
        feat["_bbox"] = _feature_bbox(feat)
    roads = roads_fc.get("features", [])
    schools = schools_fc.get("features", [])
    hospitals = hospitals_fc.get("features", [])
    hydro = hydro_fc.get("features", [])
    waste_bins = waste_bins_fc.get("features", [])

    # Build grid cells only inside AOI if provided
    lat_deg_per_m = 1 / 111320
    lon_deg_per_m = 1 / (111320 * math.cos(math.radians((bbox.south + bbox.north) / 2)))
    dlat = grid_m * lat_deg_per_m
    dlon = grid_m * lon_deg_per_m

    lat_steps = max(1, int((bbox.north - bbox.south) / dlat))
    lon_steps = max(1, int((bbox.east - bbox.west) / dlon))
    if lat_steps * lon_steps > 2500:
        factor = math.sqrt(lat_steps * lon_steps / 2500)
        lat_steps = max(1, int(lat_steps / factor))
        lon_steps = max(1, int(lon_steps / factor))

    # Count buildings per cell using centroids
    cell_buildings = {}
    for feat in buildings:
        cx, cy = _centroid(feat)
        if cx is None:
            continue
        if aoi_ring and not _point_in_ring(cx, cy, aoi_ring):
            continue
        ci = int((cy - bbox.south) / dlat)
        cj = int((cx - bbox.west) / dlon)
        ci = max(0, min(ci, lat_steps - 1))
        cj = max(0, min(cj, lon_steps - 1))
        cell_buildings[(ci, cj)] = cell_buildings.get((ci, cj), 0) + 1

    waste_grid = []
    total_pop = 0.0
    total_waste = 0.0
    cell_index = 0
    for i in range(lat_steps):
        for j in range(lon_steps):
            cell_lat = bbox.south + (i + 0.5) * dlat
            cell_lon = bbox.west  + (j + 0.5) * dlon
            if aoi_ring and not _point_in_ring(cell_lon, cell_lat, aoi_ring):
                continue
            n_bld = cell_buildings.get((i, j), 0)
            pop = n_bld * pph
            waste = pop * waste_kg
            total_pop += pop
            total_waste += waste
            waste_grid.append({
                "idx": cell_index,
                "lat": cell_lat, "lon": cell_lon,
                "buildings": n_bld,
                "population": round(pop, 1),
                "waste_kg_day": round(waste, 2),
                "i": i, "j": j
            })
            cell_index += 1

    max_waste = max((c["waste_kg_day"] for c in waste_grid), default=1) or 1

    def _build_site_metrics(lon, lat, *, strict_constraints=True, preserve_position=False, source="new", base_cell=None):
        if aoi_ring and not _point_in_ring(lon, lat, aoi_ring):
            return None
        if not preserve_position and _candidate_on_building(lon, lat, buildings, clearance_m=6.0):
            moved = False
            offsets = [(0,0), (18,0), (-18,0), (0,18), (0,-18), (18,18), (-18,18), (18,-18), (-18,-18)]
            for ox, oy in offsets:
                lon2 = lon + ox / (111320 * max(math.cos(math.radians(lat)), 0.2))
                lat2 = lat + oy / 111320
                if aoi_ring and not _point_in_ring(lon2, lat2, aoi_ring):
                    continue
                if not _candidate_on_building(lon2, lat2, buildings, clearance_m=6.0):
                    lon, lat = lon2, lat2
                    moved = True
                    break
            if not moved:
                return None

        road_d = _nearest_road_dist(lon, lat, roads) if roads else 999
        school_d = _nearest_feature_distance_m(lon, lat, schools) if schools else float("inf")
        hospital_d = _nearest_feature_distance_m(lon, lat, hospitals) if hospitals else float("inf")
        hydro_d = _nearest_feature_distance_m(lon, lat, hydro) if hydro else float("inf")

        if strict_constraints and (school_d < min_school_m or hospital_d < min_hospital_m or hydro_d < min_hydro_m):
            return None

        base_waste = (base_cell or {}).get("waste_kg_day", 0.0)
        waste_norm = base_waste / max_waste if max_waste else 0.0
        access_score = max(0.0, 1 - road_d / 250.0)
        sensitive_score = min(1.0, min(school_d / max(min_school_m*4,1), hospital_d / max(min_hospital_m*4,1)))
        hydro_score = min(1.0, hydro_d / max(min_hydro_m*5,1))
        local_score = (
            weight_waste * waste_norm +
            weight_access * access_score +
            weight_sensitive * sensitive_score +
            weight_hydro * hydro_score
        )

        ring_cells = []
        r1_pop = r2_pop = r3_pop = 0.0
        waste_r1 = waste_r2 = waste_r3 = 0.0
        for gc in waste_grid:
            d = _haversine(lon, lat, gc["lon"], gc["lat"])
            if d <= r1_m:
                ring_cells.append((gc["idx"], w1))
                r1_pop += gc["population"]
                waste_r1 += gc["waste_kg_day"]
            elif d <= r2_m:
                ring_cells.append((gc["idx"], w2))
                r2_pop += gc["population"]
                waste_r2 += gc["waste_kg_day"]
            elif d <= r3_m:
                ring_cells.append((gc["idx"], w3))
                r3_pop += gc["population"]
                waste_r3 += gc["waste_kg_day"]

        base_weighted_pop = r1_pop * w1 + r2_pop * w2 + r3_pop * w3
        base_weighted_waste = waste_r1 * w1 + waste_r2 * w2 + waste_r3 * w3
        if base_weighted_pop <= 0 and base_waste <= 0:
            return None

        payload = {
            "lon": lon, "lat": lat,
            "road_dist_m": round(road_d if math.isfinite(road_d) else 999, 1),
            "school_dist_m": round(school_d if math.isfinite(school_d) else 999, 1),
            "hospital_dist_m": round(hospital_d if math.isfinite(hospital_d) else 999, 1),
            "hydro_dist_m": round(hydro_d if math.isfinite(hydro_d) else 999, 1),
            "score": round(local_score, 4),
            "access_score": round(access_score, 3),
            "waste_score": round(waste_norm, 3),
            "sensitive_score": round(sensitive_score, 3),
            "hydro_score": round(hydro_score, 3),
            "ring_cells": ring_cells,
            "base_weighted_pop": round(base_weighted_pop, 2),
            "base_weighted_waste": round(base_weighted_waste, 2),
            "population_r1": round(r1_pop, 1),
            "population_r2": round(r2_pop, 1),
            "population_r3": round(r3_pop, 1),
            "waste_r1_kg_day": round(waste_r1, 2),
            "waste_r2_kg_day": round(waste_r2, 2),
            "waste_r3_kg_day": round(waste_r3, 2),
            "optimization_source": source,
        }
        if base_cell:
            payload.update(base_cell)
        return payload

    candidates = []
    for cell in waste_grid:
        built = _build_site_metrics(cell["lon"], cell["lat"], strict_constraints=True, preserve_position=False, source="new", base_cell=cell)
        if built:
            candidates.append(built)

    max_base_cov = max((c.get("base_weighted_pop", 0.0) for c in candidates), default=1.0) or 1.0
    max_base_waste = max((c.get("base_weighted_waste", 0.0) for c in candidates), default=1.0) or 1.0

    def _select_sites(candidate_pool, max_sites, seed_sites=None):
        selected = [dict(s) for s in (seed_sites or [])]
        selected_idx = set()
        best_cell_weight: Dict[int, float] = {}
        for s in selected:
            for cell_id, ring_weight in s.get("ring_cells", []):
                best_cell_weight[cell_id] = max(best_cell_weight.get(cell_id, 0.0), ring_weight)

        while len(selected) < max_sites:
            best_candidate = None
            best_i = None
            best_objective = -1e18
            best_metrics = None
            for i, c in enumerate(candidate_pool):
                if i in selected_idx:
                    continue
                if any(_haversine(c["lon"], c["lat"], p2["lon"], p2["lat"]) < min_bin_spacing_m for p2 in selected):
                    continue

                inc_pop = 0.0
                inc_waste = 0.0
                overlap_pop = 0.0
                for cell_id, ring_weight in c["ring_cells"]:
                    cur_w = best_cell_weight.get(cell_id, 0.0)
                    cell = waste_grid[cell_id]
                    if ring_weight > cur_w:
                        inc_pop += (ring_weight - cur_w) * cell["population"]
                        inc_waste += (ring_weight - cur_w) * cell["waste_kg_day"]
                    overlap_pop += min(cur_w, ring_weight) * cell["population"]

                local_norm = max(0.0, min(1.0, c["score"]))
                coverage_norm = inc_pop / max_base_cov
                waste_gain_norm = inc_waste / max_base_waste
                overlap_norm = overlap_pop / max(c.get("base_weighted_pop", 1.0), 1.0)
                objective = (
                    sel_weight_local * local_norm +
                    sel_weight_coverage * coverage_norm +
                    sel_weight_waste * waste_gain_norm -
                    sel_penalty_overlap * overlap_norm
                )
                if objective > best_objective:
                    best_objective = objective
                    best_candidate = c
                    best_i = i
                    best_metrics = {
                        "incremental_weighted_pop": round(inc_pop, 2),
                        "incremental_weighted_waste": round(inc_waste, 2),
                        "overlap_penalty": round(overlap_norm, 4),
                        "selection_objective": round(objective, 4),
                    }

            if best_candidate is None:
                break

            chosen = dict(best_candidate)
            chosen.update(best_metrics or {})
            selected.append(chosen)
            selected_idx.add(best_i)
            for cell_id, ring_weight in chosen["ring_cells"]:
                best_cell_weight[cell_id] = max(best_cell_weight.get(cell_id, 0.0), ring_weight)
        return selected

    proposed = _select_sites(candidates, max_bins)
    optimization_strategy = "fresh_network"
    adaptive_recommended = False
    adaptive_performed = False
    adaptive_trigger_reason = None
    auto_adaptive = bool(p.get("auto_adaptive", True))  # v13: always on by default
    rebalancing_result: Dict[str, Any] = {"performed": False}

    def _coverage_from_bins(bin_list):
        return _coverage_pop_proposed(waste_grid, bin_list, r1_m)

    before_cov_pop = _coverage_pop(waste_grid, waste_bins, r1_m)
    primary_after_cov_pop = _coverage_from_bins(proposed)

    # ── v13: Auto-adaptive — triggered automatically when existing network outperforms ──
    if waste_bins and primary_after_cov_pop + 1e-6 < before_cov_pop:
        adaptive_recommended = True
        adaptive_trigger_reason = (
            "Le réseau cartographié existant couvre plus de population dans R1 que la proposition optimisée initiale. "
            "L'analyse adaptative a été déclenchée automatiquement pour réutiliser les bacs existants les plus performants comme ancres."
        )
        # v13: auto_adaptive=True means we always try adaptive without asking the user
        if adaptive_mode or auto_adaptive:
            existing_candidates = []
            for feat in waste_bins:
                lon0, lat0 = _centroid(feat)
                if lon0 is None or lat0 is None:
                    continue
                built = _build_site_metrics(lon0, lat0, strict_constraints=False, preserve_position=True, source="existing-upgraded", base_cell={"idx": f"existing_{len(existing_candidates)}", "waste_kg_day": 0.0, "population": 0.0, "buildings": 0})
                if built:
                    built["selection_objective"] = round(built.get("score", 0.0), 4)
                    built["incremental_weighted_pop"] = round(built.get("base_weighted_pop", 0.0), 2)
                    built["incremental_weighted_waste"] = round(built.get("base_weighted_waste", 0.0), 2)
                    built["overlap_penalty"] = 0.0
                    existing_candidates.append(built)

            if existing_candidates:
                max_sites_alt = max(max_bins, len(existing_candidates))
                alt_proposed = _select_sites(candidates, max_sites_alt, seed_sites=existing_candidates)
                alt_after_cov_pop = _coverage_from_bins(alt_proposed)
                if alt_after_cov_pop + 1e-6 >= primary_after_cov_pop:
                    proposed = alt_proposed
                    optimization_strategy = "adaptive_reuse_existing"
                    adaptive_performed = True

    # ── v13: Spatial Rebalancing ────────────────────────────────────────────────────
    # After selection, detect redundant bins (high coverage overlap with neighbours)
    # and relocate them to uncovered high-demand zones for uniform spatial distribution.
    def _compute_cell_covered(bin_list, radius_m):
        """Return set of (cell_idx, ring_weight) best weight per cell given a bin list."""
        cell_best: Dict[int, float] = {}
        for b in bin_list:
            for cell_id, ring_weight in b.get("ring_cells", []):
                cell_best[cell_id] = max(cell_best.get(cell_id, 0.0), ring_weight)
        return cell_best

    def _bin_unique_coverage(b, others, cell_best_without):
        """Population uniquely covered by bin b (not covered at same level by others)."""
        unique_pop = 0.0
        for cell_id, ring_weight in b.get("ring_cells", []):
            cur_best = cell_best_without.get(cell_id, 0.0)
            cell = waste_grid[cell_id]
            if ring_weight > cur_best:
                unique_pop += (ring_weight - cur_best) * cell["population"]
        return unique_pop

    def _spatial_rebalance(bin_list, candidate_pool, grid, r1, n_iters=2):
        """
        Iteratively:
        1. Find bin with lowest unique coverage contribution (most redundant)
        2. Remove it and find the uncovered high-demand cell
        3. Place a new bin from candidates that best covers that gap
        Returns (new_bin_list, stats)
        """
        bins = [dict(b) for b in bin_list]
        redundant_detected = 0
        bins_relocated = 0
        before_cov = _coverage_from_bins(bins)

        for _ in range(n_iters):
            if len(bins) < 2:
                break
            # Build cell coverage without each bin
            min_unique = float('inf')
            worst_idx = None
            for i, b in enumerate(bins):
                others = bins[:i] + bins[i+1:]
                cell_best_others = _compute_cell_covered(others, r1)
                unique = _bin_unique_coverage(b, others, cell_best_others)
                if unique < min_unique:
                    min_unique = unique
                    worst_idx = i

            if worst_idx is None:
                break

            # Only rebalance if bin is truly redundant (unique coverage < 5% of avg)
            avg_pop = sum(c["population"] for c in grid) / max(len(grid), 1)
            if min_unique > avg_pop * 0.5:
                break  # All bins are contributing — no rebalancing needed

            redundant_detected += 1
            removed_bin = bins.pop(worst_idx)
            removed_pos = (removed_bin["lon"], removed_bin["lat"])

            # Find uncovered high-demand grid cells
            cell_best_now = _compute_cell_covered(bins, r1)
            # Cells with no/low coverage sorted by waste demand
            gap_cells = sorted(
                [c for c in grid if cell_best_now.get(c["idx"], 0.0) < 0.3 and c["waste_kg_day"] > 0],
                key=lambda c: -c["waste_kg_day"]
            )
            if not gap_cells:
                bins.append(removed_bin)  # Restore — no gap to fill
                break

            target_cell = gap_cells[0]
            target_lon, target_lat = target_cell["lon"], target_cell["lat"]

            # Find best unused candidate near that gap (not already selected, not too close to others)
            selected_positions = [(b["lon"], b["lat"]) for b in bins]
            best_replacement = None
            best_score = -1.0
            for c in candidate_pool:
                already_in = any(
                    abs(c["lon"] - b["lon"]) < 1e-6 and abs(c["lat"] - b["lat"]) < 1e-6
                    for b in bins
                )
                if already_in:
                    continue
                if any(_haversine(c["lon"], c["lat"], lon2, lat2) < min_bin_spacing_m
                       for lon2, lat2 in selected_positions):
                    continue
                # Score = proximity to gap + local suitability
                dist_to_gap = _haversine(c["lon"], c["lat"], target_lon, target_lat)
                proximity_score = max(0.0, 1.0 - dist_to_gap / max(r1_m * 2, 1.0))
                combined = 0.6 * proximity_score + 0.4 * c.get("score", 0.0)
                if combined > best_score:
                    best_score = combined
                    best_replacement = c

            if best_replacement is not None:
                new_bin = dict(best_replacement)
                new_bin["selection_objective"] = round(best_score, 4)
                new_bin["incremental_weighted_pop"] = round(new_bin.get("base_weighted_pop", 0.0), 2)
                new_bin["incremental_weighted_waste"] = round(new_bin.get("base_weighted_waste", 0.0), 2)
                new_bin["overlap_penalty"] = 0.0
                new_bin["rebalanced"] = True
                bins.append(new_bin)
                bins_relocated += 1
            else:
                bins.append(removed_bin)  # Restore — no better site found

        after_cov = _coverage_from_bins(bins)
        total_pop = sum(c["population"] for c in grid) or 1.0
        gain_pct = round((after_cov - before_cov) / total_pop * 100, 1)
        stats = {
            "performed": bins_relocated > 0,
            "redundant_detected": redundant_detected,
            "bins_relocated": bins_relocated,
            "coverage_gain_pct": gain_pct,
        }
        return bins, stats

    # Run rebalancing only if we have a reasonable number of bins and candidates
    if len(proposed) >= 3 and len(candidates) > len(proposed):
        proposed, rebalancing_result = _spatial_rebalance(proposed, candidates, waste_grid, r1_m, n_iters=3)

    def _pickup_stats(waste_day, capacity):
        useful_cap = max(capacity * fill_thr, 0.1)
        if waste_day <= 0:
            return 0.0, None
        pickups = round((7.0 * waste_day) / useful_cap, 1)
        days = round(useful_cap / waste_day, 1)
        return pickups, days

    # Enrich proposed bins
    for b in proposed:
        road_d = b["road_dist_m"]
        _, _, _, road_feat = _nearest_road_point(b["lon"], b["lat"], roads) if roads else (b["lon"], b["lat"], 999, None)
        road_width = 0.0
        road_type = "unknown"
        if road_feat:
            tags = road_feat.get("properties", {})
            road_type = tags.get("highway", "unknown")
            try:
                road_width = float(tags.get("width", 0) or 0)
            except Exception:
                road_width = 0.0
            if road_width <= 0:
                lanes = tags.get("lanes")
                try:
                    road_width = float(lanes) * 3.0 if lanes else 0.0
                except Exception:
                    road_width = 0.0
                if road_width <= 0:
                    defaults = {"motorway": 7.0, "trunk": 6.5, "primary": 6.0, "secondary": 5.5, "tertiary": 4.5, "residential": 3.5, "service": 3.0, "unclassified": 3.2, "track": 2.5, "path": 1.2, "footway": 1.0}
                    road_width = defaults.get(road_type, 2.5)

        if road_d <= truck_access_m and road_width >= truck_min_w:
            mode = "truck"
            cap = truck_bin_kg
        elif road_d <= tricycle_access_m and road_width >= tricycle_min_w:
            mode = "tricycle"
            cap = tricycle_bin_kg
        else:
            mode = "foot"
            cap = foot_capacity_kg

        weighted_pop = b["population_r1"] * w1 + b["population_r2"] * w2 + b["population_r3"] * w3
        weighted_waste = b["waste_r1_kg_day"] * w1 + b["waste_r2_kg_day"] * w2 + b["waste_r3_kg_day"] * w3
        pickups, days_between = _pickup_stats(weighted_waste, cap)
        r1_pickups, r1_days = _pickup_stats(b["waste_r1_kg_day"], cap)
        r2_pickups, r2_days = _pickup_stats(b["waste_r2_kg_day"], cap)
        r3_pickups, r3_days = _pickup_stats(b["waste_r3_kg_day"], cap)

        nearest_existing = _nearest_feature_distance_m(b["lon"], b["lat"], waste_bins) if waste_bins else float("inf")
        b["collection_mode"] = mode
        b.setdefault("optimization_source", "new")
        b["road_type"] = road_type
        b["road_width_m"] = round(road_width, 1)
        b["assigned_capacity_kg"] = round(cap, 1)
        b["pickups_per_week"] = pickups
        b["days_between_pickups"] = days_between
        b["r1_pickups_per_week"] = r1_pickups
        b["r2_pickups_per_week"] = r2_pickups
        b["r3_pickups_per_week"] = r3_pickups
        b["r1_days_between_pickups"] = r1_days
        b["r2_days_between_pickups"] = r2_days
        b["r3_days_between_pickups"] = r3_days
        b["weighted_pop"] = round(weighted_pop, 1)
        b["weighted_waste_kg_day"] = round(weighted_waste, 2)
        b["nearest_existing_bin_m"] = None if not math.isfinite(nearest_existing) else round(nearest_existing, 1)
        b["recommendation"] = 'Validate the site on the ground and confirm route access, environmental safety and community acceptance before installation.'

    scores = [b["score"] for b in proposed]
    scores = [b["score"] for b in proposed]
    if scores:
        s = sorted(scores)
        p67 = s[int((len(s)-1) * 0.67)]
        p33 = s[int((len(s)-1) * 0.33)]
    else:
        p67 = p33 = 0
    for b in proposed:
        if b["score"] >= p67:
            b["class"] = "A"
            b["recommendation"] = "Priority site for implementation. This location combines strong waste demand, acceptable access, and safer buffers from sensitive facilities and water features."
        elif b["score"] >= p33:
            b["class"] = "B"
            b["recommendation"] = "Good candidate for secondary deployment. Validate operational access and local acceptance before installation."
        else:
            b["class"] = "C"
            b["recommendation"] = "Reserve or contingency site. Consider only after higher-priority locations have been validated."

    n_bld = len(buildings)
    n_rd  = len(roads)
    n_poi = len(schools) + len(hospitals)
    area_km2 = _bbox_area_km2(bbox)
    density_factor = min(1.0, (n_bld / max(area_km2, 0.01)) / 300.0)
    conf = min(100, int((min(n_bld, 250) / 250) * 40 + (min(n_rd, 150) / 150) * 25 + (min(n_poi, 40) / 40) * 15 + density_factor * 20))

    def scenario_stats(weights):
        sw1, sw2, sw3 = weights
        rows = []
        for b in proposed:
            wp = b["population_r1"] * sw1 + b["population_r2"] * sw2 + b["population_r3"] * sw3
            ww = wp * waste_kg
            rows.append((b, wp, ww))
        cov_pop = sum(r[1] for r in rows)
        avg_pickups = round(sum(_pickup_stats(r[2], (truck_bin_kg if r[0]["collection_mode"] == "truck" else tricycle_bin_kg if r[0]["collection_mode"] == "tricycle" else foot_capacity_kg))[0] for r in rows) / max(len(rows), 1), 1)
        return {"bin_count": len(rows), "coverage_pct": round(min(100, cov_pop / max(total_pop, 1) * 100), 1), "avg_pickups": avg_pickups, "weights": {"r1": sw1, "r2": sw2, "r3": sw3}}

    scenarios = {
        "balanced": scenario_stats((w1, w2, w3)),
        "walk_first": scenario_stats((0.40, 0.35, 0.25)),
        "access_first": scenario_stats((0.70, 0.20, 0.10)),
    }
    recommended_scenario = max(scenarios.items(), key=lambda kv: kv[1]["coverage_pct"] - 0.6 * kv[1]["avg_pickups"])[0] if scenarios else "balanced"

    comparison = {
        "existing_bins_count": len(waste_bins),
        "proposed_bins_count": len(proposed),
        "median_nearest_existing_m": None,
    }
    nearests = sorted([b["nearest_existing_bin_m"] for b in proposed if b.get("nearest_existing_bin_m") is not None])
    if nearests:
        mid = len(nearests)//2
        comparison["median_nearest_existing_m"] = nearests[mid] if len(nearests)%2==1 else round((nearests[mid-1]+nearests[mid])/2,1)

    before_cov_pop = _coverage_pop(waste_grid, waste_bins, r1_m)
    after_cov_pop = _coverage_pop_proposed(waste_grid, proposed, r1_m)
    before_cov_pct = round(min(100.0, before_cov_pop / max(total_pop, 1) * 100), 1)
    after_cov_pct = round(min(100.0, after_cov_pop / max(total_pop, 1) * 100), 1)
    gain_pop = round(after_cov_pop - before_cov_pop, 1)
    gain_pct = round(after_cov_pct - before_cov_pct, 1)

    mode_counts = {
        "truck": sum(1 for b in proposed if b.get("collection_mode") == "truck"),
        "tricycle": sum(1 for b in proposed if b.get("collection_mode") == "tricycle"),
        "foot": sum(1 for b in proposed if b.get("collection_mode") == "foot"),
    }
    class_a = sum(1 for b in proposed if b.get("class") == "A")
    class_b = sum(1 for b in proposed if b.get("class") == "B")
    class_c = sum(1 for b in proposed if b.get("class") == "C")
    feasible_share = ((mode_counts["truck"] + mode_counts["tricycle"]) / max(len(proposed), 1)) * 100
    readiness_score = int(min(100, 0.35 * conf + 0.25 * after_cov_pct + 0.2 * feasible_share + 0.2 * (class_a / max(len(proposed),1) * 100)))
    readiness_label = "High" if readiness_score >= 70 else ("Moderate" if readiness_score >= 40 else "Low")

    confidence_drivers = [
        {"label": "Mapped buildings", "value": min(100, int((min(n_bld, 250) / 250) * 100))},
        {"label": "Mapped roads", "value": min(100, int((min(n_rd, 150) / 150) * 100))},
        {"label": "Mapped facilities", "value": min(100, int((min(n_poi, 40) / 40) * 100))},
        {"label": "Building density in AOI", "value": int(density_factor * 100)},
    ]

    # Remaining coverage gaps after optimization
    underserved_features = []
    well_served_pop = 0.0
    underserved_pop = 0.0
    no_service_pop = 0.0
    well_served_waste = 0.0
    underserved_waste = 0.0
    no_service_waste = 0.0
    top_gap_cells = []
    if waste_grid:
        for cell in waste_grid:
            best_ring = None
            best_weight = 0.0
            for b in proposed:
                d = _haversine(cell["lon"], cell["lat"], b["lon"], b["lat"])
                if d <= r1_m:
                    best_ring = "R1"; best_weight = max(best_weight, w1)
                elif d <= r2_m:
                    best_ring = best_ring or "R2"; best_weight = max(best_weight, w2)
                elif d <= r3_m:
                    best_ring = best_ring or "R3"; best_weight = max(best_weight, w3)
            pop = float(cell.get("population", 0) or 0)
            waste = float(cell.get("waste_kg_day", 0) or 0)
            status = "well_served"
            if best_ring is None:
                status = "no_service"
                no_service_pop += pop
                no_service_waste += waste
            elif best_ring != "R1":
                status = "underserved"
                underserved_pop += pop
                underserved_waste += waste
            else:
                well_served_pop += pop
                well_served_waste += waste

            if status != "well_served":
                feat = {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [cell["lon"], cell["lat"]]},
                    "properties": {
                        "cell_id": cell.get("idx"),
                        "i": cell.get("i"), "j": cell.get("j"),
                        "population": round(pop, 1),
                        "waste_kg_day": round(waste, 2),
                        "status": status,
                        "best_ring": best_ring or "None",
                    }
                }
                underserved_features.append(feat)
        gap_sorted = sorted(underserved_features, key=lambda f: f["properties"].get("waste_kg_day", 0), reverse=True)
        top_gap_cells = [
            {
                "cell_id": f["properties"].get("cell_id"),
                "grid_ref": f"{f['properties'].get('i')}-{f['properties'].get('j')}",
                "status": f["properties"].get("status"),
                "best_ring": f["properties"].get("best_ring"),
                "population": f["properties"].get("population"),
                "waste_kg_day": f["properties"].get("waste_kg_day"),
            }
            for f in gap_sorted[:5]
        ]
    gap_summary = {
        "well_served_pop": round(well_served_pop, 1),
        "underserved_pop": round(underserved_pop, 1),
        "no_service_pop": round(no_service_pop, 1),
        "well_served_pct": round(min(100.0, well_served_pop / max(total_pop, 1) * 100), 1),
        "underserved_pct": round(min(100.0, underserved_pop / max(total_pop, 1) * 100), 1),
        "no_service_pct": round(min(100.0, no_service_pop / max(total_pop, 1) * 100), 1),
        "underserved_waste_kg_day": round(underserved_waste, 1),
        "no_service_waste_kg_day": round(no_service_waste, 1),
        "count": len(underserved_features),
        "top_gap_cells": top_gap_cells,
    }

    decision_rationale = [
        f"The final network combines local multi-criteria suitability with service-coverage planning. Sites were selected to maximize incremental coverage and waste capture while enforcing a minimum spacing of {round(min_bin_spacing_m)} m between bins.",
        ("An adaptive analysis was performed: mapped bins were reused as optimized anchors because the initial fresh-network proposal underperformed the existing network on short-range coverage." if adaptive_performed else ("The current mapped network outperformed the first-pass optimized network on short-range coverage. Adaptive analysis is recommended if the objective is to improve the current network rather than replace it." if adaptive_recommended else "The fresh-network strategy outperformed or matched the mapped network and was therefore retained as the preferred layout.")),
        f"After optimization, {round(after_cov_pct,1)}% of the estimated population is within R1 of a proposed bin. Remaining coverage gaps represent {gap_summary['underserved_pct']}% underserved population in outer rings and {gap_summary['no_service_pct']}% outside service coverage, which should guide the next deployment phase.",
    ]
    if top_gap_cells:
        tg = top_gap_cells[0]
        decision_rationale.append(
            f"The most critical remaining hotspot is grid {tg['grid_ref']}, currently marked as {tg['status']} with about {tg['waste_kg_day']} kg/day of waste. This area should be prioritized in a next expansion round or in field validation."
        )

    comparison.update({
        "before_coverage_pop": round(before_cov_pop, 1),
        "after_coverage_pop": round(after_cov_pop, 1),
        "before_coverage_pct": before_cov_pct,
        "after_coverage_pct": after_cov_pct,
        "gain_pop": gain_pop,
        "gain_pct": gain_pct,
    })

    top_priority_sites = []
    for i, bp in enumerate(proposed[:5], 1):
        top_priority_sites.append({
            "id": f"P{i}",
            "class": bp.get("class", "—"),
            "mode": bp.get("collection_mode", "—"),
            "score": round(bp.get("score", 0), 4),
            "waste_kg_day": round(bp.get("weighted_waste_kg_day", 0), 2),
            "pickups_per_week": bp.get("pickups_per_week", 0),
            "nearest_existing_bin_m": bp.get("nearest_existing_bin_m"),
            "optimization_source": bp.get("optimization_source", "new"),
        })

    risk_mitigation = []
    if len(waste_bins) == 0:
        risk_mitigation.append({
            "risk": "No mapped existing bins in AOI",
            "mitigation": "Proceed with optimization, but validate current service conditions on the ground before deployment."
        })
    if conf < 40:
        risk_mitigation.append({
            "risk": "Low confidence due to sparse OSM data",
            "mitigation": "Improve mapping coverage and validate proposed sites with local stakeholders before implementation."
        })
    if feasible_share < 50:
        risk_mitigation.append({
            "risk": "Limited operational access for motorized collection",
            "mitigation": "Prioritize tricycle or on-foot collection in constrained areas and review street accessibility during field validation."
        })
    if optimization_strategy == "adaptive_reuse_existing":
        risk_mitigation.append({
            "risk": "Existing network already performs well on short-range coverage",
            "mitigation": "Reuse the current network as the spatial backbone and only add new bins where they close underserved gaps."
        })
    if not risk_mitigation:
        risk_mitigation.append({
            "risk": "Standard implementation risks remain",
            "mitigation": "Confirm land availability, community acceptance, and operating responsibilities before installation."
        })

    summary = {
        "n_buildings": len(buildings),
        "n_roads": len(roads),
        "n_schools": len(schools),
        "n_hospitals": len(hospitals),
        "n_existing_bins": len(waste_bins),
        "n_proposed": len(proposed),
        "total_pop": round(total_pop),
        "total_waste_kg_day": round(total_waste, 1),
        "area_km2": round(area_km2, 3),
        "confidence": conf,
        "message": (
            "No existing waste bins found in OpenStreetMap inside the selected area. Optimization was still completed using multi-criteria scoring and service coverage planning." if len(waste_bins) == 0 else
            ("The first-pass optimized network underperformed the current mapped network on R1 coverage. Review the before/after comparison and relaunch an adaptive analysis if you want to improve the current network instead of replacing it." if adaptive_recommended and not adaptive_performed else (
                "Existing bins were retained as a comparison layer only." if optimization_strategy == "fresh_network" else
                "An adaptive analysis was performed: existing bins were reused as optimized anchors and new sites were added only where they improved spatial coverage."
            ))
        ),
        "optimization_strategy": optimization_strategy,
        "adaptive_recommended": adaptive_recommended and not adaptive_performed,
        "adaptive_performed": adaptive_performed,
        "adaptive_trigger_reason": adaptive_trigger_reason,
        "recommendation": ("Prioritize adaptive upgrade of the current network: keep the strongest existing sites, then add only the new sites that improve spatial coverage and waste capture." if adaptive_performed else ("Review the existing network against the optimized proposal. If maintaining and improving the current network is operationally preferable, run the adaptive analysis to retain the strongest existing bins and add only the sites that close the main coverage gaps." if adaptive_recommended else "Implement Class A sites first, starting with areas of highest weighted waste demand and validating access and safety on the ground. Use existing OSM bins only as a visual comparison layer.")),
        "before_after": {
            "before_coverage_pop": round(before_cov_pop, 1),
            "after_coverage_pop": round(after_cov_pop, 1),
            "before_coverage_pct": before_cov_pct,
            "after_coverage_pct": after_cov_pct,
            "gain_pop": gain_pop,
            "gain_pct": gain_pct,
        },
        "implementation_readiness": {
            "score": readiness_score,
            "label": readiness_label,
            "class_a": class_a,
            "class_b": class_b,
            "class_c": class_c,
            "transport_split": mode_counts,
            "feasible_share_pct": round(feasible_share, 1),
        },
        "confidence_drivers": confidence_drivers,
        "top_priority_sites": top_priority_sites,
        "risk_mitigation": risk_mitigation,
        "coverage_gaps": gap_summary,
        "decision_rationale": decision_rationale,
        "optimization_method": {
            "name": "MCDA + weighted service coverage + overlap penalty + minimum spacing",
            "min_bin_spacing_m": round(min_bin_spacing_m, 1),
            "selection_weights": {
                "local": round(sel_weight_local, 3),
                "coverage": round(sel_weight_coverage, 3),
                "waste": round(sel_weight_waste, 3),
                "overlap_penalty": round(sel_penalty_overlap, 3),
            }
        },
        "params": {
            "pph": pph, "waste_kg": waste_kg, "grid_m": grid_m,
            "r1_m": r1_m, "r2_m": r2_m, "r3_m": r3_m,
            "min_school_m": min_school_m, "min_hospital_m": min_hospital_m, "min_hydro_m": min_hydro_m,
            "weight_waste": weight_waste, "weight_access": weight_access, "weight_sensitive": weight_sensitive, "weight_hydro": weight_hydro,
            "w1": w1, "w2": w2, "w3": w3,
            "min_bin_spacing_m": min_bin_spacing_m,
            "sel_weight_local": sel_weight_local, "sel_weight_coverage": sel_weight_coverage,
            "sel_weight_waste": sel_weight_waste, "sel_penalty_overlap": sel_penalty_overlap,
            "truck_access_m": truck_access_m, "truck_min_road_w": truck_min_w, "tricycle_access_m": tricycle_access_m, "tricycle_min_road_w": tricycle_min_w,
            "truck_bin_kg": truck_bin_kg, "tricycle_bin_kg": tricycle_bin_kg, "foot_capacity_kg": foot_capacity_kg, "fill_threshold": fill_thr,
        }
    }

    return {
        "proposed_bins": proposed,
        "waste_grid": waste_grid,
        "summary": summary,
        "confidence": conf,
        "scenarios": scenarios,
        "recommended_scenario": recommended_scenario,
        "comparison": comparison,
        "rebalancing": rebalancing_result,
        "existing_bins": waste_bins_fc,
        "underserved_cells": {"type": "FeatureCollection", "features": underserved_features},
    }

# ── PDF Report ──────────────────────────────────────────────────────────────

# ── PDF Report — v13.2 Bilingual (FR/EN) with Logo ──────────────────────────

# ── PDF Report — v13.3 Type-Safe Bilingual FR/EN ─────────────────────────────

@app.post("/api/report")
async def generate_report(req: ReportRequest):
    """PDF bilingue (FR/EN) — type-safe, fully tested."""

    # ── Helpers type-safe ────────────────────────────────────────────────────
    def sf(v, d=1, dflt=0.0):
        try: return round(float(v if v is not None else dflt), d)
        except: return float(dflt)
    def si(v, dflt=0):
        try: return int(float(v if v is not None else dflt))
        except: return int(dflt)
    def sp(v, dflt="—"):
        return dflt if v is None else str(v)
    def fmt_f(v, fmt=".1f", dflt=0.0):
        """Format a float safely regardless of incoming type."""
        return format(sf(v, 6, dflt), fmt)

    # ── Data extraction ──────────────────────────────────────────────────────
    analysis    = req.analysis or {}
    summary     = analysis.get("summary", {}) or {}
    proposed    = analysis.get("proposed_bins", []) or []
    scenarios_d = analysis.get("scenarios", {}) or {}
    lang        = ((req.report_lang or "fr").lower().strip())[:2]
    if lang not in ("fr", "en"): lang = "fr"
    city        = (req.city_name or "").strip() or ("Zone analysée" if lang == "fr" else "Analysis area")
    manual_res  = req.manual_check_result or {}

    ba          = summary.get("before_after", {}) or {}
    readiness   = summary.get("implementation_readiness", {}) or {}
    gaps        = summary.get("coverage_gaps", {}) or {}
    conf_drv    = summary.get("confidence_drivers", []) or []
    rationale   = summary.get("decision_rationale", []) or []
    risks       = summary.get("risk_mitigation", []) or []
    conf        = si(analysis.get("confidence", 0))

    # ── Bilingual strings ────────────────────────────────────────────────────
    _S = {
      "fr": {
        "doc_title": "RAPPORT D'ANALYSE",
        "cover_sub": "Planification de la Collecte des Déchets Urbains",
        "cover_tagline": "Outil de planification géospatiale · Afrique subsaharienne · Banque Mondiale",
        "cover_zone": "Zone d'analyse", "cover_date": "Généré le",
        "kpi_bldg": "Bâtiments OSM", "kpi_pop": "Population estimée",
        "kpi_waste": "kg déchets / jour", "kpi_bins": "Bacs proposés",
        "s1": "1. Résumé Exécutif", "s2": "2. Analyse de Couverture",
        "s3": "3. Sites Prioritaires", "s4": "4. Scénarios Opérationnels",
        "s5": "5. Lacunes de Couverture", "s6": "6. Faisabilité Opérationnelle",
        "s7": "7. Justification de Décision", "s8": "8. Notes Méthodologiques",
        "s9": "9. Analyse Point Check Manuel",
        "ba_title": "Couverture Avant / Après (rayon R1)",
        "col_before": "Avant", "col_after": "Après", "col_change": "Variation",
        "row_pop": "Population couverte R1", "row_cov": "Couverture (%)",
        "row_out": "Hors service", "people": "personnes", "pts": "pts",
        "lbl_strat": "Stratégie d'optimisation",
        "lbl_conf": "Niveau de confiance", "lbl_ready": "Maturité d'implémentation",
        "col_id": "Bac", "col_sc": "Score", "col_md": "Mode",
        "col_pp": "Pop. R1", "col_wt": "Déch. R1 kg/j",
        "col_fq": "Fréq./sem", "col_rd": "Route (m)", "col_ev": "Éc./Hôp./Eau (m)",
        "sc_bins": "Bacs", "sc_cov": "Couverture", "sc_pk": "Ramassages/sem",
        "row_well": "Bien desservi R1", "row_und": "Mal desservi (anneaux ext.)",
        "row_no": "Hors service", "row_wno": "Déchets hors service",
        "top_ht": "Hotspot principal",
        "truck": "Camion", "tricycle": "Tricycle", "foot": "À pied",
        "col_rsk": "Risque", "col_mit": "Mitigation",
        "rec_sc": "Scénario recommandé",
        "mb": (
            "Les résultats sont fondés sur les données OpenStreetMap dont la complétude varie "
            "selon les zones. La population est estimée par proxy (bâtiments × PPH) et non par "
            "recensement officiel. Les emplacements proposés doivent être validés sur le terrain "
            "avant tout investissement. Le modèle ne tient pas compte des contraintes foncières, "
            "des autorisations municipales ni des dynamiques sociales locales."
        ),
        "disc": (
            "Ce rapport est un outil d'aide à la décision conforme aux standards ODD 11.6.1 "
            "(ONU-Habitat / Banque Mondiale). Toute décision d'investissement doit être précédée "
            "d'une enquête terrain et d'une consultation communautaire."
        ),
        "footer": f"UrbanSanity v{VERSION} · Données OSM © OpenStreetMap contributors (ODbL) · "
                  "World Bank What a Waste 2.0 (2018) · WACA Cameroun",
        "m_banner": "⚠  Résultats indicatifs — positions placées manuellement par l'utilisateur",
        "m_s1": "9.1 Synthèse collective", "m_s2": "9.2 Détail par point",
        "m_pts": "Points analysés", "m_pop": "Population zone R1",
        "m_waste": "Déchets zone R1", "m_cov": "Couverture combinée",
        "m_vh": "Viabilité élevée", "m_vm": "Viabilité modérée", "m_vl": "Viabilité faible",
        "vib_h": "Élevée", "vib_m": "Modérée", "vib_l": "Faible",
        "m_note": (
            "Ces positions sont évaluées avec les mêmes paramètres MCDA que l'optimisation "
            "algorithmique (grille de demande, anneaux R1/R2/R3, contraintes de sécurité), "
            "sans être soumises au processus de sélection spatiale. Les résultats reflètent "
            "uniquement la suitabilité locale du site choisi."
        ),
        "cls_a": "Classe A — Priorité haute",
        "cls_b": "Classe B — Priorité moyenne",
        "cls_c": "Classe C — Priorité basse",
      },
      "en": {
        "doc_title": "ANALYSIS REPORT",
        "cover_sub": "Urban Waste Collection Planning",
        "cover_tagline": "Geospatial planning tool · Sub-Saharan Africa · World Bank",
        "cover_zone": "Analysis zone", "cover_date": "Generated on",
        "kpi_bldg": "OSM Buildings", "kpi_pop": "Estimated population",
        "kpi_waste": "kg waste / day", "kpi_bins": "Proposed bins",
        "s1": "1. Executive Summary", "s2": "2. Coverage Analysis",
        "s3": "3. Priority Sites", "s4": "4. Operational Scenarios",
        "s5": "5. Coverage Gaps", "s6": "6. Operational Feasibility",
        "s7": "7. Decision Rationale", "s8": "8. Methodological Notes",
        "s9": "9. Manual Point Check Analysis",
        "ba_title": "Before / After coverage (R1 radius)",
        "col_before": "Before", "col_after": "After", "col_change": "Change",
        "row_pop": "Population covered R1", "row_cov": "Coverage (%)",
        "row_out": "Outside service", "people": "people", "pts": "pts",
        "lbl_strat": "Optimisation strategy",
        "lbl_conf": "Confidence level", "lbl_ready": "Implementation readiness",
        "col_id": "Bin", "col_sc": "Score", "col_md": "Mode",
        "col_pp": "Pop. R1", "col_wt": "Waste R1 kg/d",
        "col_fq": "Freq./wk", "col_rd": "Road (m)", "col_ev": "Sch./Hosp./Water (m)",
        "sc_bins": "Bins", "sc_cov": "Coverage", "sc_pk": "Pickups/wk",
        "row_well": "Well served R1", "row_und": "Underserved (outer rings)",
        "row_no": "Outside service", "row_wno": "Waste outside service",
        "top_ht": "Top hotspot",
        "truck": "Truck", "tricycle": "Tricycle", "foot": "On foot",
        "col_rsk": "Risk", "col_mit": "Mitigation",
        "rec_sc": "Recommended scenario",
        "mb": (
            "Results are based on OpenStreetMap data whose completeness varies by area. "
            "Population is estimated by proxy (buildings × PPH), not from census data. "
            "Proposed locations must be validated in the field before any investment. "
            "The model does not account for land tenure constraints, municipal permits, "
            "or local social dynamics."
        ),
        "disc": (
            "This report is a decision-support tool aligned with SDG 11.6.1 "
            "(UN-Habitat / World Bank). Any investment decision must be preceded "
            "by field validation and community consultation."
        ),
        "footer": f"UrbanSanity v{VERSION} · OSM data © OpenStreetMap contributors (ODbL) · "
                  "World Bank What a Waste 2.0 (2018) · WACA",
        "m_banner": "⚠  Indicative results — manually placed points",
        "m_s1": "9.1 Collective summary", "m_s2": "9.2 Point detail",
        "m_pts": "Points analysed", "m_pop": "Population zone R1",
        "m_waste": "Waste zone R1", "m_cov": "Combined coverage",
        "m_vh": "High viability", "m_vm": "Moderate viability", "m_vl": "Low viability",
        "vib_h": "High", "vib_m": "Moderate", "vib_l": "Low",
        "m_note": (
            "These positions are evaluated with the same MCDA parameters as the algorithmic "
            "optimisation (demand grid, R1/R2/R3 rings, safety constraints), without being "
            "subject to the spatial selection process. Results reflect only the local "
            "suitability of the chosen site."
        ),
        "cls_a": "Class A — High priority",
        "cls_b": "Class B — Medium priority",
        "cls_c": "Class C — Lower priority",
      }
    }
    def L(k): return _S[lang].get(k, _S["fr"].get(k, k))

    # ── ReportLab setup ──────────────────────────────────────────────────────
    from reportlab.graphics.shapes import Drawing, Rect, Circle, Line, Polygon
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=2*cm, bottomMargin=2.2*cm)
    base = getSampleStyleSheet()

    # World Bank colour palette
    CN = colors.HexColor("#002244")   # navy
    CB = colors.HexColor("#00467F")   # blue
    CS = colors.HexColor("#0082CA")   # sky
    CP = colors.HexColor("#E6F2FA")   # pale
    CI = colors.HexColor("#F0F7FC")   # ice
    CG = colors.HexColor("#D0DAE1")   # gray rule
    CGR= colors.HexColor("#1A7A4A")   # green
    CAM= colors.HexColor("#B87B00")   # amber
    CR = colors.HexColor("#C0392B")   # red

    def ps(nm, **kw):
        return ParagraphStyle(nm, parent=base["Normal"], **kw)

    H1 = ps("_h1", textColor=CB, fontSize=14, fontName="Helvetica-Bold",
            spaceBefore=14, spaceAfter=5, borderPad=0,
            borderColor=None, borderWidth=0)
    H2 = ps("_h2", textColor=CB, fontSize=11, fontName="Helvetica-Bold",
            spaceBefore=8, spaceAfter=3)
    BD = ps("_bd", fontSize=9.5, leading=14, spaceAfter=5, alignment=TA_JUSTIFY)
    SM = ps("_sm", fontSize=8, textColor=colors.HexColor("#5A6E84"), leading=11)
    KP = ps("_kp", fontSize=20, fontName="Helvetica-Bold", textColor=CB, alignment=TA_CENTER)
    KL = ps("_kl", fontSize=7.5, textColor=colors.HexColor("#5A6E84"),
            alignment=TA_CENTER, leading=9)
    FT = ps("_ft", fontSize=7.5, textColor=colors.HexColor("#5A6E84"), leading=10)
    now = datetime.now().strftime("%d/%m/%Y  %H:%M")

    story = []

    # ── LOGO ─────────────────────────────────────────────────────────────────
    # Déchets + Agriculture + Géospatiale
    def make_logo(w=54, h=54):
        d = Drawing(w, h)
        # Globe géospatial
        d.add(Circle(27, 27, 24, strokeColor=CS, strokeWidth=2, fillColor=CP))
        d.add(Line(27, 4, 27, 50, strokeColor=CS, strokeWidth=0.8))
        d.add(Line(4, 27, 50, 27, strokeColor=CS, strokeWidth=0.8))
        # Arc latitude supérieur
        d.add(Line(10, 38, 44, 38, strokeColor=CS, strokeWidth=0.5))
        d.add(Line(10, 16, 44, 16, strokeColor=CS, strokeWidth=0.5))
        # Feuille agriculture (triangle stylisé)
        d.add(Polygon([27,47, 20,35, 16,24, 27,19, 38,24, 34,35],
                      strokeColor=CGR, strokeWidth=1.2,
                      fillColor=colors.HexColor("#27ae60")))
        # Bac déchets (blanc centré sur la feuille)
        d.add(Rect(22, 23, 10, 12, strokeColor=CN, strokeWidth=1.4, fillColor=CN))
        d.add(Rect(20, 34, 14, 3,  strokeColor=CN, strokeWidth=1.1, fillColor=CS))
        d.add(Rect(24, 37, 6,  2,  strokeColor=CS, strokeWidth=0.8, fillColor=CS))
        d.add(Line(25, 24, 25, 34, strokeColor=CP, strokeWidth=0.8))
        d.add(Line(27, 24, 27, 34, strokeColor=CP, strokeWidth=0.8))
        d.add(Line(29, 24, 29, 34, strokeColor=CP, strokeWidth=0.8))
        # Pin de localisation (géospatiale)
        d.add(Circle(41, 40, 4.5, strokeColor=CR, strokeWidth=1.2,
                     fillColor=colors.HexColor("#e74c3c")))
        d.add(Polygon([41, 30, 37, 38, 45, 38],
                      strokeColor=CR, strokeWidth=1, fillColor=colors.HexColor("#e74c3c")))
        return d

    logo = make_logo()

    # ── COVER ─────────────────────────────────────────────────────────────────
    inner = Table([
        [Paragraph("UrbanSanity",
                   ps("_br", fontSize=24, fontName="Helvetica-Bold",
                      textColor=colors.white, leading=26))],
        [Paragraph(L("cover_tagline"),
                   ps("_tg", fontSize=8.5,
                      textColor=colors.HexColor("#90b8d4"), leading=11))],
    ], colWidths=[14.5*cm])

    hdr = Table([[logo, inner]], colWidths=[1.7*cm, 14.8*cm])
    hdr.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), CN),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))
    story += [hdr, HRFlowable(width="100%", thickness=3, color=CS), Spacer(1, 0.35*cm)]

    story.append(Paragraph(L("doc_title"),
                            ps("_dt", fontSize=20, fontName="Helvetica-Bold",
                               textColor=CB, alignment=TA_CENTER)))
    story.append(Paragraph(L("cover_sub"),
                            ps("_cs", fontSize=12,
                               textColor=colors.HexColor("#5A6E84"), alignment=TA_CENTER,
                               spaceAfter=8)))

    zone_tbl = Table([
        [Paragraph(f"<b>{L('cover_zone')} :</b>  {city}",
                   ps("_zn", fontSize=11, textColor=CN, alignment=TA_CENTER))],
        [Paragraph(f"{L('cover_date')} : {now}",
                   ps("_zd", fontSize=9, textColor=colors.HexColor("#5A6E84"),
                      alignment=TA_CENTER))],
    ], colWidths=[16.5*cm])
    zone_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), CI),
        ("BOX",           (0, 0), (-1, -1), 1.5, CS),
        ("TOPPADDING",    (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story += [Spacer(1, 0.25*cm), zone_tbl, Spacer(1, 0.35*cm)]

    # KPI strip
    n_b  = si(summary.get("n_buildings",  0))
    t_p  = si(summary.get("total_pop",     0))
    t_w  = sf(summary.get("total_waste_kg_day", 0), 0)
    n_pr = si(summary.get("n_proposed",    len(proposed)))

    kpi_data = [
        [Paragraph(f"{n_b:,}",   KP), Paragraph(f"{t_p:,}", KP),
         Paragraph(fmt_f(t_w, ".0f"), KP), Paragraph(str(n_pr), KP)],
        [Paragraph(L("kpi_bldg"), KL), Paragraph(L("kpi_pop"), KL),
         Paragraph(L("kpi_waste"), KL), Paragraph(L("kpi_bins"), KL)],
    ]
    kpi_t = Table(kpi_data, colWidths=[4.1*cm]*4)
    kpi_t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), CP),
        ("BACKGROUND",    (0, 1), (-1, 1), CI),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("BOX",           (0, 0), (-1, -1), 1.5, CB),
        ("INNERGRID",     (0, 0), (-1, -1), 0.4, colors.white),
        ("LINEBELOW",     (0, 0), (-1, 0), 0.5, CS),
    ]))
    story.append(kpi_t)
    story.append(Spacer(1, 0.4*cm))

    # ── SECTION 1: Résumé Exécutif ────────────────────────────────────────────
    story.append(Paragraph(L("s1"), H1))

    conf_lbl   = ("Élevée" if lang=="fr" else "High") if conf >= 70 else \
                 (("Modérée" if lang=="fr" else "Moderate") if conf >= 40 else \
                  ("Faible" if lang=="fr" else "Low"))
    read_score = si(readiness.get("score", 0))
    read_lbl   = sp(readiness.get("label", "—"))
    opt_strat  = sp(summary.get("optimization_strategy", "—")).replace("_", " ")

    exec_rows = [
        [L("lbl_strat"),  opt_strat],
        [L("lbl_conf"),   f"{conf}/100 — {conf_lbl}"],
        [L("lbl_ready"),  f"{read_score}/100 — {read_lbl}"],
    ]
    et = Table(exec_rows, colWidths=[5.5*cm, 11*cm])
    et.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CI]),
        ("BACKGROUND",     (0, 0), (0, -1), CP),
        ("GRID",           (0, 0), (-1, -1), 0.3, CG),
        ("FONTSIZE",       (0, 0), (-1, -1), 8.5),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 7),
    ]))
    story.append(et)

    # Recommendation box
    rec = sp(summary.get("recommendation", ""))
    if rec and rec != "—":
        rt = Table([[Paragraph(f"★ {rec}",
                               ps("_rc", fontSize=9.5, textColor=CN))]],
                   colWidths=[16.5*cm])
        rt.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), CP),
            ("BOX",           (0, 0), (-1, -1), 1.5, CS),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        story += [Spacer(1, 0.2*cm), rt]

    # Before/After table
    story += [Spacer(1, 0.3*cm), Paragraph(L("ba_title"), H2)]

    g_pct  = sf(ba.get("gain_pct", 0), 1)
    sign   = "+" if g_pct >= 0 else ""
    ba_rows = [
        [L("row_pop"),
         fmt_f(ba.get("before_coverage_pop", 0), ".0f"),
         fmt_f(ba.get("after_coverage_pop",  0), ".0f"),
         f"{sign}{g_pct:.1f} {L('pts')}"],
        [L("row_cov"),
         f"{fmt_f(ba.get('before_coverage_pct', 0), '.1f')}%",
         f"{fmt_f(ba.get('after_coverage_pct',  0), '.1f')}%",
         f"{sign}{g_pct:.1f} {L('pts')}"],
        [L("row_out"),
         "—",
         f"{fmt_f(gaps.get('no_service_pct', 0), '.1f')}%",
         f"{si(gaps.get('no_service_pop', 0)):,} {L('people')}"],
    ]
    bat = Table(
        [[L("row_pop")[0:28], L("col_before"), L("col_after"), L("col_change")]] + ba_rows,
        colWidths=[5.5*cm, 3*cm, 3*cm, 5*cm]
    )
    bat.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), CB),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, CI]),
        ("GRID",          (0, 0), (-1, -1), 0.3, CG),
        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    story.append(bat)

    # ── SECTION 2: Priority Sites ─────────────────────────────────────────────
    story += [PageBreak(), Paragraph(L("s3"), H1)]

    MODE_LBL = {"truck": L("truck"), "tricycle": L("tricycle"), "foot": L("foot")}
    CLASS_BG = {
        "A": colors.HexColor("#D1FAE5"),
        "B": colors.HexColor("#FEF3C7"),
        "C": colors.HexColor("#DBEAFE"),
    }
    hdr_row = [L("col_id"), L("col_sc"), L("col_md"),
               L("col_pp"), L("col_wt"), L("col_fq"),
               L("col_rd"), L("col_ev")]
    rows_ = [hdr_row]
    for idx, b in enumerate(proposed[:50], 1):
        cls  = sp(b.get("class", "C"), "C")
        mode = MODE_LBL.get(sp(b.get("collection_mode", "—"), "—"),
                             sp(b.get("collection_mode", "—")))
        rows_.append([
            Paragraph(f"<b>#{idx}</b> {cls}", SM),
            Paragraph(fmt_f(b.get("score", 0), ".3f"), SM),
            Paragraph(mode, SM),
            Paragraph(f"{si(b.get('population_r1', 0)):,}", SM),
            Paragraph(fmt_f(b.get("waste_r1_kg_day", 0), ".1f"), SM),
            Paragraph(f"{sp(b.get('pickups_per_week','—'))}×", SM),
            Paragraph(f"{si(b.get('road_dist_m', 0))}m", SM),
            Paragraph(
                f"{si(b.get('school_dist_m',0))}/{si(b.get('hospital_dist_m',0))}"
                f"/{si(b.get('hydro_dist_m',0))}",
                SM),
        ])
    st = Table(rows_, colWidths=[1.4*cm, 1.3*cm, 1.6*cm,
                                  1.5*cm, 1.9*cm, 1.4*cm, 1.4*cm, 2.5*cm])
    s_sty = [
        ("BACKGROUND",    (0, 0), (-1, 0), CN),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
        ("GRID",          (0, 0), (-1, -1), 0.25, CG),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, CI]),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    for idx, b in enumerate(proposed[:50], 1):
        cls = sp(b.get("class", "C"), "C")
        s_sty.append(("BACKGROUND", (0, idx), (0, idx),
                       CLASS_BG.get(cls, colors.white)))
    st.setStyle(TableStyle(s_sty))
    story.append(st)

    # Legend for classes
    story.append(Spacer(1, 0.2*cm))
    leg_data = [[Paragraph(f"■ {L('cls_a')}", ps("_la", fontSize=7.5, textColor=CGR)),
                 Paragraph(f"■ {L('cls_b')}", ps("_lb", fontSize=7.5, textColor=CAM)),
                 Paragraph(f"■ {L('cls_c')}", ps("_lc", fontSize=7.5, textColor=CB))]]
    lt = Table(leg_data, colWidths=[5.5*cm, 5.5*cm, 5.5*cm])
    story.append(lt)

    # ── SECTION 3: Scenarios ──────────────────────────────────────────────────
    story += [Spacer(1, 0.4*cm), Paragraph(L("s4"), H1)]
    SC_NAMES = {
        "balanced":     ("⚖  " + ("Équilibré" if lang=="fr" else "Balanced")),
        "walk_first":   ("🚶  " + ("Priorité marche" if lang=="fr" else "Walk priority")),
        "access_first": ("🚛  " + ("Priorité accès" if lang=="fr" else "Access priority")),
    }
    rec_sc = sp(analysis.get("recommended_scenario", "balanced"))
    sc_hdr = ["", L("sc_bins"), L("sc_cov"), L("sc_pk")]
    sc_rows = [sc_hdr]
    for key, sc in (scenarios_d or {}).items():
        star  = "★ " if key == rec_sc else "  "
        label = SC_NAMES.get(key, key)
        sc_rows.append([
            Paragraph(f"{star}<b>{label}</b>", SM),
            Paragraph(str(si(sc.get("bin_count", 0))), SM),
            Paragraph(f"{fmt_f(sc.get('coverage_pct', 0), '.1f')}%", SM),
            Paragraph(f"{fmt_f(sc.get('avg_pickups', 0), '.1f')}×", SM),
        ])
    sct = Table(sc_rows, colWidths=[7*cm, 2.5*cm, 2.5*cm, 4.5*cm])
    sct.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), CB),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("GRID",          (0, 0), (-1, -1), 0.3, CG),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, CI]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
    ]))
    story.append(sct)

    # ── SECTION 4: Coverage gaps ──────────────────────────────────────────────
    story += [Spacer(1, 0.4*cm), Paragraph(L("s5"), H1)]
    gap_data = [
        [L("row_well"),
         f"{fmt_f(gaps.get('well_served_pct',  0), '.1f')}%",
         f"{si(gaps.get('well_served_pop',  0)):,} {L('people')}"],
        [L("row_und"),
         f"{fmt_f(gaps.get('underserved_pct', 0), '.1f')}%",
         f"{si(gaps.get('underserved_pop',  0)):,} {L('people')}"],
        [L("row_no"),
         f"{fmt_f(gaps.get('no_service_pct', 0), '.1f')}%",
         f"{si(gaps.get('no_service_pop',   0)):,} {L('people')}"],
        [L("row_wno"), "—",
         f"{fmt_f(gaps.get('no_service_waste_kg_day', 0), '.1f')} kg/day"],
    ]
    gg = Table(gap_data, colWidths=[6.5*cm, 2.5*cm, 7.5*cm])
    gg.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CI]),
        ("GRID",           (0, 0), (-1, -1), 0.3, CG),
        ("BACKGROUND",     (0, 0), (0, -1), CP),
        ("FONTSIZE",       (0, 0), (-1, -1), 8.5),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 7),
    ]))
    story.append(gg)
    top_cells = gaps.get("top_gap_cells", []) or []
    if top_cells:
        c0 = top_cells[0] or {}
        story += [Spacer(1, 0.1*cm), Paragraph(
            f"{L('top_ht')}: {sp(c0.get('grid_ref','?'))} — "
            f"{fmt_f(c0.get('waste_kg_day',0), '.1f')} kg/day — "
            f"{si(c0.get('population',0)):,} {L('people')}",
            FT)]

    # ── SECTION 5: Feasibility ────────────────────────────────────────────────
    story += [Spacer(1, 0.4*cm), Paragraph(L("s6"), H1)]
    tsplit = readiness.get("transport_split", {}) or {}
    feas = [
        [L("lbl_ready"),   f"{read_score}/100 — {read_lbl}"],
        ["Classe A / Class A", str(si(readiness.get("class_a", 0)))],
        ["Classe B / Class B", str(si(readiness.get("class_b", 0)))],
        ["Classe C / Class C", str(si(readiness.get("class_c", 0)))],
        [L("truck"),       str(si(tsplit.get("truck",    0)))],
        [L("tricycle"),    str(si(tsplit.get("tricycle", 0)))],
        [L("foot"),        str(si(tsplit.get("foot",     0)))],
    ]
    ft = Table(feas, colWidths=[6.5*cm, 10*cm])
    ft.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CI]),
        ("BACKGROUND",     (0, 0), (0, -1), CP),
        ("GRID",           (0, 0), (-1, -1), 0.3, CG),
        ("FONTSIZE",       (0, 0), (-1, -1), 8.5),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 7),
    ]))
    story.append(ft)

    # Risks
    if risks:
        story.append(Spacer(1, 0.25*cm))
        rr = [[L("col_rsk"), L("col_mit")]] + \
             [[Paragraph(sp(r.get("risk","")), SM),
               Paragraph(sp(r.get("mitigation","")), SM)] for r in risks]
        rt2 = Table(rr, colWidths=[5*cm, 11.5*cm])
        rt2.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), CB),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID",          (0, 0), (-1, -1), 0.3, CG),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, CI]),
            ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ]))
        story.append(rt2)

    # ── SECTION 6: Decision rationale + confidence drivers ───────────────────
    if rationale or conf_drv:
        story.append(PageBreak())
    if rationale:
        story.append(Paragraph(L("s7"), H1))
        for item in rationale:
            if item: story.append(Paragraph(f"• {sp(item)}", BD))
    if conf_drv:
        story += [Spacer(1, 0.3*cm), Paragraph(L("s2"), H1)]
        cd_rows = [["Driver / Indicateur", "%"]] + \
                  [[sp(d.get("label","")), f"{si(d.get('value',0))}%"] for d in conf_drv]
        cdt = Table(cd_rows, colWidths=[12.5*cm, 4*cm])
        cdt.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), CB),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID",          (0, 0), (-1, -1), 0.3, CG),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, CI]),
            ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
        ]))
        story.append(cdt)

    # ── SECTION 7: Methodological notes ──────────────────────────────────────
    story += [PageBreak(), Paragraph(L("s8"), H1), Paragraph(L("mb"), BD),
              Spacer(1, 0.2*cm)]
    disc_t = Table([[Paragraph(L("disc"),
                               ps("_di", fontSize=9, textColor=CN))]],
                   colWidths=[16.5*cm])
    disc_t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), CP),
        ("BOX",           (0, 0), (-1, -1), 1, CS),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(disc_t)

    # ── SECTION 8: Manual Point Check (conditional) ──────────────────────────
    if manual_res and (manual_res.get("points") or []):
        story += [PageBreak(), Paragraph(L("s9"), H1)]
        bnr = Table([[Paragraph(L("m_banner"),
                                ps("_mb", fontSize=9, fontName="Helvetica-Bold",
                                   textColor=colors.HexColor("#92400e")))]],
                    colWidths=[16.5*cm])
        bnr.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#FFF8E6")),
            ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#D97706")),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story += [bnr, Spacer(1, 0.2*cm)]

        coll = manual_res.get("collective", {}) or {}
        story.append(Paragraph(L("m_s1"), H2))
        cd = [
            [L("m_pts"),   str(si(coll.get("n_points",      0)))],
            [L("m_pop"),   f"{si(coll.get('total_pop_r1',   0)):,} {L('people')}"],
            [L("m_waste"), f"{fmt_f(coll.get('total_waste_r1', 0), '.1f')} kg/day"],
            [L("m_cov"),   f"{fmt_f(coll.get('coverage_pct', 0), '.1f')}%"],
            [L("m_vh"),    str(si(coll.get("high_viability",   0)))],
            [L("m_vm"),    str(si(coll.get("medium_viability", 0)))],
            [L("m_vl"),    str(si(coll.get("low_viability",    0)))],
        ]
        ct = Table(cd, colWidths=[7*cm, 9.5*cm])
        ct.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CI]),
            ("BACKGROUND",     (0, 0), (0, -1), CP),
            ("GRID",           (0, 0), (-1, -1), 0.3, CG),
            ("FONTSIZE",       (0, 0), (-1, -1), 8.5),
            ("TOPPADDING",     (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
            ("LEFTPADDING",    (0, 0), (-1, -1), 7),
        ]))
        story.append(ct)

        story += [Spacer(1, 0.25*cm), Paragraph(L("m_s2"), H2)]
        VIB_BG  = {"high":   colors.HexColor("#D1FAE5"),
                   "medium": colors.HexColor("#FEF3C7"),
                   "low":    colors.HexColor("#FEE2E2")}
        VIB_LBL = {"high": L("vib_h"), "medium": L("vib_m"), "low": L("vib_l")}
        ph = ["ID", L("col_sc"), L("col_md"), "Viab.",
              L("col_pp"), L("col_wt"), L("col_fq")]
        pr = [ph]
        for pt in (manual_res.get("points", []) or []):
            vib  = sp(pt.get("viability", "medium"), "medium")
            mode = MODE_LBL.get(sp(pt.get("collection_mode","—"),"—"),
                                 sp(pt.get("collection_mode","—")))
            pr.append([
                Paragraph(f"<b>{sp(pt.get('id','?'))}</b>", SM),
                Paragraph(fmt_f(pt.get("score", 0), ".3f"), SM),
                Paragraph(mode, SM),
                Paragraph(VIB_LBL.get(vib, vib), SM),
                Paragraph(f"{si(pt.get('population_r1',0)):,}", SM),
                Paragraph(fmt_f(pt.get("waste_r1_kg_day",0), ".1f"), SM),
                Paragraph(f"{sp(pt.get('pickups_per_week','—'))}×", SM),
            ])
        ptt = Table(pr, colWidths=[1.5*cm, 1.5*cm, 1.9*cm,
                                    1.6*cm, 1.5*cm, 2.1*cm, 1.5*cm])
        ps_ = [
            ("BACKGROUND",    (0, 0), (-1, 0), CN),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
            ("GRID",          (0, 0), (-1, -1), 0.25, CG),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, CI]),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ]
        for i_, pt in enumerate(manual_res.get("points", []) or [], 1):
            vib = sp(pt.get("viability", "medium"), "medium")
            ps_.append(("BACKGROUND", (3, i_), (3, i_),
                         VIB_BG.get(vib, colors.white)))
        ptt.setStyle(TableStyle(ps_))
        story.append(ptt)
        story += [Spacer(1, 0.25*cm),
                  Paragraph(L("m_note"),
                             ps("_mn", fontSize=8.5,
                                textColor=colors.HexColor("#5A6E84"),
                                leading=12, alignment=TA_JUSTIFY))]

    # ── FOOTER ───────────────────────────────────────────────────────────────
    story += [Spacer(1, 0.6*cm),
              HRFlowable(width="100%", thickness=1, color=CG),
              Spacer(1, 0.1*cm),
              Paragraph(L("footer"), FT)]

    # ── BUILD ────────────────────────────────────────────────────────────────
    try:
        doc.build(story)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"PDF build error: {str(exc)}")

    buf.seek(0)
    tag  = "FR" if lang == "fr" else "EN"
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in city)[:30]
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="UrbanSanity_{slug}_{tag}.pdf"'}
    )


@app.get("/api/how-it-works")
async def how_it_works():
    return {
        "version": VERSION,
        "steps": [
            {"id": 1, "title": "Définir la zone (AOI)", "desc": "Dessiner un polygone ou importer GeoJSON/Shapefile"},
            {"id": 2, "title": "Récupérer données OSM", "desc": "Bâtiments, routes, écoles, hôpitaux, hydrologie via Overpass API"},
            {"id": 3, "title": "Grille de demande", "desc": "Grille de cellules (défaut 200m), population = N_bâtiments × PPH"},
            {"id": 4, "title": "Score candidats", "desc": "Score = 0.6×déchets_normalisés + 0.4×accès_routier"},
            {"id": 5, "title": "Zones d'exclusion", "desc": "Buffer 15m autour écoles, hôpitaux, cours d'eau"},
            {"id": 6, "title": "Anneaux de service", "desc": "R1/R2/R3 avec poids pondérés pour calcul fréquence"},
            {"id": 7, "title": "Mode de collecte", "desc": "Camion / Tricycle / Pied selon largeur route et distance d'accès"},
            {"id": 8, "title": "Scénarios", "desc": "Équilibré / Priorité marche / Priorité accès"},
            {"id": 9, "title": "Coverage gaps", "desc": "Repérage des zones sous-desservies et des hotspots restant après optimisation"},
            {"id": 10, "title": "Rapport PDF", "desc": "Sections décisionnelles : executive brief, gaps, rationale, risques"},
        ],
        "references": [
            "WACA Cameroun : 0.42 kg/hab/jour",
            "DHS Cameroun : 5.0 personnes/ménage",
            "UN-Habitat ODD 11.6.1 : ≤ 200m distance de service",
            "Décret n°2012/2809/PM (Cameroun, gestion déchets)",
            "World Bank What a Waste 2.0 (2018)",
        ]
    }

@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": VERSION}


# ── Manual Point Check — v13.1 ───────────────────────────────────────────────
class ManualCheckRequest(BaseModel):
    points: List[Dict[str, Any]]  # [{id, lat, lon}]
    osm_data: Dict[str, Any]
    bbox: BBox
    params: Optional[Dict[str, Any]] = None
    aoi: Optional[Dict[str, Any]] = None

@app.post("/api/manual_check")
async def manual_check(req: ManualCheckRequest):
    """
    Analyze one or more user-placed points on the map.
    Completely independent from /api/analyze — no interference with optimization.
    Returns full site metrics for each point (same as proposed_bins enrichment)
    plus a collective coverage summary.
    """
    p       = req.params or {}
    osm     = req.osm_data
    bbox    = req.bbox
    aoi_gj  = req.aoi

    # ── Parameters (same defaults as /api/analyze) ──────────────────────────
    pph             = float(p.get("pph", 5.0))
    waste_kg        = float(p.get("waste_kg", 0.42))
    grid_m          = float(p.get("grid_m", 200))
    r1_m            = float(p.get("r1_m", 150))
    r2_m            = float(p.get("r2_m", 300))
    r3_m            = float(p.get("r3_m", 500))
    fill_thr        = float(p.get("fill_threshold", 0.8))
    truck_access_m  = float(p.get("truck_access_m", 50))
    truck_min_w     = float(p.get("truck_min_road_w", 3.5))
    tricycle_acc_m  = float(p.get("tricycle_access_m", 100))
    tricycle_min_w  = float(p.get("tricycle_min_road_w", 2.0))
    truck_bin_kg    = float(p.get("truck_bin_kg", 240))
    tricycle_bin_kg = float(p.get("tricycle_bin_kg", 80))
    foot_cap_kg     = float(p.get("foot_capacity_kg", 4))
    min_school_m    = float(p.get("min_school_m", 20))
    min_hospital_m  = float(p.get("min_hospital_m", 20))
    min_hydro_m     = float(p.get("min_hydro_m", 15))
    weight_waste    = float(p.get("weight_waste", 0.45))
    weight_access   = float(p.get("weight_access", 0.25))
    weight_sensitive= float(p.get("weight_sensitive", 0.15))
    weight_hydro_p  = float(p.get("weight_hydro", 0.15))
    w1              = float(p.get("w1", 0.60))
    w2              = float(p.get("w2", 0.30))
    w3              = float(p.get("w3", 0.10))

    # ── Extract OSM features (same logic as /api/analyze) ──────────────────
    buildings = list((osm.get("buildings") or {}).get("features") or [])
    roads     = list((osm.get("roads") or {}).get("features") or [])
    schools   = list((osm.get("schools") or {}).get("features") or [])
    hospitals = list((osm.get("hospitals") or {}).get("features") or [])
    hydro     = list((osm.get("hydro") or {}).get("features") or [])

    # AOI ring for point-in-polygon checks
    aoi_ring = None
    if aoi_gj:
        obj = aoi_gj
        if obj.get("type") == "FeatureCollection":
            obj = (obj.get("features") or [{}])[0]
        if obj.get("type") == "Feature":
            obj = obj.get("geometry", {})
        if obj.get("type") == "Polygon":
            aoi_ring = (obj.get("coordinates") or [[]])[0] or None
        elif obj.get("type") == "MultiPolygon":
            aoi_ring = ((obj.get("coordinates") or [[[]]])[0] or [[]])[0] or None

    # ── Build waste grid (same as /api/analyze) ─────────────────────────────
    lat_step = grid_m / 111320.0
    mid_lat  = (bbox.south + bbox.north) / 2
    lon_step = grid_m / (111320.0 * max(math.cos(math.radians(mid_lat)), 0.2))

    waste_grid: List[Dict[str, Any]] = []
    idx_counter = 0
    lat_cur = bbox.south
    while lat_cur <= bbox.north:
        lon_cur = bbox.west
        while lon_cur <= bbox.east:
            cell_lon = lon_cur + lon_step / 2
            cell_lat = lat_cur + lat_step / 2
            if aoi_ring and not _point_in_ring(cell_lon, cell_lat, aoi_ring):
                lon_cur += lon_step; continue
            n_bldg = sum(
                1 for b in buildings
                if abs(_centroid(b)[0] - cell_lon) <= lon_step / 2 + 1e-7
                and abs(_centroid(b)[1] - cell_lat) <= lat_step / 2 + 1e-7
            )
            pop      = n_bldg * pph
            waste_kd = pop * waste_kg
            if pop > 0:
                waste_grid.append({
                    "idx": idx_counter, "lat": cell_lat, "lon": cell_lon,
                    "population": round(pop, 1), "waste_kg_day": round(waste_kd, 3),
                    "buildings": n_bldg, "i": 0, "j": 0,
                })
                idx_counter += 1
            lon_cur += lon_step
        lat_cur += lat_step

    max_waste = max((c["waste_kg_day"] for c in waste_grid), default=1.0) or 1.0
    total_pop_aoi = sum(c["population"] for c in waste_grid)

    # ── Pickup stats helper ──────────────────────────────────────────────────
    def _pickup_stats_mc(waste_day, capacity):
        useful = max(capacity * fill_thr, 0.1)
        if waste_day <= 0: return 0.0, None
        pickups = round((7.0 * waste_day) / useful, 1)
        days    = round(useful / waste_day, 1)
        return pickups, days

    # ── Compute coverage from a list of (lat,lon) points ────────────────────
    def _cov_pop_from_latlons(pts, radius_m):
        covered: Dict[int, float] = {}
        for lat0, lon0 in pts:
            for c in waste_grid:
                d = _haversine(lon0, lat0, c["lon"], c["lat"])
                if d <= radius_m:
                    covered[c["idx"]] = max(covered.get(c["idx"], 0.0), 1.0)
        return sum(waste_grid[idx]["population"] * w for idx, w in covered.items() if idx < len(waste_grid))

    # ── Process each manual point ────────────────────────────────────────────
    result_points = []
    for mp in req.points:
        lat0 = float(mp["lat"])
        lon0 = float(mp["lon"])
        pid  = str(mp.get("id", f"M{len(result_points)+1}"))

        # Proximity to OSM features
        road_d = _nearest_road_dist(lon0, lat0, roads) if roads else 999.0
        school_d    = _nearest_feature_distance_m(lon0, lat0, schools)   if schools   else float("inf")
        hospital_d  = _nearest_feature_distance_m(lon0, lat0, hospitals) if hospitals else float("inf")
        hydro_d     = _nearest_feature_distance_m(lon0, lat0, hydro)     if hydro     else float("inf")

        # Road metadata
        _, _, _, road_feat = _nearest_road_point(lon0, lat0, roads) if roads else (lon0, lat0, 999.0, None)
        road_type  = "unknown"
        road_width = 0.0
        if road_feat:
            tags = road_feat.get("properties", {})
            road_type = tags.get("highway", "unknown")
            try: road_width = float(tags.get("width", 0) or 0)
            except: road_width = 0.0
            if road_width <= 0:
                lanes = tags.get("lanes")
                try: road_width = float(lanes) * 3.0 if lanes else 0.0
                except: road_width = 0.0
            if road_width <= 0:
                defaults = {"motorway":7.0,"trunk":6.5,"primary":6.0,"secondary":5.5,"tertiary":4.5,"residential":3.5,"service":3.0,"unclassified":3.2,"track":2.5,"path":1.2,"footway":1.0}
                road_width = defaults.get(road_type, 2.5)

        # MCDA score (indicative — no building-on-building check for manual points)
        waste_cells = [c for c in waste_grid if _haversine(lon0, lat0, c["lon"], c["lat"]) <= r1_m]
        base_waste  = sum(c["waste_kg_day"] for c in waste_cells)
        waste_norm  = base_waste / max_waste
        access_score     = max(0.0, 1 - road_d / 250.0)
        sensitive_score  = min(1.0, min(
            school_d   / max(min_school_m * 4, 1),
            hospital_d / max(min_hospital_m * 4, 1)
        ) if math.isfinite(school_d) and math.isfinite(hospital_d) else 1.0)
        hydro_score = min(1.0, hydro_d / max(min_hydro_m * 5, 1)) if math.isfinite(hydro_d) else 1.0
        local_score = (
            weight_waste * waste_norm +
            weight_access * access_score +
            weight_sensitive * sensitive_score +
            weight_hydro_p * hydro_score
        )

        # Ring populations
        r1_pop = r2_pop = r3_pop = 0.0
        waste_r1 = waste_r2 = waste_r3 = 0.0
        for c in waste_grid:
            d = _haversine(lon0, lat0, c["lon"], c["lat"])
            if d <= r1_m:
                r1_pop += c["population"]; waste_r1 += c["waste_kg_day"]
            elif d <= r2_m:
                r2_pop += c["population"]; waste_r2 += c["waste_kg_day"]
            elif d <= r3_m:
                r3_pop += c["population"]; waste_r3 += c["waste_kg_day"]

        weighted_pop   = r1_pop * w1 + r2_pop * w2 + r3_pop * w3
        weighted_waste = waste_r1 * w1 + waste_r2 * w2 + waste_r3 * w3

        # Collection mode
        if road_d <= truck_access_m and road_width >= truck_min_w:
            mode = "truck"; cap = truck_bin_kg
        elif road_d <= tricycle_acc_m and road_width >= tricycle_min_w:
            mode = "tricycle"; cap = tricycle_bin_kg
        else:
            mode = "foot"; cap = foot_cap_kg

        pickups, days_between = _pickup_stats_mc(weighted_waste, cap)
        r1_pickups, _  = _pickup_stats_mc(waste_r1, cap)
        r2_pickups, _  = _pickup_stats_mc(waste_r2, cap)
        r3_pickups, _  = _pickup_stats_mc(waste_r3, cap)

        # Constraint alerts
        warnings = []
        if math.isfinite(school_d)   and school_d   < min_school_m:   warnings.append(f"École à {round(school_d)}m (min {min_school_m}m)")
        if math.isfinite(hospital_d) and hospital_d < min_hospital_m: warnings.append(f"Hôpital à {round(hospital_d)}m (min {min_hospital_m}m)")
        if math.isfinite(hydro_d)    and hydro_d    < min_hydro_m:    warnings.append(f"Eau/drain à {round(hydro_d)}m (min {min_hydro_m}m)")

        # Viability rating
        constraint_ok = len(warnings) == 0
        if local_score >= 0.6 and constraint_ok: viability = "high"
        elif local_score >= 0.35 or constraint_ok: viability = "medium"
        else: viability = "low"

        note_parts = []
        if warnings: note_parts.append("Contraintes: " + "; ".join(warnings) + ".")
        if weighted_pop < 50: note_parts.append("Zone de faible densité — demande limitée.")
        elif weighted_pop > 500: note_parts.append("Zone à forte demande — priorité de déploiement élevée.")
        if not note_parts: note_parts.append("Position sans contrainte majeure — à valider sur le terrain.")

        result_points.append({
            "id": pid,
            "lat": lat0, "lon": lon0,
            "score": round(local_score, 4),
            "viability": viability,
            "warnings": warnings,
            "note": " ".join(note_parts),
            "road_dist_m": round(road_d if math.isfinite(road_d) else 999, 1),
            "road_type": road_type,
            "road_width_m": round(road_width, 1),
            "school_dist_m": round(school_d if math.isfinite(school_d) else 999, 1),
            "hospital_dist_m": round(hospital_d if math.isfinite(hospital_d) else 999, 1),
            "hydro_dist_m": round(hydro_d if math.isfinite(hydro_d) else 999, 1),
            "population_r1": round(r1_pop, 1),
            "population_r2": round(r2_pop, 1),
            "population_r3": round(r3_pop, 1),
            "waste_r1_kg_day": round(waste_r1, 2),
            "waste_r2_kg_day": round(waste_r2, 2),
            "waste_r3_kg_day": round(waste_r3, 2),
            "weighted_pop": round(weighted_pop, 1),
            "weighted_waste_kg_day": round(weighted_waste, 2),
            "collection_mode": mode,
            "assigned_capacity_kg": round(cap, 1),
            "pickups_per_week": pickups,
            "days_between_pickups": days_between,
            "r1_pickups_per_week": r1_pickups,
            "r2_pickups_per_week": r2_pickups,
            "r3_pickups_per_week": r3_pickups,
            "r1_m": r1_m, "r2_m": r2_m, "r3_m": r3_m,
        })

    # ── Collective summary ───────────────────────────────────────────────────
    latlons = [(rp["lat"], rp["lon"]) for rp in result_points]
    cov_pop = _cov_pop_from_latlons(latlons, r1_m)
    cov_pct = round(min(100, cov_pop / max(total_pop_aoi, 1) * 100), 1)
    total_waste_r1 = sum(rp["waste_r1_kg_day"] for rp in result_points)
    total_pop_r1   = sum(rp["population_r1"] for rp in result_points)

    high_count   = sum(1 for rp in result_points if rp["viability"] == "high")
    medium_count = sum(1 for rp in result_points if rp["viability"] == "medium")
    low_count    = sum(1 for rp in result_points if rp["viability"] == "low")

    collective_note = (
        f"{high_count} point(s) à viabilité élevée, {medium_count} modérée, {low_count} faible. "
        f"La couverture combinée atteint {cov_pct}% de la population de l'AOI dans le rayon R1. "
        "Ces résultats sont indicatifs et doivent être confrontés à l'optimisation spatiale algorithmique."
    )

    return {
        "points": result_points,
        "collective": {
            "n_points": len(result_points),
            "total_pop_r1": round(total_pop_r1, 1),
            "total_waste_r1": round(total_waste_r1, 2),
            "coverage_pct": cov_pct,
            "high_viability": high_count,
            "medium_viability": medium_count,
            "low_viability": low_count,
            "note": collective_note,
        }
    }


# ── Static Frontend Serving (Railway / production) ─────────────────────────
# Activated when the frontend/ folder exists next to main.py
import os as _os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse as _FileResponse

_FRONTEND_DIR = _os.path.join(_os.path.dirname(__file__), "frontend")

if _os.path.isdir(_FRONTEND_DIR):
    # Serve individual static assets (CSS, JS, images…)
    app.mount("/static-assets", StaticFiles(directory=_FRONTEND_DIR), name="static-assets")

    @app.get("/app.js")
    async def serve_appjs():
        return _FileResponse(_os.path.join(_FRONTEND_DIR, "app.js"),
                             media_type="application/javascript")

    @app.get("/styles.css")
    async def serve_css():
        return _FileResponse(_os.path.join(_FRONTEND_DIR, "styles.css"),
                             media_type="text/css")

    # SPA catch-all — serves index.html for every non-API path
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Let API routes handle /api/* and /health
        if full_path.startswith("api/") or full_path == "health":
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Not found")
        # Try to serve the exact file first (favicon, images, etc.)
        candidate = _os.path.join(_FRONTEND_DIR, full_path)
        if _os.path.isfile(candidate):
            return _FileResponse(candidate)
        # SPA fallback → index.html
        return _FileResponse(_os.path.join(_FRONTEND_DIR, "index.html"))
