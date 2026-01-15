#!/usr/bin/env python3
"""
migrate_graph_properties.py - Migrerar graf-noder till nytt schema.

Lägger till saknade required properties på alla noder:
- last_synced_at (sätts till created_at eller now)
- confidence (sätts till 0.5)
- status (sätts till PROVISIONAL)

Användning:
    python tools/migrate_graph_properties.py --dry-run   # Visa vad som skulle ändras
    python tools/migrate_graph_properties.py --confirm   # Kör migrationen
"""

import os
import sys
import json
import argparse
from datetime import datetime

# Lägg till projektroten för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.graph_service import GraphStore
import yaml


def load_config():
    """Laddar huvudconfig för sökvägar"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "config", "my_mem_config.yaml")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    for k, v in config.get('paths', {}).items():
        if isinstance(v, str):
            config['paths'][k] = os.path.expanduser(v)

    return config


def migrate_nodes(graph: GraphStore, dry_run: bool = True):
    """
    Migrerar alla noder till nytt schema.

    Returns:
        Dict med statistik
    """
    stats = {
        "total": 0,
        "needs_migration": 0,
        "migrated": 0,
        "by_field": {
            "last_synced_at": 0,
            "confidence": 0,
            "status": 0
        }
    }

    now_ts = datetime.now().isoformat()

    # Hämta alla noder
    with graph._lock:
        rows = graph.conn.execute(
            "SELECT id, type, properties FROM nodes"
        ).fetchall()

    stats["total"] = len(rows)
    print(f"\nTotalt {len(rows)} noder att kontrollera...")

    nodes_to_update = []

    for row in rows:
        node_id, node_type, props_json = row
        try:
            props = json.loads(props_json) if props_json else {}
        except:
            props = {}

        missing_fields = []
        updated_props = props.copy()

        # Kontrollera last_synced_at
        if "last_synced_at" not in props:
            # Använd created_at om det finns, annars now
            fallback = props.get("created_at", props.get("last_seen_at", now_ts))
            updated_props["last_synced_at"] = fallback
            missing_fields.append("last_synced_at")
            stats["by_field"]["last_synced_at"] += 1

        # Kontrollera confidence
        if "confidence" not in props:
            updated_props["confidence"] = 0.5
            missing_fields.append("confidence")
            stats["by_field"]["confidence"] += 1

        # Kontrollera status
        if "status" not in props:
            updated_props["status"] = "PROVISIONAL"
            missing_fields.append("status")
            stats["by_field"]["status"] += 1

        if missing_fields:
            stats["needs_migration"] += 1
            nodes_to_update.append({
                "id": node_id,
                "type": node_type,
                "missing": missing_fields,
                "props": updated_props
            })

            if dry_run:
                print(f"  [{node_type}] {node_id[:8]}... saknar: {', '.join(missing_fields)}")

    print(f"\n{stats['needs_migration']} noder behöver migreras.")
    print(f"  - last_synced_at: {stats['by_field']['last_synced_at']}")
    print(f"  - confidence: {stats['by_field']['confidence']}")
    print(f"  - status: {stats['by_field']['status']}")

    if dry_run:
        print("\n[DRY-RUN] Inga ändringar gjordes.")
        return stats

    # Kör migrationen
    print("\nKör migration...")

    with graph._lock:
        for node in nodes_to_update:
            props_json = json.dumps(node["props"], ensure_ascii=False)
            graph.conn.execute(
                "UPDATE nodes SET properties = ? WHERE id = ?",
                [props_json, node["id"]]
            )
            stats["migrated"] += 1

    print(f"\n✅ Migrerade {stats['migrated']} noder.")
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Migrerar graf-noder till nytt schema med required properties"
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Visa vad som skulle ändras utan att faktiskt ändra')
    parser.add_argument('--confirm', action='store_true',
                        help='Kör migrationen (krävs för att faktiskt ändra)')
    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        print("Användning:")
        print("  --dry-run    Visa vad som skulle ändras")
        print("  --confirm    Kör migrationen")
        sys.exit(1)

    config = load_config()
    graph_path = config.get('paths', {}).get('graph_db')

    if not graph_path or not os.path.exists(graph_path):
        print(f"HARDFAIL: Graf-db saknas: {graph_path}")
        sys.exit(1)

    print(f"Graf-databas: {graph_path}")

    # Öppna i write-mode om vi ska ändra
    read_only = args.dry_run
    graph = GraphStore(graph_path, read_only=read_only)

    try:
        stats = migrate_nodes(graph, dry_run=args.dry_run)
    finally:
        graph.close()

    sys.exit(0 if stats["needs_migration"] == 0 or stats["migrated"] > 0 else 1)


if __name__ == "__main__":
    main()
