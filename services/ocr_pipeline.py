"""Shared OCR pre-processing and layout recovery for screen/file translation.

The OCR engine only recognizes pixels.  This module owns the parts that must be
consistent between screen translation, image direct-output and scanned PDF
direct-output: language selection, small-text upscaling, large-image tiling,
duplicate removal, reading-order recovery and conservative visual-line merging.
"""

from __future__ import annotations

import math
import re
import time


DEFAULT_MIN_CONFIDENCE = 0.35


def _order_quad(points):
    """Return a four-point polygon in TL, TR, BR, BL order."""
    import numpy as np

    values = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)
    sums = values.sum(axis=1)
    differences = np.diff(values, axis=1).reshape(-1)
    ordered[0] = values[int(np.argmin(sums))]
    ordered[2] = values[int(np.argmax(sums))]
    ordered[1] = values[int(np.argmin(differences))]
    ordered[3] = values[int(np.argmax(differences))]
    return ordered


def _transform_points(points, matrix):
    """Apply a 3x3 homography while tolerating degenerate OCR coordinates."""
    import numpy as np

    values = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if matrix is None or values.size == 0:
        return values
    homogeneous = np.column_stack([values, np.ones((len(values),), dtype=np.float64)])
    mapped = homogeneous @ np.asarray(matrix, dtype=np.float64).T
    denominator = mapped[:, 2:3]
    denominator[np.abs(denominator) < 1e-8] = 1.0
    return mapped[:, :2] / denominator


def _document_perspective(gray, cv2):
    """Find a conservative full-page quadrilateral on photographed scans.

    Table borders are deliberately rejected: a candidate must occupy nearly the
    complete image in both dimensions. This makes the enhancement safe for
    engineering drawings and table-heavy PDFs.
    """
    import numpy as np

    height, width = gray.shape[:2]
    longest = max(height, width)
    analysis_scale = min(1.0, 1100.0 / max(1, longest))
    work = gray
    if analysis_scale < 0.999:
        work = cv2.resize(gray, None, fx=analysis_scale, fy=analysis_scale, interpolation=cv2.INTER_AREA)
    blurred = cv2.GaussianBlur(work, (5, 5), 0)
    edges = cv2.Canny(blurred, 45, 140)
    edges = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
        iterations=1,
    )
    contours, _hierarchy = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    work_height, work_width = work.shape[:2]
    work_area = float(max(1, work_width * work_height))
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
        perimeter = cv2.arcLength(contour, True)
        candidate = cv2.approxPolyDP(contour, 0.018 * perimeter, True)
        if len(candidate) != 4 or not cv2.isContourConvex(candidate):
            continue
        ordered = _order_quad(candidate.reshape(4, 2))
        xs, ys = ordered[:, 0], ordered[:, 1]
        span_x = float(xs.max() - xs.min())
        span_y = float(ys.max() - ys.min())
        area_ratio = abs(float(cv2.contourArea(ordered))) / work_area
        if span_x < work_width * 0.84 or span_y < work_height * 0.84 or area_ratio < 0.68:
            continue
        source = ordered / max(analysis_scale, 1e-8)
        destination = np.asarray(
            [[0.0, 0.0], [width - 1.0, 0.0], [width - 1.0, height - 1.0], [0.0, height - 1.0]],
            dtype=np.float32,
        )
        diagonal = max(1.0, math.hypot(width, height))
        displacement = float(np.mean(np.linalg.norm(source - destination, axis=1))) / diagonal
        # Tiny adjustments only add interpolation blur; extreme ones are more
        # likely to be a false-positive inner frame than the physical page.
        if displacement < 0.006 or displacement > 0.16:
            return None, 0.0
        return cv2.getPerspectiveTransform(source.astype(np.float32), destination), displacement
    return None, 0.0


