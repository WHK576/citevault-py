"""读取 citations.json / citations.yml 清单。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class ManifestError(Exception):
    """清单格式错误。"""


def _load_raw(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"JSON 解析失败: {exc}") from exc
    if suffix in {".yml", ".yaml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ManifestError(
                "读取 YAML 需要安装 PyYAML：pip install citevault[yaml]"
            ) from exc
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ManifestError(f"YAML 解析失败: {exc}") from exc
    raise ManifestError("清单只支持 .json / .yml / .yaml")


def load_manifest(path: str) -> List[Dict[str, str]]:
    """读取清单并做最小校验。支持顶层列表或 {citations: [...]}。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise ManifestError(f"清单不存在: {path}")
    data = _load_raw(file_path)
    entries = data.get("citations") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ManifestError("清单应为列表，或包含 citations 列表的对象")

    result: List[Dict[str, str]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ManifestError(f"第 {index} 条不是对象")
        claim = entry.get("claim")
        quote = entry.get("quote")
        url = entry.get("url")
        file = entry.get("file")
        if not isinstance(claim, str) or not claim.strip():
            raise ManifestError(f"第 {index} 条缺少 claim")
        if not isinstance(quote, str) or not quote.strip():
            raise ManifestError(f"第 {index} 条缺少 quote")
        if not url and not file:
            raise ManifestError(f"第 {index} 条缺少 url 或 file")
        result.append({
            "claim": claim.strip(),
            "quote": quote.strip(),
            "url": url.strip() if isinstance(url, str) else "",
            "file": file.strip() if isinstance(file, str) else "",
            "note": str(entry.get("note") or "").strip(),
        })
    return result
