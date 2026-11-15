from __future__ import annotations

import json

import frappe
from google.cloud import bigquery
from google.oauth2 import service_account


BIGQUERY_GCS_SETTINGS_NAME = "dummy_central-phalanx-297915-Authenticated"


def get_bigquery_client(gcs_settings_name: str = BIGQUERY_GCS_SETTINGS_NAME):
    settings = frappe.get_doc("GCS Settings", gcs_settings_name)
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(settings.credentials_json)
    )
    return bigquery.Client(project=settings.project_id, credentials=credentials)


def run_query(
    client,
    query: str,
    query_parameters: list[tuple[str, str, object]] | None = None,
    maximum_bytes_billed: int | None = None,
):
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, parameter_type, value)
            for name, parameter_type, value in query_parameters or []
        ],
        maximum_bytes_billed=maximum_bytes_billed,
    )
    return client.query(query, job_config=job_config).result()
