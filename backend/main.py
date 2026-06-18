"""AiCam backend — SAM 2 auto-segmentation over WebSocket."""
import io
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from PIL import Image

_state: dict = {"mask_gen": None, "device": None}


def _pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _load_sam2():
    from sam2.build_sam import build_sam2
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

    device = _pick_device()
    ckpt = Path(__file__).resolve().parent.parent / "checkpoints" / "sam2.1_hiera_tiny.pt"
    cfg = "configs/sam2.1/sam2.1_hiera_t.yaml"
    print(f"[sam2] loading on {device} from {ckpt}")
    model = build_sam2(cfg, str(ckpt), device=device, apply_postprocessing=False)
    mask_gen = SAM2AutomaticMaskGenerator(
        model=model,
        points_per_side=12,
        points_per_batch=64,
        pred_iou_thresh=0.7,
        stability_score_thresh=0.85,
        crop_n_layers=0,
        min_mask_region_area=300,
    )
    _state["mask_gen"] = mask_gen
    _state["device"] = device
    print(f"[sam2] ready ({device})")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_sam2()
    yield


app = FastAPI(title="AiCam", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True, "device": _state.get("device"), "loaded": _state.get("mask_gen") is not None}


_PALETTE = np.array(
    [
        [231, 76, 60], [46, 204, 113], [52, 152, 219], [241, 196, 15],
        [155, 89, 182], [26, 188, 156], [230, 126, 34], [149, 165, 166],
        [192, 57, 43], [39, 174, 96], [41, 128, 185], [243, 156, 18],
        [142, 68, 173], [22, 160, 133], [211, 84, 0], [127, 140, 141],
    ],
    dtype=np.uint8,
)


def _segment_to_overlay(jpeg_bytes: bytes, max_side: int = 320) -> bytes:
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    arr = np.array(img)

    t0 = time.time()
    with torch.inference_mode():
        masks = _state["mask_gen"].generate(arr)
    dt = time.time() - t0

    H, W = arr.shape[:2]
    overlay = np.zeros((H, W, 4), dtype=np.uint8)
    masks = sorted(masks, key=lambda m: -m["area"])
    for i, m in enumerate(masks):
        seg = m["segmentation"]
        color = _PALETTE[i % len(_PALETTE)]
        overlay[seg, 0] = color[0]
        overlay[seg, 1] = color[1]
        overlay[seg, 2] = color[2]
        overlay[seg, 3] = 200

    out = Image.fromarray(overlay, mode="RGBA").resize((w, h), Image.NEAREST)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=False)
    print(f"[seg] {len(masks)} masks in {dt:.2f}s @ {H}x{W}")
    return buf.getvalue()


@app.websocket("/ws/segment")
async def ws_segment(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            data = msg.get("bytes")
            if data is None:
                continue
            try:
                png = _segment_to_overlay(data)
            except Exception as e:
                await ws.send_json({"error": str(e)})
                continue
            await ws.send_bytes(png)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8100)
