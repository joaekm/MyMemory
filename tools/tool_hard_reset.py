#!/usr/bin/env python3
"""
HARD DATA RESET - MyMemory v6 (Wrapper)

Detta är en wrapper för bakåtkompatibilitet.
Den faktiska implementationen finns i tools/rebuild/hard_reset.py
"""

import sys
import os

# Lägg till project root i path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importera och kör main från rebuild.hard_reset
from tools.rebuild.hard_reset import main

if __name__ == "__main__":
    main()