def _small_skew_angle(gray, cv2):
    """Estimate a reliable small text-line rotation using long Hough segments."""
    import numpy as np

    height, width = gray.shape[:2]
    analysis_scale = min(1.0, 1000.0 / max(1, height, width))
    work = gray
    if analysis_scale < 0.999:
        work = cv2.resize(gray, None, fx=analysis_scale, fy=analysis_scale, interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(cv2.GaussianBlur(work, (3, 3), 0), 55, 155)
    minimum_length = max(60, int(work.shape[1] * 0.16))
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 360.0,
        threshold=max(35, int(work.shape[1] * 0.045)),
        minLineLength=minimum_length,
        maxLineGap=max(12, int(work.shape[1] * 0.025)),
    )
    if lines is None:
        return 0.0, 0
    angles = []
    weights = []
    for raw in lines[:500]:
        values = np.asarray(raw).reshape(-1)
        if len(values) < 4:
            continue
        x1, y1, x2, y2 = [float(value) for value in values[:4]]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < minimum_length:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        while angle <= -90:
            angle += 180
        while angle > 90:
            angle -= 180
        if abs(angle) <= 12.0:
            angles.append(angle)
            weights.append(length)
    if len(angles) < 4:
        return 0.0, len(angles)
    values = np.asarray(angles, dtype=np.float32)
    median = float(np.median(values))
    deviation = float(np.median(np.abs(values - median)))
    # PP-OCR already tolerates small rotations. Resampling a crisp page for a
    # cosmetic correction of a few degrees can lower recognition quality, so
    # geometry is changed only when skew is materially large.
    if deviation > 1.15 or abs(median) < 5.0 or abs(median) > 12.0:
        return 0.0, len(angles)
    return median, len(angles)


