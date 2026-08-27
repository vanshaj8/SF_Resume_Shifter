"""
Mock SAP SuccessFactors OData v2 Service for offline testing, verification, and demonstration.
Accurately models entity schemas, write-once field behavior, and OData error responses.
Thread-safe for concurrent multi-worker testing.
"""

from __future__ import annotations

import base64
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from client.auth import MockAuthProvider
from client.exceptions import (
    SFODataError,
    SFPayloadValidationError,
    SFVerificationError,
)
from client.odata_client import CandidateResumeCache, SFODataClient
from config.settings import AppConfig
from core.models import CandidateResumeData, JobApplicationData


class MockSFDatabase:
    """In-memory thread-safe representation of SuccessFactors Recruiting database."""

    def __init__(self) -> None:
        self.candidates: Dict[str, Dict[str, Any]] = {}
        self.job_applications: Dict[str, Dict[str, Any]] = {}
        self.attachment_id_seq: int = 20000
        self._lock = threading.RLock()
        self._seed_default_data()

    def _seed_default_data(self) -> None:
        """Seed initial test data matching the business requirement scenario."""
        with self._lock:
            # Sample base64 PDF resume payloads
            sample_b64_a = base64.b64encode(b"%PDF-1.4 Mock Resume A for John Doe").decode("utf-8")
            sample_b64_b = base64.b64encode(b"%PDF-1.4 Mock Resume B (Updated) for John Doe").decode("utf-8")
            sample_b64_c = base64.b64encode(b"%PDF-1.4 Mock Resume C for Jane Smith").decode("utf-8")

            # Candidate 501: Has Resume_A.pdf on Day 1
            self.candidates["501"] = {
                "candidateId": "501",
                "firstName": "John",
                "lastName": "Doe",
                "resume": {
                    "attachmentId": "10001",
                    "fileName": "Resume_A.pdf",
                    "fileContent": sample_b64_a,
                    "module": "RECRUITING",
                },
            }

            # Candidate 502: Has Resume_C.pdf
            self.candidates["502"] = {
                "candidateId": "502",
                "firstName": "Jane",
                "lastName": "Smith",
                "resume": {
                    "attachmentId": "10002",
                    "fileName": "Resume_C.pdf",
                    "fileContent": sample_b64_c,
                    "module": "RECRUITING",
                },
            }

            # Candidate 503: Profile has NO resume uploaded
            self.candidates["503"] = {
                "candidateId": "503",
                "firstName": "Bob",
                "lastName": "NoResume",
                "resume": None,
            }

            # Candidate 504: Has corrupted resume (empty fileContent)
            self.candidates["504"] = {
                "candidateId": "504",
                "firstName": "Alice",
                "lastName": "EmptyContent",
                "resume": {
                    "attachmentId": "10004",
                    "fileName": "Corrupt.pdf",
                    "fileContent": "",
                    "module": "RECRUITING",
                },
            }

            # Job Application 1001: Day 1 (Candidate 501, Cust_Candidate_Resume is empty)
            self.job_applications["1001"] = {
                "applicationId": "1001",
                "candidateId": "501",
                "applicationDate": datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc),
                "Cust_Candidate_Resume": None,
            }

            # Job Application 1002: Pre-existing frozen snapshot (Cust_Candidate_Resume already populated)
            self.job_applications["1002"] = {
                "applicationId": "1002",
                "candidateId": "501",
                "applicationDate": datetime(2026, 7, 15, 9, 30, 0, tzinfo=timezone.utc),
                "Cust_Candidate_Resume": {
                    "attachmentId": "90001",
                    "fileName": "Historic_Frozen_Resume.pdf",
                    "module": "RECRUITING",
                },
            }

            # Job Application 1003: Candidate 503 (No resume)
            self.job_applications["1003"] = {
                "applicationId": "1003",
                "candidateId": "503",
                "applicationDate": datetime(2026, 8, 10, 14, 0, 0, tzinfo=timezone.utc),
                "Cust_Candidate_Resume": None,
            }

            # Job Application 1004: Candidate 502 (Empty Cust_Candidate_Resume)
            self.job_applications["1004"] = {
                "applicationId": "1004",
                "candidateId": "502",
                "applicationDate": datetime(2026, 8, 20, 11, 15, 0, tzinfo=timezone.utc),
                "Cust_Candidate_Resume": None,
            }


