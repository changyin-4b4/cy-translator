"""
XY-Cut with On-Demand Isolation Testing.
Neighbor heuristic + dynamic blockage cleaning for PDF layout analysis.

Supports both absolute PDF-point coordinates and normalised (0-1) coordinates.
Set norm_coords=True when passing normalised coordinates.
"""

from typing import List, Tuple, Dict, Protocol, runtime_checkable

# ========== ALGORITHM SETTINGS (absolute PDF points) ==========
MIN_GAP_THRESHOLD = 5.0
MAX_BRIDGE_COUNT = 2

# Thresholds for normalised coordinates (0-1 range)
_NORM_V_GAP = 0.010    # 1.0% page width for vertical cuts (column splits)
_NORM_H_GAP = 0.005    # 0.5% for horizontal cuts (paragraph breaks)
_NORM_H_THRESH_CAP = 0.03
_NORM_V_THRESH_CAP = 0.015
_NORM_FALLBACK = 0.005


@runtime_checkable
class _Word(Protocol):
    x0: float
    y0: float
    x1: float
    y1: float


class LayoutParseError(Exception):
    pass


class CleanedXYCutSorter:
    def __init__(self, norm_coords: bool = False):
        self.discarded_noise: List = []
        self._norm_coords = norm_coords
        if norm_coords:
            self._v_thresh = _NORM_V_GAP
            self._h_thresh = _NORM_H_GAP
        else:
            self._v_thresh = MIN_GAP_THRESHOLD
            self._h_thresh = MIN_GAP_THRESHOLD
        self._block_counter = 0

    # ── coordinate helpers ──────────────────────────────────────

    def get_coords(self, w) -> Tuple[float, float, float, float]:
        if hasattr(w, "x0") and hasattr(w, "y0") and hasattr(w, "x1") and hasattr(w, "y1"):
            return float(w.x0), float(w.y0), float(w.x1), float(w.y1)
        if hasattr(w, "left") and hasattr(w, "bottom") and hasattr(w, "right") and hasattr(w, "top"):
            return float(w.left), float(w.bottom), float(w.right), float(w.top)
        if isinstance(w, dict):
            if "x0" in w and "y0" in w and "x1" in w and "y1" in w:
                return float(w["x0"]), float(w["y0"]), float(w["x1"]), float(w["y1"])
        raise ValueError(f"Unsupported word type: {type(w)}")

    def compute_neighborhood_thresholds(self, objects: List) -> Tuple[float, float]:
        if not objects:
            fb = _NORM_FALLBACK if self._norm_coords else 5.0
            return fb, fb
        widths, heights = [], []
        for obj in objects:
            left, bottom, right, top = self.get_coords(obj)
            widths.append(right - left)
            heights.append(top - bottom)
        widths.sort()
        heights.sort()
        median_width = widths[len(widths) // 2]
        median_height = heights[len(heights) // 2]
        if self._norm_coords:
            h_thresh = min(_NORM_H_THRESH_CAP, max(_NORM_FALLBACK, median_width * 1.5))
            v_thresh = min(_NORM_V_THRESH_CAP, max(_NORM_FALLBACK, median_height * 1.2))
        else:
            h_thresh = min(30.0, max(5.0, median_width * 1.5))
            v_thresh = min(15.0, max(5.0, median_height * 1.2))
        return h_thresh, v_thresh

    def is_isolated(self, target, all_objects: List, h_thresh: float, v_thresh: float) -> bool:
        t_left, t_bottom, t_right, t_top = self.get_coords(target)
        directions_found = set()
        for other in all_objects:
            if other is target:
                continue
            o_left, o_bottom, o_right, o_top = self.get_coords(other)
            v_overlap = max(t_bottom, o_bottom) < min(t_top, o_top)
            h_overlap = max(t_left, o_left) < min(t_right, o_right)
            if v_overlap and o_right <= t_left and (t_left - o_right) <= h_thresh:
                directions_found.add("left")
            if v_overlap and o_left >= t_right and (o_left - t_right) <= h_thresh:
                directions_found.add("right")
            if h_overlap and o_top <= t_bottom and (t_bottom - o_top) <= v_thresh:
                directions_found.add("down")
            if h_overlap and o_bottom >= t_top and (o_bottom - t_top) <= v_thresh:
                directions_found.add("up")
        return len(directions_found) < 2

    # ── sweep-line cut search ───────────────────────────────────

    @staticmethod
    def _sweep_best_gap(starts: List[float], ends: List[float]) -> Tuple[float, float]:
        """Sweep-line: return (best_gap, best_pos) for clean cuts."""
        events = []
        for x in starts:
            events.append((x, +1))
        for x in ends:
            events.append((x, -1))
        events.sort(key=lambda e: (e[0], e[1]))  # -1 (end) before +1 (start) at same x

        active = 0
        gap_start = None
        best_gap = 0.0
        best_pos = 0.0
        for x, delta in events:
            prev_active = active
            active += delta
            if prev_active == 0 and active > 0 and gap_start is not None:
                gap = x - gap_start
                if gap > best_gap:
                    best_gap = gap
                    best_pos = (gap_start + x) / 2.0
            elif prev_active > 0 and active == 0:
                gap_start = x
        return best_gap, best_pos

    def find_best_cuts(self, objects: List) -> Dict[str, dict]:
        best_v_cut = {"gap": 0.0, "pos": 0.0, "overlappers": []}
        best_h_cut = {"gap": 0.0, "pos": 0.0, "overlappers": []}

        starts = [self.get_coords(o)[0] for o in objects]
        ends   = [self.get_coords(o)[2] for o in objects]
        v_gap, v_pos = self._sweep_best_gap(starts, ends)
        if v_gap > 0:
            best_v_cut = {"gap": v_gap, "pos": v_pos, "overlappers": []}

        bottoms = [self.get_coords(o)[1] for o in objects]
        tops    = [self.get_coords(o)[3] for o in objects]
        h_gap, h_pos = self._sweep_best_gap(bottoms, tops)
        if h_gap > 0:
            best_h_cut = {"gap": h_gap, "pos": h_pos, "overlappers": []}

        if v_gap <= 0 and h_gap <= 0:
            return self._find_best_cuts_fallback(objects)

        return {"vertical": best_v_cut, "horizontal": best_h_cut}

    def _find_best_cuts_fallback(self, objects: List) -> Dict[str, dict]:
        lefts = [self.get_coords(o)[0] for o in objects]
        rights = [self.get_coords(o)[2] for o in objects]
        bottoms = [self.get_coords(o)[1] for o in objects]
        tops = [self.get_coords(o)[3] for o in objects]

        best_v_cut = {"gap": 0.0, "pos": 0.0, "overlappers": []}
        best_h_cut = {"gap": 0.0, "pos": 0.0, "overlappers": []}

        x_candidates = sorted(list(set(lefts + rights)))
        for i in range(len(x_candidates) - 1):
            pos = (x_candidates[i] + x_candidates[i+1]) / 2.0
            overlappers = [o for o in objects
                           if self.get_coords(o)[0] < pos < self.get_coords(o)[2]]
            if len(overlappers) == 0:
                left_objs = [o for o in objects if self.get_coords(o)[2] <= pos]
                right_objs = [o for o in objects if self.get_coords(o)[0] >= pos]
                gap = 0.0
                if left_objs and right_objs:
                    max_left = max(self.get_coords(o)[2] for o in left_objs)
                    min_right = min(self.get_coords(o)[0] for o in right_objs)
                    gap = min_right - max_left
                if gap > best_v_cut["gap"]:
                    best_v_cut = {"gap": gap, "pos": pos, "overlappers": []}
            elif best_v_cut["gap"] == 0.0:
                if not best_v_cut["overlappers"] or len(overlappers) < len(best_v_cut["overlappers"]):
                    best_v_cut = {"gap": 0.0, "pos": pos, "overlappers": overlappers}

        y_candidates = sorted(list(set(bottoms + tops)))
        for i in range(len(y_candidates) - 1):
            pos = (y_candidates[i] + y_candidates[i+1]) / 2.0
            overlappers = [o for o in objects
                           if self.get_coords(o)[1] < pos < self.get_coords(o)[3]]
            if len(overlappers) == 0:
                below_objs = [o for o in objects if self.get_coords(o)[3] <= pos]
                above_objs = [o for o in objects if self.get_coords(o)[1] >= pos]
                gap = 0.0
                if below_objs and above_objs:
                    max_below = max(self.get_coords(o)[3] for o in below_objs)
                    min_above = min(self.get_coords(o)[1] for o in above_objs)
                    gap = min_above - max_below
                if gap > best_h_cut["gap"]:
                    best_h_cut = {"gap": gap, "pos": pos, "overlappers": []}
            elif best_h_cut["gap"] == 0.0:
                if not best_h_cut["overlappers"] or len(overlappers) < len(best_h_cut["overlappers"]):
                    best_h_cut = {"gap": 0.0, "pos": pos, "overlappers": overlappers}

        return {"vertical": best_v_cut, "horizontal": best_h_cut}

    # ── recursive segmentation with block_id ─────────────────────

    def _tag_block(self, objs: List):
        """Assign current _block_counter as block_id to every obj, then increment."""
        bid = self._block_counter
        self._block_counter += 1
        for o in objs:
            if isinstance(o, dict):
                o["block_id"] = bid

    def recursive_segment(self, objects: List) -> List:
        if not objects or len(objects) <= 1:
            self._tag_block(objects)
            return list(objects) if objects else []

        cuts = self.find_best_cuts(objects)
        v_cut = cuts["vertical"]
        h_cut = cuts["horizontal"]

        has_v = v_cut["gap"] >= self._v_thresh
        has_h = h_cut["gap"] >= self._h_thresh

        # --- CASE A: Clean cut exists ---
        if has_v or has_h:
            use_v = False
            if has_v and has_h:
                use_v = v_cut["gap"] > h_cut["gap"]
            elif has_v:
                use_v = True

            if use_v:
                pos = v_cut["pos"]
                left = [o for o in objects if self.get_coords(o)[0] + self.get_coords(o)[2] < 2.0 * pos]
                right = [o for o in objects if o not in left]
                return self.recursive_segment(left) + self.recursive_segment(right)
            else:
                pos = h_cut["pos"]
                above = [o for o in objects if self.get_coords(o)[1] + self.get_coords(o)[3] > 2.0 * pos]
                below = [o for o in objects if o not in above]
                return self.recursive_segment(above) + self.recursive_segment(below)

        # --- CASE B: Both cuts blocked — try bridge isolation ---
        v_bridges = v_cut["overlappers"]
        h_bridges = h_cut["overlappers"]

        target_bridges = []
        if len(v_bridges) > 0 and len(v_bridges) <= MAX_BRIDGE_COUNT:
            target_bridges = v_bridges
        elif len(h_bridges) > 0 and len(h_bridges) <= MAX_BRIDGE_COUNT:
            target_bridges = h_bridges

        if target_bridges:
            h_thresh, v_thresh = self.compute_neighborhood_thresholds(objects)
            if all(self.is_isolated(b, objects, h_thresh, v_thresh) for b in target_bridges):
                for bridge in target_bridges:
                    self.discarded_noise.append(bridge)
                remaining = [o for o in objects if o not in target_bridges]
                return self.recursive_segment(remaining)

        # Cannot segment further — accept this group as a block
        result = sorted(objects, key=lambda w: (-self.get_coords(w)[3], self.get_coords(w)[0]))
        self._tag_block(result)
        return result

    def sort(self, words: List) -> List:
        self.discarded_noise.clear()
        self._block_counter = 0
        return self.recursive_segment(words)


# ========== VERIFICATION & DEMO TEST CASE ==========

def test_demo_isolated_blocker():
    sorter = CleanedXYCutSorter()

    col1_1 = {"x0": 50, "y0": 600, "x1": 270, "y1": 630, "text": "Col1_Line1"}
    col1_2 = {"x0": 50, "y0": 550, "x1": 270, "y1": 580, "text": "Col1_Line2"}
    col2_1 = {"x0": 280, "y0": 570, "x1": 500, "y1": 600, "text": "Col2_Line1"}
    col2_2 = {"x0": 280, "y0": 520, "x1": 500, "y1": 550, "text": "Col2_Line2"}
    noise = {"x0": 265, "y0": 560, "x1": 285, "y1": 575, "text": "StrayLabel"}

    words = [col1_2, col2_1, noise, col1_1, col2_2]

    print("--- Running Sorter with on-demand cleaning ---")
    sorted_words = sorter.sort(words)

    print("\nResulting sorted order:")
    for w in sorted_words:
        print(f"  {w['text']} at bbox ({w['x0']}, {w['y0']}, {w['x1']}, {w['y1']})")

    print("\nDiscarded noise items:")
    for w in sorter.discarded_noise:
        print(f"  {w['text']}")

    assert noise in sorter.discarded_noise, "Error: StrayLabel should be discarded as noise!"
    assert [w["text"] for w in sorted_words] == ["Col1_Line1", "Col1_Line2", "Col2_Line1", "Col2_Line2"]
    print("\nDemo Test Passed successfully!")


if __name__ == "__main__":
    test_demo_isolated_blocker()