def adaptive_document_preprocess(image):
    """Quickly improve scan pixels and return an exact coordinate inverse.

    Geometry fixes are applied only when confidence guards pass. Photometric
    enhancement is adaptive, stays in the luminance channel, and preserves the
    original canvas dimensions so translated overlays keep their source scale.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return image, None, {"enabled": False, "reason": "opencv_unavailable"}

    started = time.perf_counter()
    source = np.ascontiguousarray(image)
    if source.ndim == 2:
        source = cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
    elif source.shape[2] == 4:
        source = cv2.cvtColor(source, cv2.COLOR_BGRA2BGR)
    else:
        source = source[:, :, :3].copy()
    height, width = source.shape[:2]
    forward = np.eye(3, dtype=np.float64)
    diagnostics = {
        "enabled": True,
        "perspective": False,
        "perspective_displacement": 0.0,
        "deskew_angle": 0.0,
        "deskew_evidence": 0,
        "denoise": "none",
        "clahe": False,
        "sharpen": False,
    }
    geometry_changed = False

    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    perspective, displacement = _document_perspective(gray, cv2)
    if perspective is not None:
        source = cv2.warpPerspective(
            source,
            perspective,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        forward = np.asarray(perspective, dtype=np.float64) @ forward
        diagnostics["perspective"] = True
        diagnostics["perspective_displacement"] = round(float(displacement), 4)
        geometry_changed = True
        gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)

    skew_angle, evidence = _small_skew_angle(gray, cv2)
    diagnostics["deskew_evidence"] = int(evidence)
    if skew_angle:
        rotation = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), skew_angle, 1.0)
        source = cv2.warpAffine(
            source,
            rotation,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        rotation3 = np.vstack([rotation, [0.0, 0.0, 1.0]])
        forward = rotation3 @ forward
        diagnostics["deskew_angle"] = round(float(skew_angle), 3)
        geometry_changed = True

    lab = cv2.cvtColor(source, cv2.COLOR_BGR2LAB)
    luminance, channel_a, channel_b = cv2.split(lab)
    # Both metrics use a cheap quarter-resolution sample. They decide whether
    # the more expensive full-size filters are justified for this page.
    sample_scale = min(1.0, 700.0 / max(1, height, width))
    sample = luminance
    if sample_scale < 0.999:
        sample = cv2.resize(luminance, None, fx=sample_scale, fy=sample_scale, interpolation=cv2.INTER_AREA)
    smooth_sample = cv2.GaussianBlur(sample, (3, 3), 0)
    noise = float(np.median(np.abs(sample.astype(np.float32) - smooth_sample.astype(np.float32))))
    p05, p95 = [float(value) for value in np.percentile(sample, [5, 95])]
    contrast = p95 - p05
    laplacian_variance = float(cv2.Laplacian(sample, cv2.CV_32F).var())
    diagnostics.update({
        "noise": round(noise, 2),
        "contrast": round(contrast, 1),
        "sharpness": round(laplacian_variance, 1),
    })

    if noise >= 6.0:
        luminance = cv2.bilateralFilter(luminance, 5, 24, 24)
        diagnostics["denoise"] = "bilateral"
    elif noise >= 2.5:
        luminance = cv2.medianBlur(luminance, 3)
        diagnostics["denoise"] = "median"
    if contrast < 145.0 and (noise >= 2.0 or laplacian_variance < 1800.0):
        luminance = cv2.createCLAHE(clipLimit=1.55, tileGridSize=(8, 8)).apply(luminance)
        diagnostics["clahe"] = True
    # Sharpen blurred scans, but avoid magnifying noise on already crisp pages.
    if laplacian_variance < 900.0 or diagnostics["denoise"] != "none":
        blurred = cv2.GaussianBlur(luminance, (0, 0), 0.8)
        amount = 0.62 if laplacian_variance < 450.0 else 0.38
        luminance = cv2.addWeighted(luminance, 1.0 + amount, blurred, -amount, 0)
        diagnostics["sharpen"] = True
        diagnostics["sharpen_amount"] = amount

    photometric_changed = bool(
        diagnostics["denoise"] != "none" or diagnostics["clahe"] or diagnostics["sharpen"]
    )
    diagnostics["changed"] = bool(geometry_changed or photometric_changed)
    enhanced = (
        cv2.cvtColor(cv2.merge([luminance, channel_a, channel_b]), cv2.COLOR_LAB2BGR)
        if photometric_changed else source
    )
    try:
        inverse = np.linalg.inv(forward)
    except np.linalg.LinAlgError:
        inverse = np.eye(3, dtype=np.float64)
    diagnostics["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return enhanced, inverse, diagnostics


def ocr_upscale_factor(image, target_longest=1600.0, maximum=3.0):
    """Upscale small inputs without ever downscaling them before OCR."""
    try:
        height, width = image.shape[:2]
    except Exception:
        return 1.0
    longest = max(int(height), int(width))
    if longest <= 0:
        return 1.0
    return round(max(1.0, min(float(target_longest) / longest, float(maximum))), 3)


def _tile_positions(length, tile_size, overlap):
    length = max(0, int(length))
    tile_size = max(64, int(tile_size))
    if length <= tile_size:
        return [0]
    step = max(32, tile_size - max(0, int(overlap)))
    positions = list(range(0, max(1, length - tile_size + 1), step))
    last = max(0, length - tile_size)
    if not positions or positions[-1] != last:
        positions.append(last)
    return positions


def _rect_from_polygon(polygon):
    xs = [float(point[0]) for point in polygon]
    ys = [float(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _rect_iou(a, b):
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return intersection / max(1.0, area_a + area_b - intersection)


def _normalized_text(text):
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _deduplicate_regions(regions):
    """Remove tile-overlap duplicates while keeping the most reliable geometry."""
    kept = []
    for candidate in sorted(regions, key=lambda item: (-float(item["confidence"]), item["top"], item["left"])):
        duplicate_index = None
        candidate_text = _normalized_text(candidate.get("text"))
        candidate_rect = candidate["bbox"]
        for index, existing in enumerate(kept):
            iou = _rect_iou(candidate_rect, existing["bbox"])
            if iou < 0.38:
                continue
            existing_text = _normalized_text(existing.get("text"))
            same_text = candidate_text == existing_text
            contains_text = bool(candidate_text and existing_text and (
                candidate_text in existing_text or existing_text in candidate_text
            ))
            if same_text or (contains_text and iou >= 0.52) or iou >= 0.78:
                duplicate_index = index
                break
        if duplicate_index is None:
            kept.append(candidate)
            continue
        existing = kept[duplicate_index]
        # Prefer a longer recognition when confidence is effectively tied.
        if (
            len(str(candidate.get("text") or "")) > len(str(existing.get("text") or ""))
            and float(candidate["confidence"]) + 0.03 >= float(existing["confidence"])
        ):
            kept[duplicate_index] = candidate
    return kept


def _vertical_overlap(a, b):
    top = max(a["top"], b["top"])
    bottom = min(a["top"] + a["height"], b["top"] + b["height"])
    return max(0.0, bottom - top)


def _ocr_vertical_rule_mask(image, regions):
    """Locate narrow table dividers in source coordinates before row merging."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    if not regions:
        return None
    gray = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    heights = [max(1.0, float(region["height"])) for region in regions]
    height = float(np.median(heights))
    ink = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)[1]
    rules = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(24, int(height * 2)))),
        borderType=cv2.BORDER_CONSTANT, borderValue=0,
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(rules, 8)
    keep = np.zeros(count, dtype=np.uint8)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_WIDTH] <= max(4, int(height * 0.5)):
            keep[label] = 255
    return keep[labels]


