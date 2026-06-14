from __future__ import annotations

import json
import shutil
import tarfile
import urllib.parse
import urllib.request
import zipfile
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TextColumn,
)

from log_utils import setup_timestamped_print, log_info

setup_timestamped_print()

_console = Console(file=sys.__stdout__, force_terminal=True)


VOSK_MODEL_LIST_URL = "https://alphacephei.com/vosk/models/model-list.json"


def _progress_read(resp, filename: str, out_file=None) -> bytes:
    total = resp.headers.get("Content-Length")
    total = int(total) if total and total.isdigit() else None
    chunk_size = 64 * 1024

    progress = Progress(
        TextColumn("{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=None),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=_console,
    )

    data_parts: list[bytes] | None = [] if out_file is None else None
    with progress:
        task_id = progress.add_task("download", filename=filename, total=total)
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            if out_file is None:
                data_parts.append(chunk)
            else:
                out_file.write(chunk)
            progress.update(task_id, advance=len(chunk))

    if data_parts is None:
        return b""
    return b"".join(data_parts)


def _http_get_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "DragonbornVoiceControl/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        filename = Path(urllib.parse.urlparse(url).path).name or "download.json"
        _console.print(f"[dim]URL:[/] {url}")
        data = _progress_read(resp, filename)
    return json.loads(data.decode("utf-8", errors="replace"))


def _download_file(url: str, dest: Path, timeout: float = 60.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "DragonbornVoiceControl/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        filename = Path(urllib.parse.urlparse(url).path).name or dest.name
        _console.print(f"[dim]URL:[/] {url}")
        _progress_read(resp, filename, out_file=f)


def _extract_archive(archive_path: Path, extract_to: Path) -> None:
    extract_to.mkdir(parents=True, exist_ok=True)
    ap = str(archive_path).lower()
    if ap.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_to)
        return
    if ap.endswith(".tar.gz") or ap.endswith(".tgz") or ap.endswith(".tar"):
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(extract_to)
        return
    raise ValueError(f"Unsupported archive format: {archive_path.name}")


def _dict_mapping_to_vosk_candidates(model_map: dict) -> list[dict] | None:
    vals = list(model_map.values())
    if (not vals) or (not all(isinstance(v, dict) for v in vals)):
        return None

    candidates: list[dict] = []
    for key, value in model_map.items():
        entry = dict(value)
        if not entry.get("name"):
            entry["name"] = key
        candidates.append(entry)
    return candidates


def _extract_vosk_candidates(model_list: object) -> list:
    if isinstance(model_list, list):
        return model_list

    if isinstance(model_list, dict):
        models = model_list.get("models")
        if isinstance(models, list):
            return models
        mapped = _dict_mapping_to_vosk_candidates(model_list)
        if mapped is not None:
            return mapped

    raise RuntimeError(
        f"Unexpected Vosk model list format from {VOSK_MODEL_LIST_URL}: "
        f"expected a list or dict with 'models' or a mapping of name->entry (got {type(model_list).__name__})"
    )


def _format_available_vosk_names(candidates: list) -> str:
    names = [str(m.get("name", "")) for m in candidates if isinstance(m, dict) and m.get("name")]
    sample = ", ".join(names[:20])
    if len(names) > 20:
        sample += f", ...(+{len(names) - 20} more)"
    return sample or "(none)"


def _find_vosk_entry(model_list: object, model_name: str) -> dict:
    candidates = _extract_vosk_candidates(model_list)

    for m in candidates:
        if isinstance(m, dict) and str(m.get("name", "")).strip() == model_name:
            return m

    raise ValueError(
        f"Unknown Vosk model '{model_name}'. Available: {_format_available_vosk_names(candidates)}; see {VOSK_MODEL_LIST_URL}"
    )


def _pick_extracted_model_dir(extract_root: Path, model_name: str) -> Path:
    candidates = [p for p in extract_root.iterdir() if p.is_dir()]
    if not candidates:
        raise RuntimeError("Archive extracted but no folder found")
    for c in candidates:
        if c.name == model_name:
            return c
    return candidates[0]


def ensure_vosk_model(model_name: str, cache_dir: Path) -> Path:
    model_dir = (cache_dir / model_name).resolve()
    if model_dir.exists():
        return model_dir

    log_info(f"[VOSK] model '{model_name}' not found in cache -> downloading…")

    try:
        model_list = _http_get_json(VOSK_MODEL_LIST_URL)
    except Exception as e:
        raise RuntimeError(
            f"Failed to fetch Vosk model list ({VOSK_MODEL_LIST_URL}): {e}\n"
            f"Either enable internet once, or manually place the model into: {model_dir}"
        )

    entry = _find_vosk_entry(model_list, model_name)

    url = str(entry.get("url", "")).strip()
    if not url:
        raise RuntimeError(f"Vosk model entry has no url: {entry}")

    tmp_dir = cache_dir.parent / "tmp"
    filename = Path(urllib.parse.urlparse(url).path).name or f"{model_name}.zip"
    archive_path = (tmp_dir / filename).resolve()
    extract_root = (tmp_dir / ("vosk_extract_" + model_name)).resolve()

    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)

    try:
        log_info(f"[VOSK] downloading: {url}")
        _download_file(url, archive_path)
        log_info(f"[VOSK] extracting: {archive_path.name}")
        _extract_archive(archive_path, extract_root)

        picked = _pick_extracted_model_dir(extract_root, model_name)

        model_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(picked), str(model_dir))
        log_info(f"[VOSK] ready: {model_dir}")
        return model_dir
    finally:
        try:
            if extract_root.exists():
                shutil.rmtree(extract_root, ignore_errors=True)
        except Exception:
            pass
        try:
            if archive_path.exists():
                archive_path.unlink()
        except Exception:
            pass
