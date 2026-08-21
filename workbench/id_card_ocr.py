from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from .id_card import extract_id_card_fields, merge_id_card_fields
from .id_card_translate import classify_id_card_side, extract_portrait_bbox

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PIPELINE: Any = None


def _load_pipeline() -> Any:
    global _PIPELINE
    with _LOCK:
        if _PIPELINE is None:
            try:
                from paddleocr import PaddleOCRVL
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 paddleocr。请在运行 vat-bench 的环境中安装，"
                    "或使用已能运行 demo.py 的 Python 环境启动工作台。"
                ) from exc
            logger.info("正在初始化 PaddleOCRVL，首次加载可能较慢")
            _PIPELINE = PaddleOCRVL()
        return _PIPELINE


def _as_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    json_value = getattr(result, "json", None)
    if isinstance(json_value, dict):
        inner = json_value.get("res")
        return inner if isinstance(inner, dict) else json_value
    if hasattr(result, "keys") and "parsing_res_list" in result:
        blocks = []
        for item in result["parsing_res_list"]:
            if isinstance(item, dict):
                blocks.append(item)
            else:
                bbox = getattr(item, "bbox", None)
                if bbox is None:
                    bbox = getattr(item, "block_bbox", None)
                blocks.append(
                    {
                        "block_label": getattr(item, "label", "text"),
                        "block_content": getattr(item, "content", ""),
                        "block_bbox": list(bbox) if bbox is not None else [],
                    }
                )
        payload = {"parsing_res_list": blocks}
        if "width" in result:
            payload["width"] = result["width"]
        if "height" in result:
            payload["height"] = result["height"]
        return payload
    raise TypeError(f"无法解析 PaddleOCRVL 结果：{type(result)!r}")


def recognize_id_card_image(path: str | Path) -> dict[str, str]:
    """对一张证件图跑 PaddleOCRVL 并抽出字段。"""
    return recognize_id_card_pack([path])["fields"]


def recognize_id_card_pack(paths: list[str | Path]) -> dict[str, Any]:
    """识别一组正反面图片：合并字段，并记录每张图的面别与人像框。"""
    sides: list[dict[str, Any]] = []
    for path in paths:
        image = Path(path)
        if not image.is_file():
            raise FileNotFoundError(f"证件图片不存在：{image}")
        output = _load_pipeline().predict(str(image))
        batches: list[dict[str, str]] = []
        portrait = None
        for item in output or []:
            payload = _as_payload(item)
            batches.append(extract_id_card_fields(payload))
            portrait = extract_portrait_bbox(payload) or portrait
        fields = merge_id_card_fields(*batches) if batches else {}
        sides.append(
            {
                "path": str(image.resolve()),
                "fields": fields,
                "side": classify_id_card_side(fields),
                "portrait_bbox": portrait,
            }
        )
    return {
        "fields": merge_id_card_fields(*(item["fields"] for item in sides)),
        "sides": sides,
    }


def recognize_id_card_images(paths: list[str | Path]) -> dict[str, str]:
    return recognize_id_card_pack(paths)["fields"]