class MockSFODataClient(SFODataClient):
    """
    Subclasses SFODataClient to execute directly against MockSFDatabase in-memory,
    simulating OData v2 queries, filters, expansions, caching, and upserts.
    """

    def __init__(self, config: AppConfig, db: Optional[MockSFDatabase] = None) -> None:
        self.config = config
        self.auth_provider = MockAuthProvider()
        self.db = db or MockSFDatabase()
        self.fail_next_upsert = False
        self.fail_next_verification = False
        self.candidate_cache = CandidateResumeCache(max_size=config.candidate_cache_size)
        self._state_lock = threading.Lock()

    def discover_applications_page(
        self,
        watermark: Optional[datetime] = None,
        top: int = 1000,
        skip: int = 0,
    ) -> List[JobApplicationData]:
        with self.db._lock:
            # Filter applications by applicationDate > watermark
            filtered: List[Dict[str, Any]] = []
            for app in self.db.job_applications.values():
                app_date = app.get("applicationDate")
                if watermark and app_date:
                    if app_date <= watermark:
                        continue
                filtered.append(app)

            # Sort consistently by applicationDate or applicationId
            filtered.sort(key=lambda x: (x.get("applicationDate") or datetime.min.replace(tzinfo=timezone.utc), x["applicationId"]))

            # Slice according to skip and top
            paged = filtered[skip : skip + top]

            results: List[JobApplicationData] = []
            for item in paged:
                cust = item.get("Cust_Candidate_Resume")
                att_id = str(cust["attachmentId"]) if cust and cust.get("attachmentId") else None
                fn = cust.get("fileName") if cust else None

                results.append(
                    JobApplicationData(
                        application_id=item["applicationId"],
                        candidate_id=item["candidateId"],
                        application_date=item.get("applicationDate"),
                        custom_resume_attachment_id=att_id,
                        custom_resume_file_name=fn,
                        custom_resume_raw=cust,
                    )
                )
            return results

    def get_application(
        self,
        application_id: str,
        expand_cust_resume: bool = True,
    ) -> JobApplicationData:
        with self.db._lock:
            item = self.db.job_applications.get(str(application_id))
            if not item:
                raise SFODataError(f"JobApplication({application_id}) not found.", status_code=404)

            cust = item.get("Cust_Candidate_Resume")
            att_id = str(cust["attachmentId"]) if cust and cust.get("attachmentId") else None
            fn = cust.get("fileName") if cust else None

            return JobApplicationData(
                application_id=item["applicationId"],
                candidate_id=item["candidateId"],
                application_date=item.get("applicationDate"),
                custom_resume_attachment_id=att_id,
                custom_resume_file_name=fn,
                custom_resume_raw=cust,
            )

    def get_candidate_resume(self, candidate_id: str, use_cache: bool = True) -> CandidateResumeData:
        if use_cache:
            cached = self.candidate_cache.get(candidate_id)
            if cached is not None:
                return cached

        with self.db._lock:
            cand = self.db.candidates.get(str(candidate_id))
            if not cand:
                res_data = CandidateResumeData(candidate_id=candidate_id)
            else:
                res = cand.get("resume")
                if not res:
                    res_data = CandidateResumeData(candidate_id=candidate_id)
                else:
                    res_data = CandidateResumeData(
                        candidate_id=candidate_id,
                        attachment_id=str(res.get("attachmentId", "")),
                        file_name=res.get("fileName"),
                        file_content_b64=res.get("fileContent"),
                        module=res.get("module", "RECRUITING"),
                    )

            if use_cache:
                self.candidate_cache.put(candidate_id, res_data)
            return res_data

    def upsert_job_application_resume(
        self,
        application_id: str,
        file_name: str,
        file_content_b64: str,
        module: str = "RECRUITING",
    ) -> dict[str, Any]:
        with self._state_lock:
            if self.fail_next_upsert:
                self.fail_next_upsert = False
                raise SFODataError("Simulated OData Upsert Failure: Server Internal Error", status_code=500)

        if not file_name or not file_content_b64:
            raise SFPayloadValidationError("Upsert rejected: fileName and fileContent required.")

        with self.db._lock:
            app = self.db.job_applications.get(str(application_id))
            if not app:
                raise SFODataError(f"JobApplication({application_id}) does not exist.", status_code=404)

            # Simulate creation of the internal attachment snapshot
            self.db.attachment_id_seq += 1
            new_att_id = str(self.db.attachment_id_seq)

            app["Cust_Candidate_Resume"] = {
                "attachmentId": new_att_id,
                "fileName": file_name,
                "fileContent": file_content_b64,
                "module": module,
            }

            return {
                "d": [
                    {
                        "key": f"JobApplication/applicationId={application_id}",
                        "status": "OK",
                        "editStatus": "UPDATED",
                        "message": None,
                        "index": 0,
                        "httpCode": 200,
                    }
                ]
            }

    def verify_job_application_resume(
        self,
        application_id: str,
        expected_file_name: str,
    ) -> JobApplicationData:
        with self._state_lock:
            if self.fail_next_verification:
                self.fail_next_verification = False
                raise SFVerificationError("Simulated Verification Mismatch: attachment missing.")

        return super().verify_job_application_resume(application_id, expected_file_name)


def create_mock_sf_client(config: AppConfig, db: Optional[MockSFDatabase] = None) -> MockSFODataClient:
    """Factory helper to create a mock client."""
    return MockSFODataClient(config=config, db=db)