def merge_dense_ocr_regions(regions, vertical_rules=None):
    """Recover visual rows and merge only immediately adjacent OCR fragments."""
    if not regions:
        return []
    rows = []
    for region in sorted(regions, key=lambda item: (item["top"], item["left"])):
        region_top = region["top"]
        region_bottom = region["top"] + region["height"]
        target = None
        for row in rows:
            intersection = max(0.0, min(row["bottom"], region_bottom) - max(row["top"], region_top))
            if intersection >= 0.5 * max(1.0, min(row["bottom"] - row["top"], region["height"])):
                target = row
                break
        if target is None:
            rows.append({"top": region_top, "bottom": region_bottom, "items": [region]})
        else:
            target["items"].append(region)
            target["top"] = min(target["top"], region_top)
            target["bottom"] = max(target["bottom"], region_bottom)

    merged = []
    for row in sorted(rows, key=lambda item: item["top"]):
        current = None
        for item in sorted(row["items"], key=lambda value: value["left"]):
            if current is None:
                current = dict(item)
                continue
            average_height = max(1.0, (current["height"] + item["height"]) / 2.0)
            same_line = _vertical_overlap(current, item) >= average_height * 0.45
            gap = item["left"] - (current["left"] + current["width"])
            dense_gap = -2 <= gap <= max(10.0, average_height * 0.9)
            both_short = len(current.get("text") or "") <= 28 and len(item.get("text") or "") <= 28
            separated = False
            if vertical_rules is not None and same_line and dense_gap:
                left = max(0, int(current["left"] + current["width"]) - 2)
                right = min(vertical_rules.shape[1], int(math.ceil(item["left"])) + 3)
                top = max(0, int(max(current["top"], item["top"])))
                bottom = min(vertical_rules.shape[0], int(math.ceil(min(
                    current["top"] + current["height"], item["top"] + item["height"],
                ))))
                strip = vertical_rules[top:bottom, left:right]
                if strip.size:
                    separated = bool((strip > 0).sum(axis=0).max() >= max(3, (bottom - top) * 0.5))
            if same_line and dense_gap and not separated and (both_short or gap <= 4):
                right = max(current["left"] + current["width"], item["left"] + item["width"])
                bottom = max(current["top"] + current["height"], item["top"] + item["height"])
                current["text"] = f"{current.get('text', '').rstrip()} {item.get('text', '').lstrip()}".strip()
                current["confidence"] = min(float(current["confidence"]), float(item["confidence"]))
                current["top"] = min(current["top"], item["top"])
                current["left"] = min(current["left"], item["left"])
                current["width"] = max(1.0, right - current["left"])
                current["height"] = max(1.0, bottom - current["top"])
                current["bbox"] = [
                    current["left"], current["top"],
                    current["left"] + current["width"], current["top"] + current["height"],
                ]
                current["polygon"] = [
                    [current["bbox"][0], current["bbox"][1]],
                    [current["bbox"][2], current["bbox"][1]],
                    [current["bbox"][2], current["bbox"][3]],
                    [current["bbox"][0], current["bbox"][3]],
                ]
                current["merged"] = True
            else:
                merged.append(current)
                current = dict(item)
        if current is not None:
            merged.append(current)
    return merged


