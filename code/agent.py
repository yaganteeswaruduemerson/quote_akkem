
import os
import logging
import json
import re
import asyncio
from typing import Any, Dict, Optional, Union
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError, field_validator, constr
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient
from azure.storage.blob.aio import BlobServiceClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
import openai

# =========================
# Logging Configuration
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("RFQQuoteAgent")

# =========================
# Configuration Management
# =========================
class ConfigManager:
    """
    Loads static data and configuration from environment variables and files.
    """
    _config: Dict[str, Any] = {}

    @classmethod
    def load_config(cls):
        load_dotenv()
        required_keys = [
            "OPENAI_API_KEY",
            "AZURE_KEY_VAULT_URL",
            "AZURE_STORAGE_CONNECTION_STRING",
            "AZURE_STORAGE_ACCOUNT_NAME",
            "AZURE_STORAGE_CONTAINER_NAME",
            "BLOB_PATH_TEMPLATE",
            "HITL_API_URL",
            "API_BASE_URL",
            "DB_CONNECTION_STRING_SECRET"
        ]
        for key in required_keys:
            value = os.getenv(key)
            if not value:
                logger.error(f"Missing required config key: {key}")
                raise RuntimeError(f"Missing required config key: {key}")
            cls._config[key] = value

    @classmethod
    def get_config(cls, key: str) -> str:
        if not cls._config:
            cls.load_config()
        if key not in cls._config:
            logger.error(f"Config key not found: {key}")
            raise KeyError(f"Config key not found: {key}")
        return cls._config[key]

# =========================
# Security Layer
# =========================
class KeyVaultClient:
    """
    Retrieves credentials and secrets securely from Azure Key Vault.
    """
    def __init__(self, vault_url: str):
        self.vault_url = vault_url
        self.credential = DefaultAzureCredential()
        self.client = SecretClient(vault_url=self.vault_url, credential=self.credential)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2), retry=retry_if_exception_type(Exception))
    async def get_secret(self, secret_name: str) -> str:
        try:
            secret = await self.client.get_secret(secret_name)
            return secret.value
        except Exception as e:
            logger.error(f"Failed to retrieve secret {secret_name} from Key Vault: {e}")
            raise

# =========================
# Integration Layer
# =========================
class IntegrationBase:
    pass

class AzureBlobClient(IntegrationBase):
    """
    Handles blob storage operations for input/output JSON files.
    """
    def __init__(self, connection_string: str, container_name: str):
        self.connection_string = connection_string
        self.container_name = container_name
        self.service_client = BlobServiceClient.from_connection_string(self.connection_string)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2), retry=retry_if_exception_type(Exception))
    async def download_blob(self, blob_path: str) -> Dict[str, Any]:
        try:
            container_client = self.service_client.get_container_client(self.container_name)
            blob_client = container_client.get_blob_client(blob_path)
            stream = await blob_client.download_blob()
            data = await stream.readall()
            return json.loads(data)
        except Exception as e:
            logger.error(f"Failed to download blob {blob_path}: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2), retry=retry_if_exception_type(Exception))
    async def upload_blob(self, blob_path: str, data: Dict[str, Any]) -> None:
        try:
            container_client = self.service_client.get_container_client(self.container_name)
            blob_client = container_client.get_blob_client(blob_path)
            await blob_client.upload_blob(json.dumps(data), overwrite=True)
        except Exception as e:
            logger.error(f"Failed to upload blob {blob_path}: {e}")
            raise

class AzureSQLClient(IntegrationBase):
    """
    Handles SQL DB operations for audit logging and agent run status.
    """
    def __init__(self, db_connection_string: str):
        self.engine = create_async_engine(db_connection_string, echo=False, future=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2), retry=retry_if_exception_type(Exception))
    async def execute_query(self, query: str, params: Optional[dict] = None) -> Any:
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(query, params or {})
                await conn.commit()
                return result
        except Exception as e:
            logger.error(f"SQL execution failed: {e}")
            raise

# =========================
# Business Logic Layer
# =========================
class ServiceBase:
    pass

