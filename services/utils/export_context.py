"""
Export Context - Exportera K (Kontext) som markdown.

Del av Plan 1: Validera Context Assembly Engine-konceptet.
"""

import os
import re
from datetime import datetime
from pathlib import Path


def export_context(
    query: str,
    synthesis: str,
    facts: list,
    candidates: list,
    output_folder: str
) -> str:
    """
    Exportera K (Kontext) som markdown-fil.
    
    Args:
        query: Ursprunglig fråga
        synthesis: Tornets innehåll (syntes)
        facts: Lista med bevis/fakta
        candidates: Lista med kandidat-dokument
        output_folder: Mapp att spara till
    
    Returns:
        Sökväg till skapad fil
    """
    output_folder = Path(output_folder).expanduser()
    output_folder.mkdir(parents=True, exist_ok=True)
    
    # Generera filnamn med timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"context_{timestamp}.md"
    filepath = output_folder / filename
    
    # Bygg markdown
    lines = []
    
    # Header
    lines.append(f"# Kontext: {query}")
    lines.append(f"Genererad: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    
    # Syntes (Tornet)
    lines.append("## Syntes")
    if synthesis:
        lines.append(synthesis)
    else:
        lines.append("*Ingen syntes tillgänglig*")
    lines.append("")
    
    # Bevis
    lines.append("## Bevis")
    if facts:
        for fact in facts:
            lines.append(f"- {fact}")
    else:
        lines.append("*Inga bevis extraherade*")
    lines.append("")
    
    # Källor (kandidater)
    lines.append("## Källor")
    if candidates:
        lines.append("| Fil | Relevans |")
        lines.append("|-----|----------|")
        for c in candidates[:20]:  # Max 20 källor
            fname = c.get('filename', c.get('id', 'Okänd'))
            # Ta bort UUID från filnamn för läsbarhet
            clean_name = re.sub(r'_[a-f0-9-]{36}\.md$', '.md', fname)
            score = c.get('score', 0)
            lines.append(f"| {clean_name} | {score:.2f} |")
    else:
        lines.append("*Inga källor*")
    
    # Skriv fil
    content = "\n".join(lines)
    filepath.write_text(content, encoding='utf-8')
    
    return str(filepath)











