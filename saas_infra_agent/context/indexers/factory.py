from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger


logger = get_logger(__name__)


def get_indexer():
   """Return the right index_codebase function based on vector_store in config."""
   mode = config["vector_store"]["retrieval_mode"]
   vector_store = config["vector_store"]["provider"]
   logger.info(f"Using vector store: {vector_store}")

   if vector_store == "qdrant" and mode in {"hybrid", "sparse"}:
      from .hybrid_qdrant import index_codebase
   
   return index_codebase


def get_index_inspector():
    """Return the right show_index function based on vector_store in config."""
    mode = config["vector_store"]["retrieval_mode"]
    vector_store = config["vector_store"]["provider"]
    logger.info(f"Using vector store: {vector_store}")

    if vector_store == "qdrant" and mode in {"hybrid", "sparse"}:
       from .hybrid_qdrant import show_index
    
    return show_index
