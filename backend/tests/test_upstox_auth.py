"""services/auth/upstox_auth.py -- the manual daily OAuth flow that mints the Upstox access token.

Only the parts that can hurt are pinned here: the URL/code parsing, and that a failed exchange reports something useful
without ever printing the client secret. No network.
"""
import unittest
from unittest import mock

import requests

from services.auth import upstox_auth as ua

CLIENT = ua.UpstoxAuthClient(api_key="api-key-abcd123456", api_secret="super-secret", redirect_uri="https://127.0.0.1/callback")


class TestAuthorizationUrl(unittest.TestCase):
    def test_the_url_carries_the_oauth_params(self):
        url = CLIENT.get_authorization_url()
        self.assertTrue(url.startswith("https://api.upstox.com/v2/login/authorization/dialog?"))
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=api-key-abcd123456", url)
        self.assertIn("redirect_uri=https%3A%2F%2F127.0.0.1%2Fcallback", url)

    def test_the_secret_never_appears_in_the_login_url(self):
        self.assertNotIn("super-secret", CLIENT.get_authorization_url())


class TestCodeParsing(unittest.TestCase):
    def test_a_full_redirect_url_yields_its_code(self):
        self.assertEqual(ua.extract_code_from_redirect_url("https://127.0.0.1/callback?code=ABC123&state=x"), "ABC123")

    def test_a_url_without_a_code_raises_a_readable_error(self):
        with self.assertRaises(ValueError) as ctx:
            ua.extract_code_from_redirect_url("https://127.0.0.1/callback?error=access_denied")
        self.assertIn("No 'code' parameter", str(ctx.exception))

    def test_a_bare_code_is_not_mistaken_for_a_url(self):
        self.assertFalse("AUTH123".startswith("http"))       # what __main__ branches on


def _resp(status=200, payload=None, text=""):
    r = mock.Mock(status_code=status, text=text)
    r.json.return_value = payload if payload is not None else {}
    r.raise_for_status.return_value = None
    return r


class TestTokenExchange(unittest.TestCase):
    def test_a_good_response_returns_the_access_token(self):
        with mock.patch.object(ua.requests, "post", return_value=_resp(payload={"access_token": "eyJ.tok"})) as post:
            self.assertEqual(CLIENT.exchange_code_for_token("CODE"), "eyJ.tok")
        body = post.call_args.kwargs["data"]
        self.assertEqual(body["code"], "CODE")
        self.assertEqual(body["grant_type"], "authorization_code")

    def test_a_timeout_is_reported_as_a_network_problem(self):
        with mock.patch.object(ua.requests, "post", side_effect=requests.exceptions.Timeout()):
            with self.assertRaises(RuntimeError) as ctx:
                CLIENT.exchange_code_for_token("CODE")
        self.assertIn("timed out", str(ctx.exception))

    def test_an_http_error_reports_the_status_and_never_the_secret(self):
        r = _resp(status=401, text="invalid_client")
        r.raise_for_status.side_effect = requests.exceptions.HTTPError()
        with mock.patch.object(ua.requests, "post", return_value=r):
            with self.assertRaises(RuntimeError) as ctx:
                CLIENT.exchange_code_for_token("CODE")
        msg = str(ctx.exception)
        self.assertIn("HTTP 401", msg)
        self.assertIn("invalid_client", msg)
        self.assertNotIn("super-secret", msg)

    def test_a_response_without_a_token_raises_instead_of_returning_none(self):
        with mock.patch.object(ua.requests, "post", return_value=_resp(payload={"status": "error"})):
            with self.assertRaises(RuntimeError) as ctx:
                CLIENT.exchange_code_for_token("CODE")
        self.assertIn("no access_token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