def recognize_ocr_regions(
    image,
    ocr_callback,
    *,
    min_confidence=DEFAULT_MIN_CONFIDENCE,
    merge_dense=True,
    tile_large=True,
    target_longest=1600,
    tile_size=1800,
    tile_overlap=220,
    max_tiles=256,
    fit_longest=False,
    preprocess_document=False,
    preprocess_diagnostics=None,
):
    """Recognize an image and return normalized, source-coordinate regions.

    Large images are processed in overlapping tiles so small labels are not lost
    when an OCR detector internally downsizes the complete image.
    """
    try:
        import cv2
    except Exception:  # Pillow fallback keeps custom/test builds operational.
        cv2 = None

    try:
        height, width = image.shape[:2]
    except Exception as exc:
        raise ValueError("OCR 输入不是有效图像") from exc
    if height <= 0 or width <= 0:
        return []

    use_tiles = bool(tile_large and (max(width, height) > 2600 or width * height > 8_000_000))
    jobs = []
    if use_tiles:
        current_tile_size = max(512, int(tile_size))
        while True:
            xs = _tile_positions(width, current_tile_size, tile_overlap)
            ys = _tile_positions(height, current_tile_size, tile_overlap)
            if len(xs) * len(ys) <= max_tiles or current_tile_size >= max(width, height):
                break
            current_tile_size = min(max(width, height), int(current_tile_size * 1.35))
        for top in ys:
            for left in xs:
                jobs.append((left, top, image[top:min(height, top + current_tile_size), left:min(width, left + current_tile_size)]))
    else:
        jobs.append((0, 0, image))

    regions = []
    all_preprocess_diagnostics = []
    for tile_index, (offset_x, offset_y, tile) in enumerate(jobs):
        if fit_longest:
            tile_longest = max(1, int(max(tile.shape[:2])))
            scale = round(max(0.08, min(3.0, float(target_longest) / tile_longest)), 4)
        else:
            scale = ocr_upscale_factor(tile, target_longest=target_longest)
        image_for_ocr = tile
        if abs(scale - 1.0) > 0.001:
            if cv2 is not None:
                interpolation = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
                image_for_ocr = cv2.resize(tile, None, fx=scale, fy=scale, interpolation=interpolation)
            else:
                import numpy as np
                from PIL import Image

                rgb = np.ascontiguousarray(tile[:, :, :3][:, :, ::-1])
                resized = Image.fromarray(rgb, "RGB").resize(
                    (max(1, int(round(tile.shape[1] * scale))), max(1, int(round(tile.shape[0] * scale)))),
                    Image.Resampling.BICUBIC if scale > 1.0 else Image.Resampling.LANCZOS,
                )
                image_for_ocr = np.asarray(resized, dtype=np.uint8)[:, :, ::-1].copy()
        coordinate_inverse = None
        if preprocess_document:
            image_for_ocr, coordinate_inverse, current_diagnostics = adaptive_document_preprocess(image_for_ocr)
            current_diagnostics = dict(current_diagnostics or {})
            current_diagnostics["tile_index"] = tile_index if use_tiles else None
            all_preprocess_diagnostics.append(current_diagnostics)
        inverse_scale = 1.0 / scale if scale else 1.0
        raw_results = ocr_callback(image_for_ocr) if ocr_callback else []
        for raw_index, item in enumerate(raw_results or []):
            if not item or len(item) < 2:
                continue
            polygon = item[0]
            text = str(item[1] or "").strip()
            try:
                confidence = float(item[2]) if len(item) > 2 and item[2] is not None else 1.0
            except (TypeError, ValueError):
                confidence = 1.0
            if not text or confidence < float(min_confidence) or polygon is None:
                continue
            try:
                if len(polygon) == 0:
                    continue
            except TypeError:
                continue
            mapped_polygon = _transform_points(polygon, coordinate_inverse)
            normalized_polygon = []
            for point in mapped_polygon:
                x = min(float(width), max(0.0, offset_x + float(point[0]) * inverse_scale))
                y = min(float(height), max(0.0, offset_y + float(point[1]) * inverse_scale))
                normalized_polygon.append([x, y])
            if len(normalized_polygon) < 4:
                continue
            left, top, right, bottom = _rect_from_polygon(normalized_polygon)
            if right - left < 1 or bottom - top < 1:
                continue
            angle = math.degrees(math.atan2(
                normalized_polygon[1][1] - normalized_polygon[0][1],
                normalized_polygon[1][0] - normalized_polygon[0][0],
            ))
            regions.append({
                "text": text,
                "confidence": confidence,
                "polygon": normalized_polygon,
                "bbox": [left, top, right, bottom],
                "left": left,
                "top": top,
                "width": right - left,
                "height": bottom - top,
                "angle": angle,
                "tile_index": tile_index if use_tiles else None,
                "raw_index": raw_index,
                "tiled": use_tiles,
                "merged": False,
            })

    regions = _deduplicate_regions(regions)
    if merge_dense:
        rules = _ocr_vertical_rule_mask(image, regions) if preprocess_document else None
        regions = merge_dense_ocr_regions(regions, vertical_rules=rules)
    regions.sort(key=lambda item: (round(item["top"] / max(1.0, item["height"] * 0.65)), item["left"], item["top"]))
    for index, region in enumerate(regions):
        region["order"] = index
    if isinstance(preprocess_diagnostics, dict):
        preprocess_diagnostics.clear()
        if len(all_preprocess_diagnostics) == 1:
            preprocess_diagnostics.update(all_preprocess_diagnostics[0])
        else:
            preprocess_diagnostics.update({
                "enabled": bool(preprocess_document),
                "tiles": all_preprocess_diagnostics,
                "elapsed_ms": round(sum(float(item.get("elapsed_ms") or 0) for item in all_preprocess_diagnostics), 1),
            })
    return regions


def add_neighbor_context(regions, prefix):
    """Attach local reading context without making file data executable."""
    output = []
    for index, region in enumerate(regions or []):
        previous_text = str(regions[index - 1].get("text") or "") if index else ""
        next_text = str(regions[index + 1].get("text") or "") if index + 1 < len(regions) else ""
        context_parts = [str(prefix or "OCR 文字区域")]
        if previous_text:
            context_parts.append(f"上一相邻片段：{previous_text[:160]}")
        if next_text:
            context_parts.append(f"下一相邻片段：{next_text[:160]}")
        enriched = dict(region)
        enriched["context"] = "；".join(context_parts)
        output.append(enriched)
    return output
