
import os
import logging
import chromadb
from chromadb.utils import embedding_functions
from typing import List, Dict, Any, Optional
import yaml

# Setup logging
LOGGER = logging.getLogger("VectorService")

class VectorService:
    """
    Tjänst för interaktion med ChromaDB (Vektordatabas).
    Hanterar embeddings, indexering och sökning.
    
    Collection: 'dfm_node_index' (Nodes)
    """
    
    def __init__(self, config_path: str = "config/my_mem_config.yaml", collection_name: str = "dfm_node_index"):
        self.config = self._load_config(config_path)
        self.db_path = os.path.expanduser(self.config['paths']['chroma_db'])
        self.collection_name = collection_name
        
        # Init Chroma
        os.makedirs(self.db_path, exist_ok=True)
        self.client = chromadb.PersistentClient(path=self.db_path)
        
        # Embedding Function
        # Läs modellnamn från config, fallback till standard
        model_name = self.config.get('ai_engine', {}).get('models', {}).get('embedding_swedish', "all-MiniLM-L6-v2")
        LOGGER.info(f"Using embedding model: {model_name}")
        
        self.embedding_func = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model_name)
        
        # Get/Create Collection
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name, 
            embedding_function=self.embedding_func
        )
        LOGGER.info(f"VectorService initialized at {self.db_path} (Collection: {self.collection_name})")

    def _load_config(self, path: str) -> dict:
        try:
            # Försök hitta config relativt om absolut misslyckas
            if not os.path.exists(path):
                # Försök hitta uppåt
                base_dir = os.path.dirname(os.path.abspath(__file__))
                path = os.path.join(base_dir, "..", "..", "config", "my_mem_config.yaml")
                
            with open(path, "r") as f:
                return yaml.safe_load(f)
        except Exception as e:
            LOGGER.error(f"Failed to load config from {path}: {e}")
            raise

    def upsert(self, id: str, text: str, metadata: Dict[str, Any] = None):
        """Lägg till eller uppdatera ett dokument i indexet."""
        if not text:
            LOGGER.warning(f"Skipping upsert for {id}: Empty text")
            return

        try:
            self.collection.upsert(
                ids=[id],
                documents=[text],
                metadatas=[metadata or {}]
            )
            LOGGER.debug(f"Upserted {id} into vector index.")
        except Exception as e:
            LOGGER.error(f"Failed to upsert {id}: {e}")
            raise

    def upsert_node(self, node: Dict):
        """
        Specialmetod för att indexera en graf-nod.
        Formaterar noden till sökbar text.
        """
        if not node: return
        
        node_id = node.get('id')
        node_type = node.get('type')
        properties = node.get('properties', {})
        name = properties.get('name', '')
        
        if not name: return

        # Bygg textrepresentation ("Document") för embedding
        aliases = node.get('aliases', [])
        aliases_str = ", ".join(aliases) if aliases else ""
        context_keywords = properties.get('context_keywords', [])
        keywords_str = ", ".join(context_keywords) if isinstance(context_keywords, list) else str(context_keywords)
        role = properties.get('role', '')
        
        text_parts = [
            f"Name: {name}",
            f"Type: {node_type}"
        ]
        if role: text_parts.append(f"Role: {role}")
        if aliases_str: text_parts.append(f"Aliases: {aliases_str}")
        if keywords_str: text_parts.append(f"Keywords: {keywords_str}")
            
        full_text = ". ".join(text_parts)
        
        metadata = {
            "type": node_type,
            "name": name,
            "source": "graph_node"
        }
        
        self.upsert(id=node_id, text=full_text, metadata=metadata)

    def search(self, query_text: str, limit: int = 5, where: Dict = None) -> List[Dict]:
        """
        Sök efter liknande dokument.
        Returnerar en lista av dicts: { 'id': str, 'distance': float, 'metadata': dict, 'document': str }
        """
        if not query_text:
            return []
            
        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=limit,
                where=where
            )
            
            # Formatera resultatet snyggare
            formatted_results = []
            if not results['ids']:
                return []
                
            ids = results['ids'][0]
            distances = results['distances'][0] if results['distances'] else [0.0]*len(ids)
            metadatas = results['metadatas'][0] if results['metadatas'] else [{}]*len(ids)
            documents = results['documents'][0] if results['documents'] else [""]*len(ids)
            
            for i in range(len(ids)):
                formatted_results.append({
                    "id": ids[i],
                    "distance": distances[i], # Lägre är bättre (cosine distance)
                    "metadata": metadatas[i],
                    "document": documents[i]
                })
                
            return formatted_results
            
        except Exception as e:
            LOGGER.error(f"Vector search failed: {e}")
            return []

    def delete(self, id: str):
        """Ta bort ett dokument från indexet."""
        try:
            self.collection.delete(ids=[id])
            LOGGER.debug(f"Deleted {id} from vector index.")
        except Exception as e:
            LOGGER.error(f"Failed to delete {id}: {e}")

    def count(self) -> int:
        return self.collection.count()


