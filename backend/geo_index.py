"""
UrbanSanity — Index spatiaux (grille régulière) pour accélérer l'analyse.

Les fonctions d'origine parcouraient TOUS les bâtiments et TOUS les sommets de
routes pour chaque cellule candidate (des dizaines de millions de calculs sur un
grand quartier). Ces index ne regardent que le voisinage immédiat, avec une
recherche en anneaux qui garantit le MÊME résultat que la version exhaustive
(même plus proche voisin, même règle de départage en cas d'égalité).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

M_PER_DEG = 111320.0
# Marge de sécurité entre la métrique locale de la grille et haversine / l'approximation
# équirectangulaire utilisée par les fonctions de distance (écarts < 1 % sur quelques km).
SAFETY = 0.97


class GridIndex:
    """Grille régulière en degrés ; chaque case contient une liste d'objets."""

    def __init__(self, cell_m: float, ref_lat: float):
        self.cell_m = float(cell_m)
        self.dlat = cell_m / M_PER_DEG
        self.dlon = cell_m / (M_PER_DEG * max(math.cos(math.radians(ref_lat)), 0.2))
        self.cells: Dict[Tuple[int, int], List[Any]] = {}
        self.imin = self.jmin = 10 ** 12
        self.imax = self.jmax = -10 ** 12

    def key(self, lon: float, lat: float) -> Tuple[int, int]:
        return int(math.floor(lon / self.dlon)), int(math.floor(lat / self.dlat))

    def _touch(self, i: int, j: int):
        if i < self.imin: self.imin = i
        if i > self.imax: self.imax = i
        if j < self.jmin: self.jmin = j
        if j > self.jmax: self.jmax = j

    def add_point(self, lon: float, lat: float, item: Any):
        k = self.key(lon, lat)
        self.cells.setdefault(k, []).append(item)
        self._touch(*k)

    def add_bbox(self, minx: float, miny: float, maxx: float, maxy: float, item: Any):
        i0, j0 = self.key(minx, miny)
        i1, j1 = self.key(maxx, maxy)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                self.cells.setdefault((i, j), []).append(item)
        self._touch(i0, j0)
        self._touch(i1, j1)

    def query_bbox(self, minx: float, miny: float, maxx: float, maxy: float) -> Iterable[Any]:
        i0, j0 = self.key(minx, miny)
        i1, j1 = self.key(maxx, maxy)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                lst = self.cells.get((i, j))
                if lst:
                    yield from lst

    def ring(self, ci: int, cj: int, k: int) -> Iterable[Any]:
        """Objets des cases situées exactement à la distance de Tchebychev k."""
        if k == 0:
            lst = self.cells.get((ci, cj))
            if lst:
                yield from lst
            return
        for i in range(ci - k, ci + k + 1):
            for j in (cj - k, cj + k):
                lst = self.cells.get((i, j))
                if lst:
                    yield from lst
        for j in range(cj - k + 1, cj + k):
            for i in (ci - k, ci + k):
                lst = self.cells.get((i, j))
                if lst:
                    yield from lst

    def max_ring(self, ci: int, cj: int) -> int:
        if not self.cells:
            return -1
        return max(abs(ci - self.imin), abs(ci - self.imax), abs(cj - self.jmin), abs(cj - self.jmax))

    def ring_lower_bound_m(self, lat: float, k: int) -> float:
        """Distance minimale (m) entre un point de la case centrale et tout objet hors des anneaux 0..k."""
        kx = M_PER_DEG * max(math.cos(math.radians(abs(lat) + 0.5)), 0.2)
        return k * min(self.dlat * M_PER_DEG, self.dlon * kx) * SAFETY


