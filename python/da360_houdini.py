"""
da360_houdini.py – HDRI -> DA360-Tiefe -> Punktwolke (.ply) für das Houdini-Light-Tool

Wird vom HDA julian::light_creator::11.0 (Button "Tiefe berechnen") als eigener Prozess gestartet,
läuft aber auch eigenständig:

    python3 da360_houdini.py --hdri studio.hdr --da360 /path/to/DA360 \
        --weights /path/to/DA360_large.pth --camera-height 1.477 --out out.ply

Ablauf (entspricht hdri_infer.py --tonemap reinhard_lamps + da360_infer.py + hdri_pointcloud.py):
  1. HDRI linear lesen, flächengemittelt auf Modellauflösung (1036x518) bringen
  2. Auto-Exposure, Lampen-Kompression, Reinhard (white 4), sRGB, ImageNet-Normierung
  3. DA360 (relative Tiefe = 1 / Disparität), GPU wenn vorhanden (MPS / CUDA)
  4. Punktwolke in der Projektions-Konvention des Tools (Y-up, uvproject "polar", urange 1->0):
         u = (Spalte + 0.5) / W,  v = 1 - (Zeile + 0.5) / H
         az = (u - 0.5) * 2pi,  el = (v - 0.5) * pi
         dir = (-sin(az) cos(el), sin(el), cos(az) cos(el))
  5. Skala über den Boden: häufigste Höhe der unteren Punkte = -Kamerahöhe
  6. PLY (binary): x y z float, red green blue float = lineare HDR-Farbe

Benötigt: torch, numpy, opencv-python (pyexr für .exr) und den DA360-Code (Ordner mit networks/, checkpoint.py).
"""

import argparse
import os
import sys
import time

import cv2
import numpy as np

LUM_W = np.array([0.2126, 0.7152, 0.0722], np.float32)


def log(msg):
    print(msg, flush=True)


def read_hdri_linear(path):
    if os.path.splitext(path)[1].lower() == ".exr":
        import pyexr
        rgb = np.asarray(pyexr.read(path), np.float32)
        if rgb.ndim == 2:
            rgb = np.repeat(rgb[..., None], 3, -1)
        rgb = rgb[..., :3]
    else:
        bgr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if bgr is None:
            raise FileNotFoundError(path)
        if bgr.ndim == 2:
            bgr = np.repeat(bgr[..., None], 3, -1)
        rgb = cv2.cvtColor(bgr[..., :3], cv2.COLOR_BGR2RGB).astype(np.float32)
        if bgr.dtype == np.uint8:
            rgb /= 255.0
        elif bgr.dtype == np.uint16:
            rgb /= 65535.0
    return np.maximum(np.nan_to_num(rgb, nan=0.0, posinf=0.0, neginf=0.0), 0.0)


def tonemap_lamps(rgb, knee=1.0, lamp_lo=1.2, lamp_hi=4.0, lamp_thr=1000.0, white=4.0):
    """Wie hdri_infer.tonemap(..., 'reinhard_lamps'): Lampen stauchen statt ausbrennen lassen."""
    lum = rgb @ LUM_W
    x = rgb * (0.18 / max(float(np.exp(np.mean(np.log(lum + 1e-6)))), 1e-6))
    lum = x @ LUM_W
    lamps = lum > lamp_thr
    if lamps.any():
        l1 = max(float(np.percentile(lum[lamps], 1)), knee * 1.01)
        l2 = max(float(np.percentile(lum[lamps], 99.5)), l1 * 1.01)
        out = lum.copy()
        m1 = (lum > knee) & (lum <= l1)
        out[m1] = knee + np.log(lum[m1] / knee) / np.log(l1 / knee) * (lamp_lo - knee)
        m2 = lum > l1
        out[m2] = lamp_lo + np.clip(np.log(lum[m2] / l1) / np.log(l2 / l1), 0, 1) * (lamp_hi - lamp_lo)
        x = x * (out / np.maximum(lum, 1e-9))[..., None]
    l = x @ LUM_W
    lt = l * (1.0 + l / (white * white)) / (1.0 + l)
    y = np.clip(x * (lt / np.maximum(l, 1e-6))[..., None], 0.0, 1.0)
    return np.where(y <= 0.0031308, 12.92 * y, 1.055 * np.power(y, 1.0 / 2.4) - 0.055).astype(np.float32)


