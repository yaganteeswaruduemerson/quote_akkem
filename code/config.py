
import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("RFQConfig")

class ConfigError(Exception):
    pass

class Config:
    # LLM Configuration
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2000"))
    LLM_SYSTEM_PROMPT = os.getenv(
        "LLM_SYSTEM_PROMPT",
        "You are the RFQ Review and Quote Creation Agent. Your role is to automate the review of RFQ data, validate ATP availability, create quotes in EBS, and orchestrate lead time, discount, and tax exempt checks. Ensure all actions are logged, exceptions are escalated to HITL, and all communications are professional and clear. Always retrieve credentials from Key Vault and static data from config files."
    )
    LLM_USER_PROMPT_TEMPLATE = os.getenv(
        "LLM_USER_PROMPT_TEMPLATE",
        "Please provide the required RFQ data or agent_run_id to proceed with quote creation. For restarts, ensure the pipeline_run_id is included."
    )
    LLM_FEW_SHOT_EXAMPLES = [
        "agent_run_id: 12345, pipeline_run_id: abcde -> Resuming RFQ review from agent run 12345. Fetching parent output and continuing process.",
        "json_payload: {...}, pipeline_run_id: xyz -> Starting new RFQ review and quote creation process. Audit entry created and proceeding with validations."
    ]

    # API Endpoints & Credentials (all required)
    API_BASE_URL = os.getenv("API_BASE_URL")
    AZURE_BLOB_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    AZURE_STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    AZURE_STORAGE_CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER_NAME")
    BLOB_PATH_TEMPLATE = os.getenv("BLOB_PATH_TEMPLATE")
    AZURE_SQL_DB_CONNECTION_STRING = os.getenv("AZURE_SQL_DB_CONNECTION_STRING")
    HITL_API_URL = os.getenv("HITL_API_URL")
    ATP_API_URL = os.getenv("ATP_API_URL")
    EBS_QUOTE_API_URL = os.getenv("EBS_QUOTE_API_URL")
    LEAD_TIME_CALCULATOR_API_URL = os.getenv("LEAD_TIME_CALCULATOR_API_URL")
    TDE_API_URL = os.getenv("TDE_API_URL")
    PRICING_API_URL = os.getenv("PRICING_API_URL")
    AZURE_KEY_VAULT_URL = os.getenv("AZURE_KEY_VAULT_URL")

    # Key Vault secret names (for DB, API keys, etc.)
    DB_CONNECTION_STRING_SECRET = os.getenv("DB_CONNECTION_STRING_SECRET")
    OPENAI_API_KEY_SECRET = os.getenv("OPENAI_API_KEY_SECRET")
    # Add more secret names as needed

    # Default values and fallbacks
    DOMAIN = "customer_service"
    AGENT_NAME = "RFQ Review and Quote Creation Agent"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Validation for required keys
    @classmethod
    def validate(cls):
        required_env = [
            "API_BASE_URL",
            "AZURE_STORAGE_CONNECTION_STRING",
            "AZURE_STORAGE_ACCOUNT_NAME",
            "AZURE_STORAGE_CONTAINER_NAME",
            "BLOB_PATH_TEMPLATE",
            "HITL_API_URL",
            "ATP_API_URL",
            "EBS_QUOTE_API_URL",
            "LEAD_TIME_CALCULATOR_API_URL",
            "TDE_API_URL",
            "PRICING_API_URL",
            "AZURE_KEY_VAULT_URL",
            "DB_CONNECTION_STRING_SECRET",
            "OPENAI_API_KEY_SECRET"
        ]
        missing = []
        for key in required_env:
            if getattr(cls, key, None) in [None, ""]:
                missing.append(key)
        if missing:
            logger.error(f"Missing required configuration(s): {', '.join(missing)}")
            raise ConfigError(f"Missing required configuration(s): {', '.join(missing)}")

    # API Key Management (retrieval from Key Vault should be implemented in integration layer)
    @classmethod
    def get_openai_api_key(cls, keyvault_client):
        """
        Retrieve OpenAI API key from Azure Key Vault.
        """
        secret_name = cls.OPENAI_API_KEY_SECRET
        if not secret_name:
            logger.error("OPENAI_API_KEY_SECRET not set in environment/config.")
            raise ConfigError("OPENAI_API_KEY_SECRET not set in environment/config.")
        try:
            api_key = keyvault_client.get_secret(secret_name)
            if not api_key:
                raise ConfigError("OpenAI API key not found in Key Vault.")
            return api_key
        except Exception as e:
            logger.error(f"Error retrieving OpenAI API key from Key Vault: {e}")
            raise ConfigError("Failed to retrieve OpenAI API key from Key Vault.")

    @classmethod
    def get_db_connection_string(cls, keyvault_client):
        """
        Retrieve DB connection string from Azure Key Vault.
        """
        secret_name = cls.DB_CONNECTION_STRING_SECRET
        if not secret_name:
            logger.error("DB_CONNECTION_STRING_SECRET not set in environment/config.")
            raise ConfigError("DB_CONNECTION_STRING_SECRET not set in environment/config.")
        try:
            conn_str = keyvault_client.get_secret(secret_name)
            if not conn_str:
                raise ConfigError("DB connection string not found in Key Vault.")
            return conn_str
        except Exception as e:
            logger.error(f"Error retrieving DB connection string from Key Vault: {e}")
            raise ConfigError("Failed to retrieve DB connection string from Key Vault.")

    # Domain-specific settings
    @classmethod
    def get_domain_settings(cls):
        return {
            "domain": cls.DOMAIN,
            "agent_name": cls.AGENT_NAME,
            "log_level": cls.LOG_LEVEL
        }

    # Error handling for missing API keys or config
    @classmethod
    def get_required_api_url(cls, key):
        url = getattr(cls, key, None)
        if not url:
            logger.error(f"Missing required API URL: {key}")
            raise ConfigError(f"Missing required API URL: {key}")
        return url

    # Example: get static config value
    @classmethod
    def get_config_value(cls, key, default=None):
        return getattr(cls, key, default)

# Validate config at import
try:
    Config.validate()
except ConfigError as ce:
    logger.critical(f"Configuration validation failed: {ce}")
    raise

# Usage example (in agent code):
# openai_api_key = Config.get_openai_api_key(keyvault_client)
# db_conn_str = Config.get_db_connection_string(keyvault_client)
# atp_api_url = Config.get_required_api_url("ATP_API_URL")
