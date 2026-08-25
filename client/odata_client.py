"""
SuccessFactors OData v2 API Client.
Provides resilient, authenticated OData communication with automated retries,
pagination, error unwrapping, payload construction, and post-upsert verification.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Generator, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from client.auth import BaseAuthProvider
from client.exceptions import (
    SFODataError,
    SFPayloadValidationError,
    SFVerificationError,
)
from config.settings import AppConfig
from core.models import CandidateResumeData, JobApplicationData

logger = logging.getLogger("ResumeShifter.ODataClient")


def parse_sf_odata_datetime(date_str: Any) -> Optional[datetime]:
    """
    Parse SAP SuccessFactors OData v2 date representations:
    1. /Date(1577836800000)/ or /Date(1577836800000+0000)/ (Epoch ms)
    2. ISO-8601 strings: 2026-01-01T12:00:00Z
    """
    if not date_str or not isinstance(date_str, str):
        return None

    epoch_match = re.search(r"/Date\((\d+)([+-]\d+)?\)/", date_str)
    if epoch_match:
        ms = int(epoch_match.group(1))
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    try:
        # Standard ISO 8601
        clean_str = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_str)
    except (ValueError, TypeError):
        return None


def format_sf_odata_filter_datetime(dt: datetime) -> str:
    """Format Python datetime for SAP OData v2 $filter: datetime'YYYY-MM-DDTHH:MM:SS'."""
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return f"datetime'{utc_dt.strftime('%Y-%m-%dT%H:%M:%S')}'"


class SFODataClient:
    """Production client for SAP SuccessFactors Recruiting OData v2 API."""

    def __init__(self, config: AppConfig, auth_provider: BaseAuthProvider) -> None:
        self.config = config
        self.auth_provider = auth_provider
        self.session = self._create_resilient_session()

    def _create_resilient_session(self) -> requests.Session:
        """Create a requests session with HTTP connection pooling and exponential retries."""
        session = requests.Session()
        retries = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=10,
            pool_maxsize=20,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """
        Execute an HTTP request against the SuccessFactors OData API.
        Handles token attachment, 401 retry, and OData error unwrapping.
        """
        url = self.config.get_api_endpoint(path)
        headers = self.auth_provider.get_auth_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        # SuccessFactors custom header to bypass some caching if supported
        headers["DataServiceVersion"] = "2.0"

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers,
                timeout=self.config.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SFODataError(f"Network error during {method} {url}: {exc}") from exc

        # Handle token expiration (HTTP 401)
        if response.status_code == 401 and retry_on_401:
            logger.warning("Received HTTP 401 Unauthorized from %s. Invalidating auth cache and retrying...", url)
            self.auth_provider.invalidate()
            return self._request(
                method=method,
                path=path,
                params=params,
                json_data=json_data,
                retry_on_401=False,
            )

        if not response.ok:
            self._handle_error_response(response, url)

        # Handle HTTP 204 No Content
        if response.status_code == 204 or not response.content.strip():
            return {}

        try:
            return response.json()
        except Exception as exc:
            raise SFODataError(
                f"Failed to parse JSON response from {url} (HTTP {response.status_code}): {response.text[:200]}"
            ) from exc

    def _handle_error_response(self, response: requests.Response, url: str) -> None:
        """Extract and format SAP SuccessFactors OData error structures."""
        status_code = response.status_code
        sf_code = "UNKNOWN"
        sf_message = response.text

        try:
            err_json = response.json()
            if "error" in err_json:
                error_obj = err_json["error"]
                sf_code = error_obj.get("code", sf_code)
                msg_val = error_obj.get("message")
                if isinstance(msg_val, dict):
                    sf_message = msg_val.get("value", str(msg_val))
                elif isinstance(msg_val, str):
                    sf_message = msg_val
        except Exception:
            pass

        logger.error(
            "OData API call failed HTTP %d on %s: [%s] %s",
            status_code,
            url,
            sf_code,
            sf_message,
        )
        raise SFODataError(
            message=f"OData Request Failed ({status_code}): {sf_message}",
            status_code=status_code,
            error_code=sf_code,
            sf_message=sf_message,
        )

    def discover_applications_page(
        self,
        watermark: Optional[datetime] = None,
        top: int = 1000,
        skip: int = 0,
    ) -> list[JobApplicationData]:
        """
        Query a single page of JobApplications created/modified since watermark.
        Uses $expand=Cust_Candidate_Resume to inspect current target state.
        """
        params: dict[str, Any] = {
            "$top": top,
            "$skip": skip,
            "$expand": "Cust_Candidate_Resume",
            "$select": "applicationId,candidateId,applicationDate,Cust_Candidate_Resume/attachmentId,Cust_Candidate_Resume/fileName",
            "$format": "json",
        }

        if watermark:
            filter_date_str = format_sf_odata_filter_datetime(watermark)
            params["$filter"] = f"applicationDate gt {filter_date_str}"

        logger.debug(
            "Discovering JobApplications page top=%d, skip=%d, watermark=%s",
            top,
            skip,
            watermark,
        )
        raw_res = self._request("GET", "JobApplication", params=params)

        # Unpack OData results
        results = []
        d_block = raw_res.get("d", {})
        if isinstance(d_block, dict):
            results = d_block.get("results", [])
        elif isinstance(d_block, list):
            results = d_block

        applications: list[JobApplicationData] = []
        for item in results:
            app_id = str(item.get("applicationId", ""))
            cand_id = str(item.get("candidateId", ""))
            app_date = parse_sf_odata_datetime(item.get("applicationDate"))

            cust_resume = item.get("Cust_Candidate_Resume")
            att_id = None
            file_name = None
            raw_cust = None

            if isinstance(cust_resume, dict):
                raw_cust = cust_resume
                if "results" in cust_resume and isinstance(cust_resume["results"], list):
                    if len(cust_resume["results"]) > 0:
                        first_att = cust_resume["results"][0]
                        if isinstance(first_att, dict):
                            att_id = str(first_att.get("attachmentId", ""))
                            file_name = first_att.get("fileName")
                else:
                    att_id = str(cust_resume.get("attachmentId", ""))
                    file_name = cust_resume.get("fileName")

            applications.append(
                JobApplicationData(
                    application_id=app_id,
                    candidate_id=cand_id,
                    application_date=app_date,
                    custom_resume_attachment_id=att_id if att_id and att_id != "None" else None,
                    custom_resume_file_name=file_name,
                    custom_resume_raw=raw_cust,
                )
            )

        return applications

    def get_application(
        self,
        application_id: str,
        expand_cust_resume: bool = True,
    ) -> JobApplicationData:
        """Retrieve a single JobApplication by ID."""
        params: dict[str, Any] = {"$format": "json"}
        if expand_cust_resume:
            params["$expand"] = "Cust_Candidate_Resume"

        path = f"JobApplication({application_id})"
        raw_res = self._request("GET", path, params=params)

        data = raw_res.get("d", raw_res)
        if not data:
            raise SFODataError(f"JobApplication({application_id}) not found.")

        app_id = str(data.get("applicationId", application_id))
        cand_id = str(data.get("candidateId", ""))
        app_date = parse_sf_odata_datetime(data.get("applicationDate"))

        cust_resume = data.get("Cust_Candidate_Resume")
        att_id = None
        file_name = None
        raw_cust = None

        if isinstance(cust_resume, dict):
            raw_cust = cust_resume
            if "results" in cust_resume and isinstance(cust_resume["results"], list):
                if len(cust_resume["results"]) > 0:
                    first_att = cust_resume["results"][0]
                    if isinstance(first_att, dict):
                        att_id = str(first_att.get("attachmentId", ""))
                        file_name = first_att.get("fileName")
            else:
                att_id = str(cust_resume.get("attachmentId", ""))
                file_name = cust_resume.get("fileName")

        return JobApplicationData(
            application_id=app_id,
            candidate_id=cand_id,
            application_date=app_date,
            custom_resume_attachment_id=att_id if att_id and att_id != "None" else None,
            custom_resume_file_name=file_name,
            custom_resume_raw=raw_cust,
        )

    def get_candidate_resume(self, candidate_id: str) -> CandidateResumeData:
        """
        Retrieve Candidate profile with expanded resume attachment object.
        GET Candidate(<candidateId>)?$expand=resume
        """
        path = f"Candidate({candidate_id})"
        params = {
            "$expand": "resume",
            "$select": "candidateId,resume/attachmentId,resume/fileName,resume/fileContent,resume/module",
            "$format": "json",
        }

        raw_res = self._request("GET", path, params=params)
        data = raw_res.get("d", raw_res)
        if not data:
            return CandidateResumeData(candidate_id=candidate_id)

        resume_obj = data.get("resume")
        if not isinstance(resume_obj, dict):
            return CandidateResumeData(candidate_id=candidate_id)

        att_id = str(resume_obj.get("attachmentId", ""))
        file_name = resume_obj.get("fileName")
        file_content = resume_obj.get("fileContent")
        module = resume_obj.get("module", "RECRUITING")

        return CandidateResumeData(
            candidate_id=candidate_id,
            attachment_id=att_id if att_id and att_id != "None" else None,
            file_name=file_name,
            file_content_b64=file_content,
            module=module if module else "RECRUITING",
        )

    def upsert_job_application_resume(
        self,
        application_id: str,
        file_name: str,
        file_content_b64: str,
        module: str = "RECRUITING",
    ) -> dict[str, Any]:
        """
        Perform direct resume copy into JobApplication.Cust_Candidate_Resume using POST /odata/v2/upsert.
        
        Payload Architecture:
        {
          "__metadata": { "type": "SFOData.JobApplication" },
          "applicationId": "<APPLICATION_ID>",
          "Cust_Candidate_Resume": {
            "__metadata": { "type": "SFOData.Attachment" },
            "fileContent": "<BASE64_CONTENT>",
            "fileName": "<FILE_NAME>",
            "module": "RECRUITING"
          }
        }
        """
        if not file_name or not file_content_b64:
            raise SFPayloadValidationError("Cannot upsert: fileName and fileContent must not be empty.")

        payload = {
            "__metadata": {
                "uri": f"JobApplication({application_id})",
                "type": "SFOData.JobApplication",
            },
            "applicationId": str(application_id),
            "Cust_Candidate_Resume": {
                "__metadata": {
                    "type": "SFOData.Attachment",
                },
                "fileContent": file_content_b64,
                "fileName": file_name,
                "module": module or "RECRUITING",
            },
        }

        logger.debug("Executing upsert for JobApplication %s with attachment '%s'", application_id, file_name)
        response_json = self._request("POST", "upsert", json_data=payload)

        # Validate upsert response
        # SF Upsert returns {"d": [{"httpCode": 200/204/201, "status": "OK", "message": ...}]}
        d_res = response_json.get("d")
        if isinstance(d_res, list) and len(d_res) > 0:
            first_entry = d_res[0]
            http_code = first_entry.get("httpCode")
            status = first_entry.get("status")
            if http_code and http_code >= 400:
                err_msg = first_entry.get("message") or f"Upsert item returned error code {http_code}"
                raise SFODataError(
                    f"Upsert failed for application {application_id}: {err_msg}",
                    status_code=http_code,
                    sf_message=str(first_entry),
                )
            if status and status.upper() not in ["OK", "SUCCESS", "UPDATED", "INSERTED"]:
                raise SFODataError(
                    f"Upsert response returned non-OK status: {status}",
                    sf_message=str(first_entry),
                )

        return response_json

    def verify_job_application_resume(
        self,
        application_id: str,
        expected_file_name: str,
    ) -> JobApplicationData:
        """
        Verify that Cust_Candidate_Resume has been populated and matches expectations.
        GET JobApplication(<APPLICATION_ID>)?$expand=Cust_Candidate_Resume
        """
        app_data = self.get_application(application_id, expand_cust_resume=True)

        if not app_data.is_custom_resume_populated:
            raise SFVerificationError(
                f"Verification failed for application {application_id}: Cust_Candidate_Resume is still empty."
            )

        if expected_file_name:
            matching = False
            if app_data.custom_resume_file_name and app_data.custom_resume_file_name.strip() == expected_file_name.strip():
                matching = True
            elif app_data.custom_resume_raw and isinstance(app_data.custom_resume_raw, dict):
                results_list = app_data.custom_resume_raw.get("results", [])
                for att in results_list:
                    if isinstance(att, dict) and att.get("fileName") == expected_file_name:
                        matching = True
                        break

            if not matching and app_data.custom_resume_file_name:
                raise SFVerificationError(
                    f"Verification mismatch for application {application_id}: "
                    f"expected fileName '{expected_file_name}', found '{app_data.custom_resume_file_name}'."
                )

        return app_data
