#!/usr/bin/env python3
"""
tool_staged_rebuild.py - Kronologisk återuppbyggnad av MyMemory

Kör efter hard reset för att indexera data dag-för-dag,
med pauser för konsolidering ("drömma") mellan varje dag.

Stöder två faser för att maximera datakvalitet:
1. Foundation Phase: Bygger grunden från textkällor (Slack, Docs, Mail).
2. Enrichment Phase: Bearbetar ljud/transkript med kontext från grunden.

Användning:
    python tools/tool_staged_rebuild.py --confirm --phase foundation
    python tools/tool_staged_rebuild.py --confirm --phase enrichment
"""

import os
import sys
import signal
import argparse
import yaml
import logging

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.rebuild import RebuildOrchestrator

# === LOGGING ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - REBUILD - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('StagedRebuild')

# === CONFIG ===

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'my_mem_config.yaml')
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    # Propagera multipass-flagga i config om satt via CLI
    try:
        if hasattr(NAMESPACE, 'multipass') and NAMESPACE.multipass:
            config.setdefault("processing", {})["multipass_enabled"] = True
    except NameError:
        LOGGER.debug("NAMESPACE inte definierad än vid modulimport (förväntat)")
    return config

CONFIG = load_config()

# Global orchestrator för signal handling
_orchestrator = None


def handle_interrupt(signum, frame):
    """Hantera avbrott (Ctrl+C)."""
    print("\n⚠️ Avbruten.")
    if _orchestrator:
        if _orchestrator.staging_info:
            print("📂 Återställer filer...")
            _orchestrator.file_manager.restore_all_from_staging(_orchestrator.staging_info)
        _orchestrator.file_manager.cleanup_staging()
    sys.exit(1)


def main():
    global _orchestrator, NAMESPACE
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--phase', choices=['foundation', 'enrichment'], required=True, 
                        help='Välj fas: foundation (text) eller enrichment (ljud)')
    parser.add_argument('--multipass', action='store_true')
    parser.add_argument('--days', type=int)
    
    NAMESPACE = parser.parse_args()
    
    if not NAMESPACE.confirm:
        print("""
╔══════════════════════════════════════════════════════════════╗
║              STAGED REBUILD - MyMemory v6                    ║
╠══════════════════════════════════════════════════════════════╣
║  Detta kommer att:                                           ║
║                                                              ║
║  • Processera filer dag-för-dag                              ║
║  • Indexera i Lake                                           ║
║  • Bygga graf                                                ║
║  • Konsolidera med Dreamer                                   ║
║  • Interaktiv granskning (om entiteter hittas)               ║
║                                                              ║
║  För att köra: python tools/tool_staged_rebuild.py --confirm --phase <foundation|enrichment>
╚══════════════════════════════════════════════════════════════╝
""")
        sys.exit(0)
    
    # Registrera signal handler
    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)
    
    # Uppdatera config med multipass om satt
    if NAMESPACE.multipass:
        CONFIG.setdefault("processing", {})["multipass_enabled"] = True
    
    # Skapa och kör orchestrator
    _orchestrator = RebuildOrchestrator(NAMESPACE.phase, CONFIG)
    _orchestrator.run(days_limit=NAMESPACE.days, use_multipass=NAMESPACE.multipass)


if __name__ == "__main__":
    main()