class AuditLogger(ServiceBase):
    """
    Persists audit logs in Azure SQL DB, ensures immutability and traceability.
    """
    def __init__(self, sql_client: AzureSQLClient):
        self.sql_client = sql_client

    async def log_action(self, action: str, status: str, details: str) -> None:
        try:
            query = (
                "INSERT INTO qo_audit_log (action, status, details) "
                "VALUES (:action, :status, :details)"
            )
            await self.sql_client.execute_query(query, {"action": action, "status": status, "details": details})
        except Exception as e:
            logger.error(f"Audit log action failed: {e}")
            raise

    async def log_error(self, action: str, error_code: str, details: str) -> None:
        try:
            query = (
                "INSERT INTO qo_audit_log (action, status, details, error_code) "
                "VALUES (:action, 'ERROR', :details, :error_code)"
            )
            await self.sql_client.execute_query(query, {"action": action, "details": details, "error_code": error_code})
        except Exception as e:
            logger.error(f"Audit log error failed: {e}")
            raise

    async def update_status(self, agent_run_id: str, status: str) -> None:
        try:
            query = (
                "UPDATE qo_agent_run SET status = :status WHERE agent_run_id = :agent_run_id"
            )
            await self.sql_client.execute_query(query, {"status": status, "agent_run_id": agent_run_id})
        except Exception as e:
            logger.error(f"Failed to update agent run status: {e}")
            raise

class HITLEscalator(ServiceBase):
    """
    Triggers HITL API for manual intervention on business rule exceptions.
    """
    def __init__(self, hitl_api_url: str, audit_logger: AuditLogger):
        self.hitl_api_url = hitl_api_url
        self.audit_logger = audit_logger

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2), retry=retry_if_exception_type(Exception))
    async def escalate(self, task_type: str, reason: str, context: Dict[str, Any]) -> str:
        import requests
        try:
            payload = {
                "task_type": task_type,
                "reason": reason,
                "context": context
            }
            response = requests.post(self.hitl_api_url, json=payload, timeout=10)
            if response.status_code == 200:
                await self.audit_logger.log_action("HITL_Escalation", "SUCCESS", f"Escalated: {reason}")
                return response.json().get("hitl_task_id", "unknown")
            else:
                await self.audit_logger.log_error("HITL_Escalation", "ERR_HITL_API", f"Failed: {response.text}")
                raise Exception(f"HITL API failed: {response.text}")
        except Exception as e:
            logger.error(f"HITL escalation failed: {e}")
            raise

