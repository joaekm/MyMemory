#!/usr/bin/env python3
"""
Graf-inspektör - Visa information om grafdatabasen (DuckDB).
"""
import os
import sys
import yaml

# Lägg till services i path för import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.graph_service import GraphStore

# --- CONFIG ---
def ladda_yaml(filnamn):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                return yaml.safe_load(f)
    print(f"[FEL] Saknar {filnamn}")
    sys.exit(1)

CONFIG = ladda_yaml('my_mem_config.yaml')
GRAPH_PATH = os.path.expanduser(CONFIG['paths']['kuzu_db'])


def inspektera_graf():
    """Visa översikt av grafen."""
    if not os.path.exists(GRAPH_PATH):
        print(f"❌ Graf-databas finns inte: {GRAPH_PATH}")
        print("   Tips: Kör 'python services/my_mem_graph_builder.py' för att skapa")
        return
    
    try:
        graph = GraphStore(GRAPH_PATH, read_only=True)
        stats = graph.get_stats()
        
        print("=" * 60)
        print(" GRAF-INSPEKTION (DuckDB)")
        print("=" * 60)
        print(f"\n📊 Statistik:")
        print(f"   Totalt noder:  {stats['total_nodes']}")
        print(f"   Totalt kanter: {stats['total_edges']}")
        
        print(f"\n📦 Noder per typ:")
        for node_type, count in sorted(stats['nodes'].items()):
            print(f"   - {node_type}: {count}")
        
        print(f"\n🔗 Kanter per typ:")
        for edge_type, count in sorted(stats['edges'].items()):
            print(f"   - {edge_type}: {count}")
        
        # Visa några exempel-entiteter
        print(f"\n👥 Exempel på Entity-noder (max 10):")
        entities = graph.find_nodes_by_type("Entity")
        for entity in entities[:10]:
            entity_type = entity.get('properties', {}).get('entity_type', 'Unknown')
            aliases = entity.get('aliases', [])
            alias_str = f" [alias: {', '.join(aliases[:2])}]" if aliases else ""
            print(f"   - {entity['id']} ({entity_type}){alias_str}")
        
        if len(entities) > 10:
            print(f"   ... och {len(entities) - 10} till")
        
        graph.close()
        
    except Exception as e:
        print(f"❌ Fel vid graf-inspektion: {e}")
        raise


def inspektera_nod(node_id: str):
    """Inspektera en specifik nod."""
    if not os.path.exists(GRAPH_PATH):
        print(f"❌ Graf-databas finns inte: {GRAPH_PATH}")
        return
    
    try:
        graph = GraphStore(GRAPH_PATH, read_only=True)
        
        print(f"\n🔍 Inspekterar nod: {node_id}")
        print("=" * 60)
        
        node = graph.get_node(node_id)
        if not node:
            print(f"❌ Nod '{node_id}' finns inte i grafen")
            
            # Kolla om det finns som alias
            matches = graph.find_nodes_by_alias(node_id)
            if matches:
                print(f"\n💡 Hittades som alias för:")
                for m in matches:
                    print(f"   - {m['id']} ({m['type']})")
            return
        
        print(f"\n📋 Nod-info:")
        print(f"   ID:     {node['id']}")
        print(f"   Typ:    {node['type']}")
        print(f"   Alias:  {node.get('aliases', [])}")
        print(f"   Props:  {node.get('properties', {})}")
        
        # Hitta utgående kanter
        outgoing = graph.get_edges_from(node_id)
        if outgoing:
            print(f"\n🔗 Utgående kanter ({len(outgoing)}):")
            for edge in outgoing[:10]:
                print(f"   → [{edge['type']}] → {edge['target']}")
            if len(outgoing) > 10:
                print(f"   ... och {len(outgoing) - 10} till")
        
        # Hitta inkommande kanter
        incoming = graph.get_edges_to(node_id)
        if incoming:
            print(f"\n🔗 Inkommande kanter ({len(incoming)}):")
            for edge in incoming[:10]:
                print(f"   ← [{edge['type']}] ← {edge['source']}")
            if len(incoming) > 10:
                print(f"   ... och {len(incoming) - 10} till")
        
        graph.close()
        
    except Exception as e:
        print(f"❌ Fel vid nod-inspektion: {e}")
        raise


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Inspektera specifik nod
        node_id = sys.argv[1]
        inspektera_nod(node_id)
    else:
        # Visa översikt
        inspektera_graf()