def run_da360(img01, da360_root, weights, device_name):
    import torch
    sys.path.insert(0, da360_root)
    import networks
    from checkpoint import checkpoint_meta, load_checkpoint, state_dict_from_checkpoint

    if device_name == "auto":
        device_name = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    device = torch.device(device_name)
    ckpt = load_checkpoint(weights, map_location="cpu")
    meta = checkpoint_meta(ckpt)
    net = getattr(networks, meta["net"])(meta["height"], meta["width"], dinov2_encoder=meta["dinov2_encoder"])
    net.load_state_dict(state_dict_from_checkpoint(ckpt, net.state_dict()), strict=False)
    net.to(device).eval()
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    t = torch.from_numpy(((img01 - mean) / std).transpose(2, 0, 1)[None].copy()).to(device)
    with torch.no_grad():
        disp = net(t)["pred_disp"].float().cpu().numpy()[0, 0]
    return 1.0 / np.maximum(disp, 1e-6), device_name


def tool_directions(h, w):
    """Richtung je Pixel in der Konvention des Tools (uvproject polar, urange 1->0, Y-up)."""
    u = (np.arange(w, dtype=np.float32) + 0.5) / w
    v = 1.0 - (np.arange(h, dtype=np.float32) + 0.5) / h
    u, v = np.meshgrid(u, v)
    az = (u - 0.5) * 2.0 * np.pi
    el = (v - 0.5) * np.pi
    return np.stack([-np.sin(az) * np.cos(el), np.sin(el), np.cos(az) * np.cos(el)], -1)


def floor_height(points):
    """Häufigste Höhe der unteren Punkte (skalenunabhängig über den Median normiert)."""
    y = points[..., 1].ravel()
    r = np.linalg.norm(points.reshape(-1, 3), axis=1)
    norm = 3.0 / float(np.median(r))
    yl = y[y * norm < -0.3] * norm
    hist, edges = np.histogram(yl, bins=400)
    i = int(np.argmax(hist))
    return 0.5 * (edges[i] + edges[i + 1]) / norm


def write_ply(path, pts, cols, comment):
    v = np.empty(len(pts), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                  ("red", "<f4"), ("green", "<f4"), ("blue", "<f4")])
    v["x"], v["y"], v["z"] = pts.T
    v["red"], v["green"], v["blue"] = cols.T
    head = ("ply\nformat binary_little_endian 1.0\n" f"comment {comment}\n" f"element vertex {len(v)}\n"
            "property float x\nproperty float y\nproperty float z\n"
            "property float red\nproperty float green\nproperty float blue\nend_header\n")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(head.encode("ascii"))
        f.write(v.tobytes())
    os.replace(tmp, path)          # erst nach vollständigem Schreiben sichtbar


def main():
    p = argparse.ArgumentParser(description="HDRI -> DA360 -> Punktwolke für das Houdini-Light-Tool")
    p.add_argument("--hdri", required=True)
    p.add_argument("--da360", required=True, help="DA360-Code-Ordner (enthält networks/ und checkpoint.py)")
    p.add_argument("--weights", required=True, help="DA360-Gewichte (.pth), z. B. DA360_large.pth")
    p.add_argument("--out", required=True, help="Ausgabe .ply")
    p.add_argument("--camera-height", type=float, default=1.477, help="Kamerahöhe über dem Boden in m")
    p.add_argument("--device", default="auto", choices=["auto", "mps", "cuda", "cpu"])
    args = p.parse_args()

    t0 = time.time()
    rgb = read_hdri_linear(args.hdri)
    log(f"HDRI {rgb.shape[1]}x{rgb.shape[0]} gelesen")
    w, h = 1036, 518                                   # DA360-Modellauflösung
    small = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
    depth, dev = run_da360(tonemap_lamps(small), args.da360, args.weights, args.device)
    log(f"DA360 auf {dev}: {time.time() - t0:.1f} s")

    pts = depth[..., None] * tool_directions(h, w)
    fy = floor_height(pts)
    scale = args.camera_height / -fy
    pts = (pts * scale).reshape(-1, 3).astype(np.float32)
    log(f"Boden bei {fy:.4f} -> Skala {scale:.2f} (Kamerahöhe {args.camera_height} m)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    write_ply(args.out, pts, small.reshape(-1, 3),
              f"DA360 depth, tool polar convention (Y-up), floor = -{args.camera_height} m, Cd = linear HDR, "
              f"erp {w}x{h}, source {os.path.basename(args.hdri)}")
    log(f"Fertig: {len(pts)} Punkte -> {args.out}  ({time.time() - t0:.1f} s gesamt)")


if __name__ == "__main__":
    main()
