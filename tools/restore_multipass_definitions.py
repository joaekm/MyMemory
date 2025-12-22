#!/usr/bin/env python3
"""
Återställ multipass_definition från template-filen till den faktiska taxonomin.
Behåller alla sub_nodes från den faktiska taxonomin.
"""

import json
import os
import sys

# Lägg till projektroten i sys.path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)

template_path = os.path.join(project_root, 'config', 'taxonomy_template.json')
# Läs sökvägar från config (Princip 8)
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml')
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)
taxonomy_path = os.path.expanduser(config['paths']['taxonomy_file'])

print("📚 Återställer multipass_definition från template...")
print(f"   Template: {template_path}")
print(f"   Taxonomy: {taxonomy_path}")
print()

# Ladda template och faktisk taxonomi
with open(template_path, 'r', encoding='utf-8') as f:
    template = json.load(f)

with open(taxonomy_path, 'r', encoding='utf-8') as f:
    taxonomy = json.load(f)

# Återställ multipass_definition från template, behåll sub_nodes
updated = 0
for key in taxonomy.keys():
    if key in template:
        if 'multipass_definition' in template[key]:
            if 'multipass_definition' not in taxonomy[key] or taxonomy[key]['multipass_definition'] != template[key]['multipass_definition']:
                taxonomy[key]['multipass_definition'] = template[key]['multipass_definition']
                updated += 1
                print(f'✅ Återställde multipass_definition för {key}')
        elif 'multipass_definition' in taxonomy[key]:
            # Ta bort om den saknas i template
            del taxonomy[key]['multipass_definition']
            print(f'⚠️  Tog bort multipass_definition för {key} (saknas i template)')

# Spara uppdaterad taxonomi
with open(taxonomy_path, 'w', encoding='utf-8') as f:
    json.dump(taxonomy, f, ensure_ascii=False, indent=2)

print(f'\n✅ Klar! Återställde multipass_definition för {updated} masternoder.')
print(f'   Taxonomi sparad: {taxonomy_path}')