# ── Sommets de routes : plus proche sommet (haversine) ─────────────────────────
class RoadVertexIndex:
    def __init__(self, roads: List[dict], haversine: Callable, ref_lat: float, cell_m: float = 120.0,
                 line_only: bool = False):
        self.h = haversine
        self.idx = GridIndex(cell_m, ref_lat)
        self.roads = roads
        for fi, feat in enumerate(roads):
            geom = feat.get("geometry", {}) or {}
            if line_only and geom.get("type") != "LineString":
                continue
            coords = geom.get("coordinates", []) or []
            if not coords:
                continue
            for vi, pt in enumerate(coords):
                try:
                    x, y = float(pt[0]), float(pt[1])
                except Exception:
                    continue
                self.idx.add_point(x, y, (x, y, fi, vi))

    def nearest(self, lon: float, lat: float):
        """Retourne (distance, x, y, fi) du sommet le plus proche ; départage par ordre d'origine."""
        best = (float("inf"), 0, 0)
        best_pt = None
        ci, cj = self.idx.key(lon, lat)
        kmax = self.idx.max_ring(ci, cj)
        k = 0
        while k <= kmax:
            for (x, y, fi, vi) in self.idx.ring(ci, cj, k):
                d = self.h(lon, lat, x, y)
                cand = (d, fi, vi)
                if cand < best:
                    best = cand
                    best_pt = (x, y, fi)
            if best_pt is not None and best[0] <= self.idx.ring_lower_bound_m(lat, k):
                break
            k += 1
        if best_pt is None:
            return float("inf"), lon, lat, None
        return best[0], best_pt[0], best_pt[1], best_pt[2]


# ── Bâtiments : test « point sur/près d'un bâtiment » ─────────────────────────
class BuildingIndex:
    def __init__(self, buildings: List[dict], ref_lat: float, cell_m: float = 60.0):
        self.idx = GridIndex(cell_m, ref_lat)
        self.buildings = buildings
        self._global: List[int] = []
        for bi, feat in enumerate(buildings):
            geom = feat.get("geometry", {}) or {}
            if geom.get("type") != "Polygon":
                continue
            bb = feat.get("_bbox")
            if not bb:
                # même comportement que l'original : sans bbox, le bâtiment est toujours examiné
                self._global.append(bi)
                continue
            self.idx.add_bbox(bb[0], bb[1], bb[2], bb[3], bi)

    def candidates(self, lon: float, lat: float, clearance_m: float) -> List[int]:
        pad_lon = clearance_m / (M_PER_DEG * max(math.cos(math.radians(lat)), 0.2))
        pad_lat = clearance_m / M_PER_DEG
        out = list(self._global)
        seen = set(out)
        for bi in self.idx.query_bbox(lon - pad_lon, lat - pad_lat, lon + pad_lon, lat + pad_lat):
            if bi not in seen:
                seen.add(bi)
                out.append(bi)
        return out


# ── Entités quelconques (écoles, santé, hydrographie, bacs) : distance minimale ──
class FeatureDistIndex:
    """Reproduit exactement _nearest_feature_distance_m, en ne regardant que le voisinage."""

    def __init__(self, features: List[dict], haversine: Callable, seg_dist: Callable,
                 point_in_ring: Callable, ref_lat: float, cell_m: float = 100.0):
        self.h = haversine
        self.seg = seg_dist
        self.pir = point_in_ring
        self.idx = GridIndex(cell_m, ref_lat)
        self.polys = GridIndex(cell_m, ref_lat)
        self.n = 0
        for feat in features:
            geom = feat.get("geometry", {}) or {}
            gtype = geom.get("type")
            coords = geom.get("coordinates") or []
            if gtype == "Point" and coords:
                self.idx.add_point(coords[0], coords[1], ("p", coords[0], coords[1]))
                self.n += 1
            elif gtype == "LineString" and coords:
                for i in range(len(coords) - 1):
                    a, b = coords[i], coords[i + 1]
                    self.idx.add_bbox(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]),
                                      ("s", a[0], a[1], b[0], b[1]))
                    self.n += 1
            elif gtype == "Polygon" and coords and coords[0]:
                ring = coords[0]
                xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
                self.polys.add_bbox(min(xs), min(ys), max(xs), max(ys), ring)
                for i in range(len(ring) - 1):
                    a, b = ring[i], ring[i + 1]
                    self.idx.add_bbox(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]),
                                      ("s", a[0], a[1], b[0], b[1]))
                    self.n += 1

    def distance(self, lon: float, lat: float) -> float:
        # Point à l'intérieur d'un polygone → 0 (comme l'original)
        for ring in self.polys.query_bbox(lon, lat, lon, lat):
            if self.pir(lon, lat, ring):
                return 0.0
        best = float("inf")
        ci, cj = self.idx.key(lon, lat)
        kmax = self.idx.max_ring(ci, cj)
        k = 0
        while k <= kmax:
            for it in self.idx.ring(ci, cj, k):
                if it[0] == "p":
                    d = self.h(lon, lat, it[1], it[2])
                else:
                    d = self.seg(lon, lat, it[1], it[2], it[3], it[4])
                if d < best:
                    best = d
            if best <= self.idx.ring_lower_bound_m(lat, k):
                break
            k += 1
        return best
