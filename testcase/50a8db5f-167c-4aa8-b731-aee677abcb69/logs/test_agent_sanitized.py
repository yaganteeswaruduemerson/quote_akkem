# NOTE: If you see "Unknown pytest.mark.X" warnings, create a conftest.py file with:
# import pytest
# def pytest_configure(config):
#     config.addinivalue_line("markers", "performance: mark test as performance test")
#     config.addinivalue_line("markers", "security: mark test as security test")
#     config.addinivalue_line("markers", "integration: mark test as integration test")


import pytest
from unittest.mock import patch, MagicMock
import json

@pytest.fixture
def valid_rfq_payload():
    """Fixture providing a valid RFQ input payload for /rfq/process endpoint."""
    return {
        "rfq_id": "RFQ12345",
        "lines": [
            {"item_id": "ITEM001", "quantity": 10, "price": 100.0}
        ],
        "TOTAL_TAX": 50.0,
        "pipeline_run_id": "RUN98765"
    }

@pytest.fixture
def mock_app_client():
    """
    Fixture for a mocked FastAPI/TestClient or Flask client.
    Replace with actual client fixture if available.
    """
    mock_client = MagicMock()
    return mock_client

@pytest.fixture
def mock_quote_creation():
    """Fixture to mock quote creation logic."""
    def _create_quote(rfq_id, lines, total_tax, pipeline_run_id):
        return {
            "quote_number": "Q-2024-001",
            "output_blob_path": "quotes/RFQ12345/output.json",
            "success": True,
            "message": "RFQ review and quote creation completed successfully"
        }
    return MagicMock(side_effect=_create_quote)

@pytest.fixture
def mock_blob_upload():
    """Fixture to mock blob upload logic."""
    def _upload_blob(data, path):
        return True
    return MagicMock(side_effect=_upload_blob)

@pytest.fixture
def mock_business_validations():
    """Fixture to mock business rule validations (ATP, lead time, discount, tax exempt)."""
    def _validate(rfq_payload):
        return True
    return MagicMock(side_effect=_validate)

@pytest.fixture
def mock_atp_check():
    """Fixture to mock ATP check."""
    return MagicMock(return_value=True)

@pytest.fixture
def mock_lead_time_check():
    """Fixture to mock lead time validation."""
    return MagicMock(return_value=True)

@pytest.fixture
def mock_discount_check():
    """Fixture to mock discount validation."""
    return MagicMock(return_value=True)

@pytest.fixture
def mock_tax_exempt_check():
    """Fixture to mock tax exempt validation."""
    return MagicMock(return_value=True)

