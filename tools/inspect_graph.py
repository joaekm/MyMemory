#!/usr/bin/env python3
"""
Graph Inspector - Inspektera Evidence, Relationer och Alias

Användning:
    python tools/inspect_graph.py
    python tools/inspect_graph.py --evidence-only
    python tools/inspect_graph.py --relations-only
    python tools/inspect_graph.py --aliases-only
    python tools/inspect_graph.py --entity "Jocke"  # Visa detaljer för specifik entitet
"""

import os
import sys
import json
import argparse
import yaml

# Lägg till project root i path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.graph_service import GraphStore


def load_config():
    """Ladda config för att hitta GraphDB-sökväg."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml')
    
    if not os.path.exists(config_path):
        print(f"❌ HARDFAIL: Kunde inte hitta config: {config_path}")
        sys.exit(1)
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    graph_path = os.path.expanduser(config['paths']['graph_db'])
    return graph_path


def inspect_evidence(graph: GraphStore, limit: int = 20):
    """Inspektera Evidence Layer."""
    print("=" * 60)
    print("EVIDENCE LAYER (Learnings)")
    print("=" * 60)
    
    # Total statistik
    evidence_count = graph.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    print(f"\n📊 Total evidence items: {evidence_count}")
    
    if evidence_count == 0:
        print("  ⚠️  Inga evidence items hittades.")
        return
    
    # Statistik per masternod
    evidence_by_master = graph.conn.execute("""
        SELECT master_node_candidate, COUNT(*) as count, AVG(confidence) as avg_conf
        FROM evidence
        GROUP BY master_node_candidate
        ORDER BY count DESC
        LIMIT 10
    """).fetchall()
    
    print("\n📈 Top 10 Master Nodes (efter antal evidence):")
    for row in evidence_by_master:
        master_node = row[0]
        count = row[1]
        avg_conf = row[2] if row[2] is not None else 0.0
        print(f"  {master_node:30} {count:4} items  (avg confidence: {avg_conf:.2f})")
    
    # Senaste evidence
    print(f"\n📝 Senaste {limit} evidence items:")
    recent_evidence = graph.conn.execute("""
        SELECT entity_name, master_node_candidate, context_description, 
               source_file, confidence, created_at
        FROM evidence
        ORDER BY created_at DESC
        LIMIT ?
    """, [limit]).fetchall()
    
    for ev in recent_evidence:
        entity_name = ev[0]
        master_node = ev[1]
        context = ev[2][:80] + "..." if len(ev[2]) > 80 else ev[2]
        source_file = ev[3]
        confidence = ev[4] if ev[4] is not None else "N/A"
        created_at = ev[5]
        
        print(f"\n  Entity: {entity_name}")
        print(f"    Master Node: {master_node}")
        print(f"    Context: {context}")
        print(f"    Source: {source_file}")
        print(f"    Confidence: {confidence}")
        print(f"    Created: {created_at}")


def inspect_relations(graph: GraphStore, limit: int = 20):
    """Inspektera Relationer (Edges)."""
    print("\n" + "=" * 60)
    print("RELATIONER (Edges)")
    print("=" * 60)
    
    # Total statistik
    total_edges = graph.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"\n📊 Total relationer: {total_edges}")
    
    if total_edges == 0:
        print("  ⚠️  Inga relationer hittades.")
        return
    
    # Relationstyper och antal
    edge_types = graph.conn.execute("""
        SELECT edge_type, COUNT(*) as count
        FROM edges
        GROUP BY edge_type
        ORDER BY count DESC
    """).fetchall()
    
    print("\n🔗 Relationstyper:")
    for et in edge_types:
        edge_type = et[0]
        count = et[1]
        print(f"  {edge_type:30} {count:6} relationer")
    
    # Exempel på relationer
    print(f"\n📝 Exempel på relationer (top {limit}):")
    sample_edges = graph.conn.execute("""
        SELECT source, target, edge_type, properties
        FROM edges
        LIMIT ?
    """, [limit]).fetchall()
    
    for edge in sample_edges:
        source = edge[0]
        target = edge[1]
        edge_type = edge[2]
        props = json.loads(edge[3]) if edge[3] else {}
        
        print(f"\n  {source} --[{edge_type}]--> {target}")
        if props:
            props_str = ", ".join([f"{k}={v}" for k, v in props.items()])
            print(f"    Properties: {props_str}")


def inspect_aliases(graph: GraphStore, limit: int = 20):
    """Inspektera Alias."""
    print("\n" + "=" * 60)
    print("ALIAS")
    print("=" * 60)
    
    # Total statistik
    total_entities = graph.conn.execute("""
        SELECT COUNT(*) FROM nodes WHERE type = 'Entity'
    """).fetchone()[0]
    
    entities_with_aliases = graph.conn.execute("""
        SELECT COUNT(*) 
        FROM nodes 
        WHERE type = 'Entity' AND aliases IS NOT NULL AND aliases != '[]'
    """).fetchone()[0]
    
    print(f"\n📊 Totala entiteter: {total_entities}")
    print(f"📊 Entiteter med aliases: {entities_with_aliases}")
    
    if entities_with_aliases == 0:
        print("  ⚠️  Inga entiteter med aliases hittades.")
        return
    
    # Hämta alla Entity-noder med aliases
    entities_with_aliases_list = graph.conn.execute("""
        SELECT id, aliases, properties
        FROM nodes
        WHERE type = 'Entity' AND aliases IS NOT NULL AND aliases != '[]'
        ORDER BY id
        LIMIT ?
    """, [limit]).fetchall()
    
    print(f"\n📝 Entiteter med aliases (top {limit}):")
    for entity in entities_with_aliases_list:
        entity_id = entity[0]
        aliases = json.loads(entity[1]) if entity[1] else []
        props = json.loads(entity[2]) if entity[2] else {}
        entity_type = props.get('entity_type', 'Unknown')
        
        if aliases:
            aliases_str = ", ".join(aliases)
            print(f"\n  {entity_id} ({entity_type}):")
            print(f"    Aliases: {aliases_str}")


def inspect_entity(graph: GraphStore, entity_name: str):
    """Inspektera detaljer för en specifik entitet."""
    print("=" * 60)
    print(f"ENTITET: {entity_name}")
    print("=" * 60)
    
    # Hitta noden (kan vara canonical eller alias)
    node = graph.get_node(entity_name)
    if not node:
        # Försök hitta via alias
        matches = graph.find_nodes_by_alias(entity_name)
        if matches:
            node = matches[0]
            print(f"⚠️  '{entity_name}' är ett alias för canonical: {node['id']}")
        else:
            print(f"❌ Hittade ingen nod med ID eller alias: {entity_name}")
            return
    
    # Visa nodinformation
    print(f"\n📋 Nodinformation:")
    print(f"  ID: {node['id']}")
    print(f"  Type: {node.get('type', 'Unknown')}")
    
    aliases = node.get('aliases', [])
    if aliases:
        print(f"  Aliases: {', '.join(aliases)}")
    else:
        print(f"  Aliases: (inga)")
    
    properties = node.get('properties', {})
    if properties:
        print(f"  Properties: {json.dumps(properties, indent=4, ensure_ascii=False)}")
    
    # Relationer
    edges_from = graph.get_edges_from(node['id'])
    edges_to = graph.get_edges_to(node['id'])
    
    print(f"\n🔗 Relationer:")
    print(f"  Utgående: {len(edges_from)}")
    for e in edges_from[:10]:
        props_str = ""
        if e.get('properties'):
            props_str = f" ({json.dumps(e['properties'], ensure_ascii=False)})"
        print(f"    -> {e['target']} [{e['type']}]{props_str}")
    
    print(f"  Inkommande: {len(edges_to)}")
    for e in edges_to[:10]:
        props_str = ""
        if e.get('properties'):
            props_str = f" ({json.dumps(e['properties'], ensure_ascii=False)})"
        print(f"    <- {e['source']} [{e['type']}]{props_str}")
    
    # Evidence
    evidences = graph.get_evidence_for_entity(node['id'], limit=10)
    if evidences:
        print(f"\n📚 Evidence ({len(evidences)} items):")
        for ev in evidences[:5]:
            context = ev['context_description'][:100] + "..." if len(ev['context_description']) > 100 else ev['context_description']
            print(f"  - {ev['master_node_candidate']}: {context}")
            print(f"    Source: {ev['source_file']}, Confidence: {ev.get('confidence', 'N/A')}")


def main():
    parser = argparse.ArgumentParser(
        description="Inspektera GraphDB: Evidence, Relationer och Alias"
    )
    parser.add_argument(
        '--evidence-only',
        action='store_true',
        help='Visa endast Evidence Layer'
    )
    parser.add_argument(
        '--relations-only',
        action='store_true',
        help='Visa endast Relationer'
    )
    parser.add_argument(
        '--aliases-only',
        action='store_true',
        help='Visa endast Alias'
    )
    parser.add_argument(
        '--entity',
        type=str,
        help='Visa detaljerad information för en specifik entitet'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=20,
        help='Antal exempel att visa (default: 20)'
    )
    
    args = parser.parse_args()
    
    # Ladda config och öppna graf
    graph_path = load_config()
    
    if not os.path.exists(graph_path):
        print(f"❌ HARDFAIL: GraphDB finns inte: {graph_path}")
        sys.exit(1)
    
    try:
        graph = GraphStore(graph_path, read_only=True)
        
        # Om specifik entitet angiven, visa endast den
        if args.entity:
            inspect_entity(graph, args.entity)
        else:
            # Visa alla sektioner (eller endast vald)
            show_all = not (args.evidence_only or args.relations_only or args.aliases_only)
            
            if show_all or args.evidence_only:
                inspect_evidence(graph, limit=args.limit)
            
            if show_all or args.relations_only:
                inspect_relations(graph, limit=args.limit)
            
            if show_all or args.aliases_only:
                inspect_aliases(graph, limit=args.limit)
        
        graph.close()
        
    except Exception as e:
        print(f"❌ HARDFAIL: Fel vid inspektion: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

