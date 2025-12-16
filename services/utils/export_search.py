"""
Export kandidater till hotfiles som symlinks.

Används av /export kommandot i chatten.
"""

import os
import shutil
from pathlib import Path


MAX_EXPORT = 10  # Gemini tar max 10 filer


def export_candidates(candidates: list, hot_folder: str, lake_path: str) -> dict:
    """
    Skapa symlinks från kandidatlista till hot folder.
    
    Sorterar på score och exporterar max 10 filer (Geminis gräns).
    
    Args:
        candidates: Lista med dokument (måste ha 'filename' eller 'id')
        hot_folder: Sökväg till hotfolder
        lake_path: Sökväg till Lake
    
    Returns:
        dict med status, count, files, total (antal före begränsning)
    """
    if not candidates:
        return {"status": "NO_RESULTS", "count": 0, "files": [], "total": 0}
    
    # Sortera på score (högst först) och begränsa till MAX_EXPORT
    sorted_candidates = sorted(candidates, key=lambda x: x.get('score', 0), reverse=True)
    top_candidates = sorted_candidates[:MAX_EXPORT]
    total = len(candidates)
    
    hot_folder = Path(hot_folder).expanduser()
    lake_path = Path(lake_path).expanduser()
    
    # Rensa och skapa mapp
    if hot_folder.exists():
        shutil.rmtree(hot_folder)
    hot_folder.mkdir(parents=True)
    
    # Skapa symlinks för top kandidater
    created = []
    for doc in top_candidates:
        fname = doc.get("filename", f"{doc.get('id', 'unknown')}.md")
        src = lake_path / fname
        if src.exists():
            (hot_folder / fname).symlink_to(src)
            created.append(fname)
    
    return {
        "status": "OK", 
        "count": len(created), 
        "files": created,
        "folder": str(hot_folder),
        "total": total
    }
