import asyncio
import contextlib
import io
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app.api import emqx
from app.core.errors import ServiceError


def request(body=b'{}', key=None):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    headers = [(b"idempotency-key", key.encode())] if key is not None else []
    return Request({"type": "http", "headers": headers}, receive)


class IngestionTransportTests(unittest.TestCase):
    def test_wave_and_gps_service_errors_keep_http_status_and_detail(self):
        for endpoint, method in ((emqx.emqx_webhook, "process_wave"), (emqx.emqx_gps, "process_gps")):
            for status in (400, 409, 500):
                with self.subTest(endpoint=method, status=status):
                    with patch.object(emqx.ingestion, method, side_effect=ServiceError(status, "existing detail")):
                        with self.assertRaises(HTTPException) as caught:
                            asyncio.run(endpoint(request()))
                    self.assertEqual(caught.exception.status_code, status)
                    self.assertEqual(caught.exception.detail, "existing detail")

    def test_existing_idempotency_header_is_passed_unchanged(self):
        with patch.object(emqx.ingestion, "process_wave", return_value={"status": "ok"}) as process:
            result = asyncio.run(emqx.emqx_webhook(request(b'{"clientid":"D1"}', "event-1")))
        self.assertEqual(result, {"status": "ok"})
        process.assert_called_once_with({"clientid": "D1"}, "event-1")

    def test_invalid_header_remains_400_and_does_not_call_service(self):
        with patch.object(emqx.ingestion, "process_wave") as process:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(emqx.emqx_webhook(request(key="invalid key")))
        self.assertEqual(caught.exception.status_code, 400)
        process.assert_not_called()

    def test_json_error_handling_is_unchanged(self):
        for body in (b'[]', b'null', b'{broken', b'\xff'):
            with self.subTest(body=body), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(emqx.emqx_webhook(request(body)))
                self.assertEqual(caught.exception.status_code, 400)

    def test_readiness_service_error_is_still_http_503(self):
        with patch.object(emqx.ingestion, "check_readiness", side_effect=ServiceError(503, "not ready")):
            with self.assertRaises(HTTPException) as caught:
                emqx.ingestion_health()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, "not ready")