class ATPChecker(ServiceBase):
    """
    Performs ATP availability checks with retry logic and HITL escalation.
    """
    def __init__(self, atp_api_url: str, hitl_escalator: HITLEscalator):
        self.atp_api_url = atp_api_url
        self.hitl_escalator = hitl_escalator

    @retry(stop=stop_after_attempt(12), wait=wait_exponential(multiplier=5, min=5, max=300), retry=retry_if_exception_type(Exception))
    async def check_atp(self, rfq_id: str) -> str:
        import requests
        try:
            response = requests.get(f"{self.atp_api_url}/atp/{rfq_id}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("atp_available", False):
                    return "available"
                else:
                    raise Exception("ATP not available yet")
            else:
                raise Exception(f"ATP API error: {response.text}")
        except Exception as e:
            logger.warning(f"ATP check failed for RFQ {rfq_id}: {e}")
            raise

    async def escalate_if_unavailable(self, rfq_id: str, context: Dict[str, Any]) -> str:
        return await self.hitl_escalator.escalate(
            task_type="ATP_Unavailable",
            reason="ATP unavailable after retries",
            context=context
        )

class EBSQuoteCreator(ServiceBase):
    """
    Creates quote header and lines in EBS, maps fields, validates responses.
    """
    def __init__(self, ebs_api_url: str, audit_logger: AuditLogger, hitl_escalator: HITLEscalator):
        self.ebs_api_url = ebs_api_url
        self.audit_logger = audit_logger
        self.hitl_escalator = hitl_escalator

    async def create_quote(self, quote_payload: Dict[str, Any]) -> str:
        import requests
        try:
            response = requests.post(f"{self.ebs_api_url}/quotes", json=quote_payload, timeout=15)
            if response.status_code == 201:
                data = response.json()
                await self.audit_logger.log_action("Create_Quote", "SUCCESS", f"Quote {data.get('quote_number')}")
                return data.get("quote_number")
            else:
                await self.audit_logger.log_error("Create_Quote", "ERR_QUOTE_CREATION_FAILED", response.text)
                await self.hitl_escalator.escalate(
                    task_type="Quote_Creation_Failed",
                    reason="Quote creation failed",
                    context={"payload": quote_payload, "response": response.text}
                )
                raise Exception("Quote creation failed")
        except Exception as e:
            logger.error(f"Quote creation failed: {e}")
            raise

class LeadTimeValidator(ServiceBase):
    """
    Validates and calculates lead times, escalates to HITL if unresolved.
    """
    def __init__(self, lead_time_api_url: str, hitl_escalator: HITLEscalator):
        self.lead_time_api_url = lead_time_api_url
        self.hitl_escalator = hitl_escalator

    async def validate_lead_time(self, quote_response: Dict[str, Any]) -> Dict[str, Any]:
        import requests
        missing_lead_time = [line for line in quote_response.get("lines", []) if not line.get("lead_time")]
        if not missing_lead_time:
            return {"status": "complete", "details": "All lead times present"}
        try:
            response = requests.post(
                f"{self.lead_time_api_url}/calculate",
                json={"lines": missing_lead_time},
                timeout=10
            )
            if response.status_code == 200:
                updated = response.json()
                return {"status": "updated", "details": updated}
            else:
                await self.hitl_escalator.escalate(
                    task_type="LeadTime_Missing",
                    reason="Lead time missing after calculation",
                    context={"quote_response": quote_response, "api_response": response.text}
                )
                raise Exception("Lead time calculation failed")
        except Exception as e:
            logger.error(f"Lead time validation failed: {e}")
            raise

class DiscountValidator(ServiceBase):
    """
    Validates discounts against TDE recommendations, escalates to HITL if exceeded.
    """
    def __init__(self, tde_api_url: str, hitl_escalator: HITLEscalator):
        self.tde_api_url = tde_api_url
        self.hitl_escalator = hitl_escalator

    async def validate_discount(self, quote_response: Dict[str, Any], input_payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests
        try:
            requested_discount = input_payload.get("discount", 0)
            response = requests.post(
                f"{self.tde_api_url}/recommend",
                json={"lines": quote_response.get("lines", [])},
                timeout=10
            )
            if response.status_code == 200:
                tde_discount = response.json().get("recommended_discount", 0)
                if requested_discount > tde_discount:
                    await self.hitl_escalator.escalate(
                        task_type="Discount_Review",
                        reason="Discount exceeds TDE recommendation",
                        context={"requested_discount": requested_discount, "tde_discount": tde_discount}
                    )
                    return {"status": "escalated", "details": "Discount exceeds TDE"}
                else:
                    return {"status": "approved", "details": "Discount within TDE"}
            else:
                await self.hitl_escalator.escalate(
                    task_type="Discount_Review",
                    reason="TDE API failure",
                    context={"response": response.text}
                )
                raise Exception("TDE API failed")
        except Exception as e:
            logger.error(f"Discount validation failed: {e}")
            raise

class TaxExemptValidator(ServiceBase):
    """
    Validates tax exempt status, checks TOTAL_TAX, escalates to HITL if not applied.
    """
    def __init__(self, hitl_escalator: HITLEscalator):
        self.hitl_escalator = hitl_escalator

    async def validate_tax_exempt(self, input_payload: Dict[str, Any], quote_response: Dict[str, Any]) -> Dict[str, Any]:
        tax_exempt_required = input_payload.get("tax_exempt", False)
        total_tax = quote_response.get("TOTAL_TAX", None)
        if tax_exempt_required and (total_tax is None or float(total_tax) > 0):
            await self.hitl_escalator.escalate(
                task_type="Tax_Exempt_Review",
                reason="Tax exempt not applied",
                context={"input_payload": input_payload, "quote_response": quote_response}
            )
            return {"status": "escalated", "details": "Tax exempt not applied"}
        return {"status": "approved", "details": "Tax exempt validated"}

# =========================
# Input Validation Layer
# =========================
class InputValidator(ServiceBase):
    """
    Validates input payloads, agent state, and ensures data integrity before processing.
    """
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager

    def validate_payload(self, payload: Dict[str, Any]) -> bool:
        if not payload:
            raise ValueError("Input payload is empty.")
        if isinstance(payload, dict):
            if len(json.dumps(payload)) > 50000:
                raise ValueError("Input payload exceeds 50,000 character limit.")
        else:
            raise ValueError("Input payload must be a JSON object.")
        return True

# =========================
# LLM Integration Layer
# =========================
class LLMClient:
    """
    Handles LLM calls to OpenAI.
    """
    def __init__(self, api_key: str, model: str, temperature: float, max_tokens: int, system_prompt: str):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt

    async def chat(self, user_message: str) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_message}
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

# =========================
# Orchestration Layer
# =========================
class AgentOrchestrator:
    """
    Coordinates the end-to-end RFQ review and quote creation workflow.
    """
    def __init__(
        self,
        input_validator: InputValidator,
        audit_logger: AuditLogger,
        atp_checker: ATPChecker,
        ebs_quote_creator: EBSQuoteCreator,
        lead_time_validator: LeadTimeValidator,
        discount_validator: DiscountValidator,
        tax_exempt_validator: TaxExemptValidator,
        hitl_escalator: HITLEscalator,
        blob_client: AzureBlobClient,
        sql_client: AzureSQLClient,
        config_manager: ConfigManager,
        key_vault_client: KeyVaultClient,
        llm_client: LLMClient
    ):
        self.input_validator = input_validator
        self.audit_logger = audit_logger
        self.atp_checker = atp_checker
        self.ebs_quote_creator = ebs_quote_creator
        self.lead_time_validator = lead_time_validator
        self.discount_validator = discount_validator
        self.tax_exempt_validator = tax_exempt_validator
        self.hitl_escalator = hitl_escalator
        self.blob_client = blob_client
        self.sql_client = sql_client
        self.config_manager = config_manager
        self.key_vault_client = key_vault_client
        self.llm_client = llm_client

    async def process_rfq(
        self,
        input_payload: Dict[str, Any],
        agent_run_id: Optional[str],
        pipeline_run_id: Optional[str]
    ) -> Dict[str, Any]:
        try:
            # Validate input
            self.input_validator.validate_payload(input_payload)
            await self.audit_logger.log_action("Input_Validation", "SUCCESS", "Input validated")

            # Create audit entry for new run
            if not agent_run_id:
                # Insert RUNNING status entry in qo_agent_run
                query = (
                    "INSERT INTO qo_agent_run (pipeline_run_id, status, agent_name) "
                    "VALUES (:pipeline_run_id, 'RUNNING', 'Quote Creation')"
                )
                await self.sql_client.execute_query(query, {"pipeline_run_id": pipeline_run_id})
                await self.audit_logger.log_action("Agent_Run_Initialization", "SUCCESS", f"Pipeline: {pipeline_run_id}")

            # ATP Check
            rfq_id = input_payload.get("rfq_id")
            try:
                atp_status = await self.atp_checker.check_atp(rfq_id)
            except Exception:
                atp_status = "unavailable"
            if atp_status != "available":
                await self.audit_logger.log_error("ATP_Check", "ERR_ATP_TIMEOUT", "ATP unavailable after retries")
                hitl_task_id = await self.atp_checker.escalate_if_unavailable(rfq_id, {"rfq_id": rfq_id})
                return {
                    "success": False,
                    "error_type": "ATPUnavailable",
                    "error_code": "ERR_ATP_TIMEOUT",
                    "message": "ATP unavailable after retries. Escalated to HITL.",
                    "hitl_task_id": hitl_task_id,
                    "tips": "Check ATP API status or escalate to manual review."
                }

            # Quote Creation
            try:
                quote_number = await self.ebs_quote_creator.create_quote(input_payload)
            except Exception as e:
                await self.audit_logger.log_error("Quote_Creation", "ERR_QUOTE_CREATION_FAILED", str(e))
                return {
                    "success": False,
                    "error_type": "QuoteCreationFailed",
                    "error_code": "ERR_QUOTE_CREATION_FAILED",
                    "message": "Quote creation failed and escalated to HITL.",
                    "tips": "Review EBS API logs and escalate to manual review."
                }

            # Simulate fetching quote response (in real scenario, fetch from EBS)
            quote_response = {"quote_number": quote_number, "lines": input_payload.get("lines", []), "TOTAL_TAX": input_payload.get("TOTAL_TAX", 0)}

            # Lead Time Validation
            lead_time_result = await self.lead_time_validator.validate_lead_time(quote_response)
            if lead_time_result.get("status") == "escalated":
                await self.audit_logger.log_error("LeadTime_Validation", "ERR_LEAD_TIME_MISSING", "Lead time missing after calculation")
                return {
                    "success": False,
                    "error_type": "LeadTimeMissing",
                    "error_code": "ERR_LEAD_TIME_MISSING",
                    "message": "Lead time missing after calculation. Escalated to HITL.",
                    "tips": "Check Lead Time Calculator API or escalate to manual review."
                }

            # Discount Validation
            discount_result = await self.discount_validator.validate_discount(quote_response, input_payload)
            if discount_result.get("status") == "escalated":
                await self.audit_logger.log_error("Discount_Validation", "ERR_DISCOUNT_REVIEW", "Discount exceeds TDE")
                return {
                    "success": False,
                    "error_type": "DiscountReview",
                    "error_code": "ERR_DISCOUNT_REVIEW",
                    "message": "Discount exceeds TDE recommendation. Escalated to HITL.",
                    "tips": "Review discount policy or escalate to manual review."
                }

            # Tax Exempt Validation
            tax_result = await self.tax_exempt_validator.validate_tax_exempt(input_payload, quote_response)
            if tax_result.get("status") == "escalated":
                await self.audit_logger.log_error("TaxExempt_Validation", "ERR_TAX_EXEMPT", "Tax exempt not applied")
                return {
                    "success": False,
                    "error_type": "TaxExempt",
                    "error_code": "ERR_TAX_EXEMPT",
                    "message": "Tax exempt not applied. Escalated to HITL.",
                    "tips": "Review tax exempt status or escalate to manual review."
                }

            # Upload output to blob storage
            blob_path = f"output/{quote_number}.json"
            await self.blob_client.upload_blob(blob_path, quote_response)
            await self.audit_logger.log_action("Upload_Output", "SUCCESS", f"Output uploaded to {blob_path}")

            # Update agent run status
            if agent_run_id:
                await self.audit_logger.update_status(agent_run_id, "COMPLETED")

            return {
                "success": True,
                "quote_number": quote_number,
                "output_blob_path": blob_path,
                "message": "RFQ review and quote creation completed successfully."
            }
        except Exception as e:
            logger.error(f"process_rfq failed: {e}")
            await self.audit_logger.log_error("Process_RFQ", "ERR_PROCESS_RFQ", str(e))
            return {
                "success": False,
                "error_type": "ProcessRFQ",
                "error_code": "ERR_PROCESS_RFQ",
                "message": f"RFQ processing failed: {e}",
                "tips": "Check logs for details and escalate to HITL if needed."
            }

    async def resume_from_agent_run_id(
        self,
        agent_run_id: str,
        pipeline_run_id: Optional[str]
    ) -> Dict[str, Any]:
        try:
            # Fetch parent output from blob storage
            blob_path = f"output/{agent_run_id}.json"
            try:
                parent_output = await self.blob_client.download_blob(blob_path)
            except Exception as e:
                await self.audit_logger.log_error("Resume_From_Agent_Run", "ERR_DATA_PARSE", str(e))
                return {
                    "success": False,
                    "error_type": "DataParse",
                    "error_code": "ERR_DATA_PARSE",
                    "message": "Failed to fetch parent output from blob storage.",
                    "tips": "Ensure agent_run_id is correct and blob exists."
                }
            # Resume processing
            return await self.process_rfq(parent_output, agent_run_id, pipeline_run_id)
        except Exception as e:
            logger.error(f"resume_from_agent_run_id failed: {e}")
            await self.audit_logger.log_error("Resume_From_Agent_Run", "ERR_RESUME", str(e))
            return {
                "success": False,
                "error_type": "ResumeError",
                "error_code": "ERR_RESUME",
                "message": f"Resume from agent_run_id failed: {e}",
                "tips": "Check logs for details and escalate to HITL if needed."
            }

# =========================
# Persistence Layer
# =========================
# (Handled by AzureBlobClient and AzureSQLClient above)

# =========================
# Presentation Layer (FastAPI)
# =========================
class RFQInputModel(BaseModel):
    input_payload: Optional[dict] = Field(default=None, description="RFQ input payload as JSON object")
    agent_run_id: Optional[constr(strip_whitespace=True, min_length=1, max_length=128)] = None
    pipeline_run_id: Optional[constr(strip_whitespace=True, min_length=1, max_length=128)] = None

    @field_validator("input_payload")
    @classmethod
    def validate_input_payload(cls, v):
        if v is not None:
            if not isinstance(v, dict):
                raise ValueError("input_payload must be a JSON object")
            if len(json.dumps(v)) > 50000:
                raise ValueError("input_payload exceeds 50,000 character limit")
        return v

    @field_validator("agent_run_id")
    @classmethod
    def validate_agent_run_id(cls, v):
        if v is not None and not re.match(r"^[\w\-]+$", v):
            raise ValueError("agent_run_id contains invalid characters")
        return v

    @field_validator("pipeline_run_id")
    @classmethod
    def validate_pipeline_run_id(cls, v):
        if v is not None and not re.match(r"^[\w\-]+$", v):
            raise ValueError("pipeline_run_id contains invalid characters")
        return v

# =========================
# Main Agent Class
# =========================
class BaseAgent:
    pass

class RFQQuoteAgent(BaseAgent):
    """
    Main agent class for RFQ review and quote creation.
    """
    def __init__(self):
        # Load config
        ConfigManager.load_config()
        self.config_manager = ConfigManager

        # Key Vault
        self.key_vault_client = KeyVaultClient(
            vault_url=self.config_manager.get_config("AZURE_KEY_VAULT_URL")
        )

        # Retrieve DB connection string from Key Vault
        db_conn_secret = asyncio.get_event_loop().run_until_complete(
            self.key_vault_client.get_secret(self.config_manager.get_config("DB_CONNECTION_STRING_SECRET"))
        )

        # Integration clients
        self.sql_client = AzureSQLClient(db_connection_string=db_conn_secret)
        self.blob_client = AzureBlobClient(
            connection_string=self.config_manager.get_config("AZURE_STORAGE_CONNECTION_STRING"),
            container_name=self.config_manager.get_config("AZURE_STORAGE_CONTAINER_NAME")
        )

        # Audit logger
        self.audit_logger = AuditLogger(self.sql_client)

        # HITL escalator
        self.hitl_escalator = HITLEscalator(
            hitl_api_url=self.config_manager.get_config("HITL_API_URL"),
            audit_logger=self.audit_logger
        )

        # Service clients
        self.atp_checker = ATPChecker(
            atp_api_url=self.config_manager.get_config("API_BASE_URL"),
            hitl_escalator=self.hitl_escalator
        )
        self.ebs_quote_creator = EBSQuoteCreator(
            ebs_api_url=self.config_manager.get_config("API_BASE_URL"),
            audit_logger=self.audit_logger,
            hitl_escalator=self.hitl_escalator
        )
        self.lead_time_validator = LeadTimeValidator(
            lead_time_api_url=self.config_manager.get_config("API_BASE_URL"),
            hitl_escalator=self.hitl_escalator
        )
        self.discount_validator = DiscountValidator(
            tde_api_url=self.config_manager.get_config("API_BASE_URL"),
            hitl_escalator=self.hitl_escalator
        )
        self.tax_exempt_validator = TaxExemptValidator(
            hitl_escalator=self.hitl_escalator
        )
        self.input_validator = InputValidator(self.config_manager)

        # LLM client
        self.llm_client = LLMClient(
            api_key=self.config_manager.get_config("OPENAI_API_KEY"),
            model="gpt-4o",
            temperature=0.7,
            max_tokens=2000,
            system_prompt="You are the RFQ Review and Quote Creation Agent. Your role is to automate the review of RFQ data, validate ATP availability, create quotes in EBS, and orchestrate lead time, discount, and tax exempt checks. Ensure all actions are logged, exceptions are escalated to HITL, and all communications are professional and clear. Always retrieve credentials from Key Vault and static data from config files."
        )

        # Orchestrator
        self.orchestrator = AgentOrchestrator(
            input_validator=self.input_validator,
            audit_logger=self.audit_logger,
            atp_checker=self.atp_checker,
            ebs_quote_creator=self.ebs_quote_creator,
            lead_time_validator=self.lead_time_validator,
            discount_validator=self.discount_validator,
            tax_exempt_validator=self.tax_exempt_validator,
            hitl_escalator=self.hitl_escalator,
            blob_client=self.blob_client,
            sql_client=self.sql_client,
            config_manager=self.config_manager,
            key_vault_client=self.key_vault_client,
            llm_client=self.llm_client
        )

    async def handle_rfq(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handles the RFQ review and quote creation process.
        """
        input_payload = request_data.get("input_payload")
        agent_run_id = request_data.get("agent_run_id")
        pipeline_run_id = request_data.get("pipeline_run_id")
        if agent_run_id:
            return await self.orchestrator.resume_from_agent_run_id(agent_run_id, pipeline_run_id)
        elif input_payload:
            return await self.orchestrator.process_rfq(input_payload, agent_run_id, pipeline_run_id)
        else:
            return {
                "success": False,
                "error_type": "InputValidation",
                "error_code": "ERR_INPUT_MISSING",
                "message": "Either input_payload or agent_run_id must be provided.",
                "tips": "Provide a valid input_payload for new run or agent_run_id for resume."
            }

# =========================
# FastAPI App
# =========================
app = FastAPI(
    title="RFQ Review and Quote Creation Agent",
    description="Automates RFQ review, ATP check, quote creation, and business rule validation.",
    version="1.0.0"
)

# CORS (if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_instance: Optional[RFQQuoteAgent] = None

@app.on_event("startup")
async def startup_event():
    global agent_instance
    agent_instance = RFQQuoteAgent()
    logger.info("RFQQuoteAgent initialized.")

@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "error_type": "ValidationError",
            "error_code": "ERR_VALIDATION",
            "message": "Input validation failed.",
            "details": exc.errors(),
            "tips": "Check your JSON structure, field types, and required fields."
        }
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error_type": "HTTPException",
            "error_code": f"ERR_HTTP_{exc.status_code}",
            "message": exc.detail,
            "tips": "Check your request and try again."
        }
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error_type": "InternalServerError",
            "error_code": "ERR_INTERNAL",
            "message": "An unexpected error occurred.",
            "tips": "Check logs for details and ensure your JSON is well-formed."
        }
    )

@app.post("/rfq/process", response_model=Dict[str, Any])
async def process_rfq_endpoint(input_data: RFQInputModel):
    """
    Endpoint to process RFQ review and quote creation.
    """
    try:
        data = input_data.model_dump()
        result = await agent_instance.handle_rfq(data)
        return result
    except ValidationError as ve:
        logger.error(f"Validation error: {ve}")
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        logger.error(f"Processing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rfq/resume", response_model=Dict[str, Any])
async def resume_rfq_endpoint(input_data: RFQInputModel):
    """
    Endpoint to resume RFQ processing from agent_run_id.
    """
    try:
        if not input_data.agent_run_id:
            raise HTTPException(status_code=400, detail="agent_run_id is required for resume.")
        data = input_data.model_dump()
        result = await agent_instance.handle_rfq(data)
        return result
    except ValidationError as ve:
        logger.error(f"Validation error: {ve}")
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        logger.error(f"Resume error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rfq/llm", response_model=Dict[str, Any])
async def llm_endpoint(request: Request):
    """
    Endpoint to interact with LLM for professional notifications or status updates.
    """
    try:
        body = await request.json()
        user_message = body.get("user_message", "")
        if not user_message or not isinstance(user_message, str):
            raise HTTPException(status_code=400, detail="user_message must be a non-empty string.")
        response = await agent_instance.llm_client.chat(user_message)
        return {"success": True, "llm_response": response}
    except json.JSONDecodeError as jde:
        logger.error(f"Malformed JSON: {jde}")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error_type": "MalformedJSON",
                "error_code": "ERR_JSON_PARSE",
                "message": "Malformed JSON in request body.",
                "tips": "Ensure your JSON is valid. Common issues: missing quotes, trailing commas, or unescaped characters."
            }
        )
    except Exception as e:
        logger.error(f"LLM endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# =========================
# Main Execution Block
# =========================
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting RFQ Review and Quote Creation Agent API...")
    uvicorn.run("agent:app", host="0.0.0.0", port=8080, reload=False)
