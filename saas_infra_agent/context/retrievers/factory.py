from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)

def get_retriever():
    """ Return the right retrieve function based on vector_store in config. """
    mode = config["vector_store"]["retrieval_mode"]
    provider = config["vector_store"]["provider"]

    if mode == "hybrid" and provider == "qdrant":
        from .hybrid_qdrant import retrieve

    return retrieve
