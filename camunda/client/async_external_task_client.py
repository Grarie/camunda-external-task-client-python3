import logging
from http import HTTPStatus

import httpx

from camunda.client.engine_client import ENGINE_LOCAL_BASE_URL
from camunda.utils.log_utils import log_with_context
from camunda.utils.response_utils import raise_exception_if_not_ok
from camunda.utils.utils import str_to_list
from camunda.utils.auth_basic import AuthBasic, obfuscate_password
from camunda.utils.auth_bearer import AuthBearer
from camunda.variables.variables import Variables

logger = logging.getLogger(__name__)


class AsyncExternalTaskClient:
    default_config = {
        "maxConcurrentTasks": 10,  # Number of concurrent tasks you can process
        "lockDuration": 300000,  # in milliseconds
        "asyncResponseTimeout": 30000,
        "retries": 3,
        "retryTimeout": 300000,
        "httpTimeoutMillis": 30000,
        "timeoutDeltaMillis": 5000,
        "includeExtensionProperties": True,  # enables Camunda Extension Properties
        "deserializeValues": True,  # deserialize values when fetch a task by default
        "usePriority": False,
        "sorting": None
    }

    def __init__(self, worker_id, engine_base_url=ENGINE_LOCAL_BASE_URL, config=None):
        config = config if config is not None else {}
        self.worker_id = worker_id
        self.external_task_base_url = engine_base_url + "/external-task"
        self.config = type(self).default_config.copy()
        self.config.update(config)
        self.is_debug = config.get('isDebug', False)
        self.http_timeout_seconds = self.config.get('httpTimeoutMillis') / 1000
        self._log_with_context(f"Created External Task client with config: {obfuscate_password(self.config)}")

    def get_fetch_and_lock_url(self):
        return f"{self.external_task_base_url}/fetchAndLock"

    async def fetch_and_lock(self, topic_names, process_variables=None, variables=None):
        url = self.get_fetch_and_lock_url()
        body = {
            "workerId": str(self.worker_id),  # convert to string to make it JSON serializable
            "maxTasks": 1,
            "topics": self._get_topics(topic_names, process_variables, variables),
            "asyncResponseTimeout": self.config["asyncResponseTimeout"],
            "usePriority": self.config["usePriority"],
            "sorting": self.config["sorting"]
        }

        if self.is_debug:
            self._log_with_context(f"Trying to fetch and lock with request payload: {body}")
        http_timeout_seconds = self.__get_fetch_and_lock_http_timeout_seconds()

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=self._get_headers(), json=body, timeout=http_timeout_seconds)
        raise_exception_if_not_ok(response)

        resp_json = response.json()
        if self.is_debug:
            self._log_with_context(f"Fetch and lock response JSON: {resp_json} for request: {body}")
        return resp_json

    def __get_fetch_and_lock_http_timeout_seconds(self):
        # Use HTTP timeout slightly more than async response / long polling timeout
        return (self.config["timeoutDeltaMillis"] + self.config["asyncResponseTimeout"]) / 1000

    def _get_topics(self, topic_names, process_variables, variables):
        topics = []
        for topic in str_to_list(topic_names):
            topics.append({
                "topicName": topic,
                "lockDuration": self.config["lockDuration"],
                "processVariables": process_variables if process_variables else {},
                # Enables Camunda Extension Properties
                "includeExtensionProperties": self.config.get("includeExtensionProperties") or False,
                "deserializeValues": self.config["deserializeValues"],
                "variables": variables
            })
        return topics

    async def complete(self, task_id, global_variables, local_variables=None):
        url = self.get_task_complete_url(task_id)

        body = {
            "workerId": self.worker_id,
            "variables": Variables.format(global_variables),
            "localVariables": Variables.format(local_variables)
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=self._get_headers(), json=body, timeout=self.http_timeout_seconds)
        raise_exception_if_not_ok(response)
        return response.status_code == HTTPStatus.NO_CONTENT

    def get_task_complete_url(self, task_id):
        return f"{self.external_task_base_url}/{task_id}/complete"

    async def failure(self, task_id, error_message, error_details, retries, retry_timeout):
        url = self.get_task_failure_url(task_id)
        logger.info(f"Setting retries to: {retries} for task: {task_id}")
        body = {
            "workerId": self.worker_id,
            "errorMessage": error_message,
            "retries": retries,
            "retryTimeout": retry_timeout,
        }
        if error_details:
            body["errorDetails"] = error_details

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=self._get_headers(), json=body, timeout=self.http_timeout_seconds)
        raise_exception_if_not_ok(response)
        return response.status_code == HTTPStatus.NO_CONTENT

    def get_task_failure_url(self, task_id):
        return f"{self.external_task_base_url}/{task_id}/failure"

    async def bpmn_failure(self, task_id, error_code, error_message, variables=None):
        url = self.get_task_bpmn_error_url(task_id)

        body = {
            "workerId": self.worker_id,
            "errorCode": error_code,
            "errorMessage": error_message,
            "variables": Variables.format(variables),
        }

        if self.is_debug:
            self._log_with_context(f"Trying to report BPMN error with request payload: {body}")

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=self._get_headers(), json=body, timeout=self.http_timeout_seconds)
        response.raise_for_status()
        return response.status_code == HTTPStatus.NO_CONTENT

    def get_task_bpmn_error_url(self, task_id):
        return f"{self.external_task_base_url}/{task_id}/bpmnError"

    @property
    def auth_basic(self) -> dict:
        if not self.config.get("auth_basic") or not isinstance(self.config.get("auth_basic"), dict):
            return {}
        token = AuthBasic(**self.config.get("auth_basic").copy()).token
        return {"Authorization": token}

    @property
    def auth_bearer(self) -> dict:
        if not self.config.get("auth_bearer") or not isinstance(self.config.get("auth_bearer"), dict):
            return {}
        token = AuthBearer(access_token=self.config["auth_bearer"]).access_token
        return {"Authorization": token}

    def _get_headers(self):
        headers = {
            "Content-Type": "application/json"
        }
        if self.auth_basic:
            headers.update(self.auth_basic)
        if self.auth_bearer:
            headers.update(self.auth_bearer)
        return headers

    def _log_with_context(self, msg, log_level='info', **kwargs):
        context = {"WORKER_ID": self.worker_id}
        log_with_context(msg, context=context, log_level=log_level, **kwargs)
