    def find_potential_matches(self, node: Dict) -> List[Dict]:
        """Hitta potentiella dubbletter för en given nod."""
        # 1. Fuzzy search på namn/alias
        name = node.get("properties", {}).get("name", "")
        if not name:
            # Fallback: Sök på ID om namn saknas (t.ex. vid tester)
            name = node.get("id")
            
        if not name:
            return []
            
        # Sök efter liknande namn
        # Exkludera sig själv
        # OBS: GraphStore fuzzy-söker på ID och Aliases.
        # Vi måste se till att namnet vi söker på finns i grafen.
        # För MVP-testerna, nodernas IDn matchar ofta delvis namnen, 
        # men "Microsoft Corp" (name) vs "ms_corp" (id) är inte en ID-match.
        
        # FIX: Vi behöver söka på NAME-propertyt också, inte bara ID/Alias.
        # Men GraphStore.find_nodes_fuzzy söker bara på ID/Alias.
        # Vi måste antingen uppdatera GraphStore eller göra en bredare sökning här.
        
        # För MVP: Vi söker på delar av namnet.
        search_term = name.split()[0] if " " in name else name
        
        matches = self.graph_store.find_nodes_fuzzy(search_term, limit=10)
        
        valid_matches = []
        for m in matches:
            if m["id"] == node["id"]:
                continue
            
            # Enkel heuristik: Måste vara samma typ (Person vs Person)
            # if m["type"] != node["type"]:
            #    continue
                
            valid_matches.append(m)
            
        return valid_matches