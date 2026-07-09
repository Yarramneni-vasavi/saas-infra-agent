from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger


logger = get_logger(__name__)


def _build_chat_openai(model: str, max_tokens: int | None):
  from langchain_openai import ChatOpenAI

  kwargs = {"model": model}
  if max_tokens is not None:
    kwargs["max_tokens"] = max_tokens
  return ChatOpenAI(**kwargs)


def get_llm():
  """Return the right LangChain LLM based on config."""
  provider = config["llm"]["provider"]
  model = config["llm"]["model"]
  max_tokens = config["llm"].get("max_tokens")
  logger.info(f"Using LLM provider: {provider}, model: {model}, max_tokens: {max_tokens}")

  # if provider == "anthropic":
  #     from langchain_anthropic import ChatAnthropic
  #     return ChatAnthropic(model=model)

  return _build_chat_openai(model, max_tokens)


def get_small_llm():
  """Return the right LangChain LLM based on config."""
  provider = config["llm"]["provider"]
  model = config["llm"]["small_model"]
  max_tokens = config["llm"].get("small_max_tokens", config["llm"].get("max_tokens"))
  logger.info(f"Using LLM provider: {provider}, model: {model}, max_tokens: {max_tokens}")

  # if provider == "anthropic":
  #     from langchain_anthropic import ChatAnthropic
  #     return ChatAnthropic(model=model)

  return _build_chat_openai(model, max_tokens)


def get_embedder():
  """Return the right LangChain embedder based on config."""
  provider = config["embeddings"]["provider"]
  model = config["embeddings"]["model"]
  logger.info(f"Using embeddings provider: {provider}, model: {model}")

  # if provider == "huggingface":
  #     from langchain_huggingface import HuggingFaceEmbeddings
  #     return HuggingFaceEmbeddings(model_name=model)
  
  from langchain_openai import OpenAIEmbeddings
  return OpenAIEmbeddings(model=model)