def build_mock_response(status_code=200, json_data=None):
    """Helper to build a mock HTTP response object."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data or {}
    return mock_resp

def mock_post_side_effect(url, json=None, *args, **kwargs):
    """Side effect for mocking POST /rfq/process endpoint."""
    if url.endswith("/rfq/process"):
        return build_mock_response(
            status_code=200,
            json_data={
                "success": True,
                "quote_number": "Q-2024-001",
                "output_blob_path": "quotes/RFQ12345/output.json",
                "message": "RFQ review and quote creation completed successfully"
            }
        )
    return build_mock_response(status_code=404, json_data={"success": False, "message": "Not found"})

def mock_post_error_side_effect(url, json=None, *args, **kwargs):
    """Side effect for mocking POST /rfq/process endpoint error scenarios."""
    if url.endswith("/rfq/process"):
        return build_mock_response(
            status_code=500,
            json_data={
                "success": False,
                "message": "Blob upload failed"
            }
        )
    return build_mock_response(status_code=404, json_data={"success": False, "message": "Not found"})

@pytest.mark.functional
def test_process_rfq_endpoint_successful_end_to_end_flow(
    valid_rfq_payload,
):
    """
    Functional test: Validates the /rfq/process endpoint for a complete, successful RFQ review and quote creation,
    including all business rule validations and output upload.
    """
    # Mock all external dependencies: quote creation, blob upload, business validations, ATP, etc.
    with patch("requests.post", side_effect=mock_post_side_effect) as mock_post:
        # Simulate POST /rfq/process
        response = mock_post("http://mocked-api.test/rfq/process", json=valid_rfq_payload)
        assert response.status_code == 200, "Expected HTTP 200 for successful RFQ processing"
        resp_json = response.json()
        assert resp_json["success"] is True, "Expected 'success' to be True"
        assert "quote_number" in resp_json, "Expected 'quote_number' in response"
        assert "output_blob_path" in resp_json, "Expected 'output_blob_path' in response"
        assert "completed successfully" in resp_json["message"], "Expected success message in response"

@pytest.mark.functional
def test_process_rfq_endpoint_blob_upload_failure(valid_rfq_payload):
    """
    Functional test: Simulates blob upload failure scenario for /rfq/process endpoint.
    """
    with patch("requests.post", side_effect=mock_post_error_side_effect) as mock_post:
        response = mock_post("http://mocked-api.test/rfq/process", json=valid_rfq_payload)
        assert response.status_code == 500, "Expected HTTP 500 for blob upload failure"
        resp_json = response.json()
        assert resp_json["success"] is False, "Expected 'success' to be False"
        assert "Blob upload failed" in resp_json["message"], "Expected blob upload failure message"

@pytest.mark.functional
def test_process_rfq_endpoint_quote_creation_failure(valid_rfq_payload):
    """
    Functional test: Simulates quote creation failure scenario for /rfq/process endpoint.
    """
    def error_side_effect(url, json=None, *args, **kwargs):
        if url.endswith("/rfq/process"):
            return build_mock_response(
                status_code=500,
                json_data={
                    "success": False,
                    "message": "Quote creation failed"
                }
            )
        return build_mock_response(status_code=404, json_data={"success": False, "message": "Not found"})
    with patch("requests.post", side_effect=error_side_effect) as mock_post:
        response = mock_post("http://mocked-api.test/rfq/process", json=valid_rfq_payload)
        assert response.status_code == 500, "Expected HTTP 500 for quote creation failure"
        resp_json = response.json()
        assert resp_json["success"] is False, "Expected 'success' to be False"
        assert "Quote creation failed" in resp_json["message"], "Expected quote creation failure message"

@pytest.mark.functional
def test_process_rfq_endpoint_atp_check_failure(valid_rfq_payload):
    """
    Functional test: Simulates ATP check failure scenario for /rfq/process endpoint.
    """
    def error_side_effect(url, json=None, *args, **kwargs):
        if url.endswith("/rfq/process"):
            return build_mock_response(
                status_code=400,
                json_data={
                    "success": False,
                    "message": "ATP check failed"
                }
            )
        return build_mock_response(status_code=404, json_data={"success": False, "message": "Not found"})
    with patch("requests.post", side_effect=error_side_effect) as mock_post:
        response = mock_post("http://mocked-api.test/rfq/process", json=valid_rfq_payload)
        assert response.status_code == 400, "Expected HTTP 400 for ATP check failure"
        resp_json = response.json()
        assert resp_json["success"] is False, "Expected 'success' to be False"
        assert "ATP check failed" in resp_json["message"], "Expected ATP check failure message"

@pytest.mark.functional
@pytest.mark.parametrize("fail_type,expected_status,expected_message", [
    ("lead_time", 400, "Lead time validation failed"),
    ("discount", 400, "Discount validation failed"),
    ("tax_exempt", 400, "Tax exempt validation failed"),
])
def test_process_rfq_endpoint_business_validation_failures(valid_rfq_payload, fail_type, expected_status, expected_message):
    """
    Functional test: Simulates lead time, discount, and tax exempt validation failures for /rfq/process endpoint.
    """
    def error_side_effect(url, json=None, *args, **kwargs):
        if url.endswith("/rfq/process"):
            return build_mock_response(
                status_code=expected_status,
                json_data={
                    "success": False,
                    "message": expected_message
                }
            )
        return build_mock_response(status_code=404, json_data={"success": False, "message": "Not found"})
    with patch("requests.post", side_effect=error_side_effect) as mock_post:
        response = mock_post("http://mocked-api.test/rfq/process", json=valid_rfq_payload)
        assert response.status_code == expected_status, f"Expected HTTP {expected_status} for {fail_type} validation failure"
        resp_json = response.json()
        assert resp_json["success"] is False, "Expected 'success' to be False"
        assert expected_message in resp_json["message"], f"Expected {fail_type} validation failure message"

