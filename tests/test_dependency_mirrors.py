"""Offline tests for HTTPS mirror metadata, proxy safety, and bounded benchmarks."""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.dependency_manager import mirrors  # noqa: E402 - direct unittest entry point


class _Response:
    def __init__(self, body: bytes, url: str, *, on_read=None) -> None:
        self._body = BytesIO(body)
        self.url = url
        self.on_read = on_read
        self.read_bytes = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, size=-1):
        data = self._body.read(size)
        self.read_bytes += len(data)
        if self.on_read:
            self.on_read(data)
        return data


class _FakeOpener:
    def __init__(
        self,
        index_body: bytes,
        wheel_body: bytes,
        *,
        on_index_read=None,
        wheel_error=None,
        final_wheel_url=None,
    ) -> None:
        self.index_body = index_body
        self.wheel_body = wheel_body
        self.on_index_read = on_index_read
        self.wheel_error = wheel_error
        self.final_wheel_url = final_wheel_url
        self.requests: list[tuple[Request, float]] = []
        self.responses: list[_Response] = []

    def open(self, request, timeout):
        url = request.full_url
        self.requests.append((request, timeout))
        if url.endswith("/pip/"):
            response = _Response(self.index_body, url, on_read=self.on_index_read)
        else:
            if self.wheel_error is not None:
                raise self.wheel_error
            response = _Response(self.wheel_body, self.final_wheel_url or url)
        self.responses.append(response)
        return response


def _index(wheel_url="https://pypi.org/packages/pip-25.2-py3-none-any.whl"):
    return f'<html><a href="{wheel_url}">pip wheel</a></html>'.encode("utf-8")


class MirrorMetadataTests(unittest.TestCase):
    def test_catalog_contains_official_https_simple_indexes_and_docs(self):
        self.assertEqual(set(mirrors.MIRRORS), {"pypi", "tuna", "ustc"})
        expected = {
            "pypi": "https://pypi.org/simple/",
            "tuna": "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/",
            "ustc": "https://mirrors.ustc.edu.cn/pypi/simple/",
        }
        for mirror_id, record in mirrors.MIRRORS.items():
            self.assertEqual(set(record), {"name", "url", "documentation"})
            self.assertEqual(record["url"], expected[mirror_id])
            self.assertTrue(record["url"].startswith("https://"))
            self.assertIn("/simple", record["url"])
            self.assertTrue(record["documentation"].startswith("https://"))
            self.assertNotIn("trusted-host", record["url"] + record["documentation"])
            self.assertNotIn("trusted-host", str(record).lower())

    def test_catalog_urls_have_no_credentials_or_query(self):
        for mirror in mirrors.MIRRORS.values():
            parts = mirrors.urlsplit(mirror["url"])
            self.assertIsNone(parts.username)
            self.assertIsNone(parts.password)
            self.assertFalse(parts.query)
            self.assertFalse(parts.fragment)


class ProxyConfigTests(unittest.TestCase):
    def test_https_proxy_transport_is_explicitly_rejected_before_authentication(self):
        config = mirrors.ProxyConfig("http", "https://alice:private@proxy.example:8443")
        with self.assertRaisesRegex(ValueError, "TLS-in-TLS"):
            config.validate()

    def test_none_mode_clears_all_proxy_and_no_proxy_environment_keys(self):
        config = mirrors.ProxyConfig(mode="none")
        config.validate()
        env = config.environment()
        for key in mirrors._PROXY_KEYS:
            self.assertEqual(env[key], "")
        for key in mirrors._NO_PROXY_KEYS:
            self.assertEqual(env[key], "")
        self.assertEqual(config.public(), {"mode": "none", "endpoint": None})

    def test_environment_mode_normalizes_proxy_keys_and_clears_bypass_list(self):
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://proxy.example:8080",
                "HTTPS_PROXY": "http://proxy.example:8080",
                "NO_PROXY": "localhost,example.org",
            },
            clear=True,
        ):
            config = mirrors.ProxyConfig(mode="env")
            config.validate()
            env = config.environment()
            public = config.public()
        for key in mirrors._PROXY_KEYS:
            self.assertEqual(env[key], "http://proxy.example:8080")
        for key in mirrors._NO_PROXY_KEYS:
            self.assertEqual(env[key], "")
        self.assertEqual(
            public, {"mode": "env", "endpoint": "http://proxy.example:8080"}
        )

    def test_environment_mode_rejects_socks_and_conflicting_proxies(self):
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "socks5h://proxy.example:1080"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "http:// CONNECT"):
                mirrors.ProxyConfig(mode="env").validate()
        with patch.dict(
            os.environ, {"ALL_PROXY": "socks5h://proxy.example:1080"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "http:// CONNECT"):
                mirrors.ProxyConfig(mode="env").validate()
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://one.example:8080",
                "HTTPS_PROXY": "http://two.example:8080",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "one consistent"):
                mirrors.ProxyConfig(mode="env").validate()

    def test_explicit_http_https_environment_proxies_override_all_proxy_fallback(self):
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://proxy.example:8080",
                "HTTPS_PROXY": "http://proxy.example:8080",
                "ALL_PROXY": "socks5h://unused.example:1080",
            },
            clear=True,
        ):
            config = mirrors.ProxyConfig(mode="env")
            config.validate()
            self.assertEqual(
                config.environment()["HTTPS_PROXY"], "http://proxy.example:8080"
            )

    def test_redaction_uses_environment_proxy_credentials(self):
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://alice:env-secret@proxy.example:8080",
                "HTTPS_PROXY": "http://alice:env-secret@proxy.example:8080",
            },
            clear=True,
        ):
            proxy = mirrors.ProxyConfig(mode="env")
            redacted = mirrors.redact("transport failed with env-secret", proxy)
        self.assertNotIn("env-secret", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_http_proxy_public_form_and_subprocess_environment_keep_secrets_separate(
        self,
    ):
        config = mirrors.ProxyConfig(
            mode="http", url="http://alice:s3cr3t@proxy.example:3128"
        )
        config.validate()
        self.assertEqual(
            config.public(),
            {"mode": "http", "endpoint": "http://proxy.example:3128"},
        )
        env = config.environment()
        self.assertEqual(env["HTTPS_PROXY"], config.url)
        self.assertEqual(env["https_proxy"], config.url)
        self.assertNotIn("NO_PROXY", config.url)

    def test_http_mode_rejects_socks_url_and_non_proxy_url_fields(self):
        for value in ("socks5://proxy.example:1080", "file:///tmp/proxy"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mirrors.ProxyConfig(mode="http", url=value).validate()
        with self.assertRaises(ValueError):
            mirrors.ProxyConfig(mode="none", url="http://proxy.example:8080").validate()
        with self.assertRaises(ValueError):
            mirrors.ProxyConfig(
                mode="http", url="http://proxy.example:8080/path"
            ).validate()

    def test_redact_removes_url_credentials_queries_proxy_passwords_and_tokens(self):
        proxy = mirrors.ProxyConfig(
            mode="http", url="http://alice:s3cr3t@proxy.example:3128"
        )
        message = (
            "failed https://bob:pw@mirror.example/simple/?token=abc123&x=y "
            "proxy password s3cr3t api_key=key-value Authorization: Bearer bearer-value"
        )
        redacted = mirrors.redact(message, proxy)
        for secret in (
            "bob",
            "pw@",
            "abc123",
            "x=y",
            "s3cr3t",
            "key-value",
            "bearer-value",
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("https://mirror.example/simple/", redacted)
        self.assertIn("[REDACTED]", redacted)


class MirrorBenchmarkTests(unittest.TestCase):
    def _benchmark_with_opener(
        self,
        opener,
        mirror_ids=("pypi",),
        *,
        proxy=None,
        cancel_event=None,
        timeout=3,
        sample_bytes=64,
    ):
        proxy = proxy or mirrors.ProxyConfig(mode="none")
        cancel_event = cancel_event or threading.Event()
        with patch.object(mirrors, "_make_opener", return_value=opener) as make_opener:
            result = mirrors.benchmark_mirrors(
                list(mirror_ids),
                proxy,
                cancel_event,
                timeout=timeout,
                sample_bytes=sample_bytes,
            )
        return result, make_opener

    def test_mocked_benchmark_uses_https_index_and_reads_only_sample_limit(self):
        wheel = b"w" * 512
        opener = _FakeOpener(_index(), wheel)
        proxy = mirrors.ProxyConfig(
            mode="http", url="http://alice:s3cr3t@proxy.example:3128"
        )
        result, make_opener = self._benchmark_with_opener(
            opener, proxy=proxy, sample_bytes=64
        )

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["operation"], "benchmark")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["recommended_mirror"], "pypi")
        self.assertTrue(
            any(
                "pip-25.2-py3-none-any.whl" in warning for warning in result["warnings"]
            )
        )
        self.assertTrue(
            any(
                "latency_ms measures the complete index request" in warning
                for warning in result["warnings"]
            )
        )
        self.assertEqual(len(result["results"]), 1)
        item = result["results"][0]
        self.assertEqual(
            set(item),
            {
                "mirror",
                "name",
                "reachable",
                "latency_ms",
                "download_bytes",
                "throughput_mbps",
                "error",
                "sample",
                "download_url",
                "download_host",
                "sample_origin",
                "ranking_eligible",
            },
        )
        self.assertTrue(item["reachable"])
        self.assertEqual(item["download_bytes"], 64)
        self.assertLessEqual(item["download_bytes"], 64)
        self.assertGreaterEqual(item["latency_ms"], 0)
        self.assertGreater(item["throughput_mbps"], 0)
        self.assertEqual(item["sample"], "pip-25.2-py3-none-any.whl")
        self.assertEqual(item["sample_origin"], "mirror")
        self.assertTrue(item["ranking_eligible"])
        self.assertEqual(item["download_host"], "pypi.org")
        self.assertTrue(item["download_url"].startswith("https://pypi.org/"))
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(
            all(req.full_url.startswith("https://") for req, _ in opener.requests)
        )
        self.assertTrue(all(timeout == 3 for _, timeout in opener.requests))
        proxies = make_opener.call_args.args[1]
        self.assertEqual(
            proxies,
            {
                "http": proxy.url,
                "https": proxy.url,
            },
        )
        self.assertEqual(opener.responses[-1].read_bytes, 64)

    def test_https_only_and_host_allowlist_rejects_http_userinfo_and_unknown_hosts(
        self,
    ):
        bad_urls = (
            "http://files.pythonhosted.org/packages/pip-25.2-py3-none-any.whl",
            "https://user:password@files.pythonhosted.org/packages/pip-25.2-py3-none-any.whl",
            "https://evil.example/pip-25.2-py3-none-any.whl",
        )
        for bad_url in bad_urls:
            with self.subTest(url=bad_url):
                opener = _FakeOpener(_index(bad_url), b"wheel")
                result, _ = self._benchmark_with_opener(opener)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["results"][0]["reachable"])
                self.assertTrue(result["results"][0]["error"])
                self.assertEqual(len(opener.requests), 1)

    def test_timeouts_are_per_request_and_errors_are_redacted(self):
        opener = _FakeOpener(
            _index(),
            b"",
            wheel_error=TimeoutError(
                "timeout at https://alice:s3cr3t@files.pythonhosted.org/x?token=hidden"
            ),
        )
        proxy = mirrors.ProxyConfig(
            mode="http", url="http://alice:s3cr3t@proxy.example:3128"
        )
        result, _ = self._benchmark_with_opener(opener, proxy=proxy, sample_bytes=128)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("s3cr3t", result["results"][0]["error"])
        self.assertNotIn("hidden", result["results"][0]["error"])

        opener = _FakeOpener(_index(), b"wheel")
        with patch.object(
            mirrors, "_make_opener", side_effect=TimeoutError("connect timeout")
        ):
            result = mirrors.benchmark_mirrors(
                ["pypi"],
                mirrors.ProxyConfig(mode="none"),
                threading.Event(),
                timeout=0.1,
            )
        self.assertEqual(result["results"][0]["error"], "connect timeout")

    def test_redirects_reject_http_userinfo_and_unapproved_hosts(self):
        handler = mirrors._CheckedRedirectHandler(
            {"pypi.org", "files.pythonhosted.org"}
        )
        request = mirrors.Request("https://pypi.org/simple/pip/")
        for target in (
            "http://pypi.org/simple/pip/",
            "https://user:password@pypi.org/simple/pip/",
            "https://evil.example/pip/",
        ):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    handler.redirect_request(request, None, 302, "Found", {}, target)
        accepted = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://files.pythonhosted.org/simple/pip/",
        )
        self.assertIsNotNone(accepted)

    def test_cancel_before_and_during_index_read_stops_wheel_request(self):
        event = threading.Event()
        event.set()
        result, _ = self._benchmark_with_opener(
            _FakeOpener(_index(), b"wheel"), cancel_event=event
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["recommended_mirror"])
        self.assertEqual(result["results"][0]["error"], "Cancelled before benchmark")

        event = threading.Event()

        def cancel_after_first_chunk(_data):
            event.set()

        opener = _FakeOpener(_index(), b"wheel", on_index_read=cancel_after_first_chunk)
        result, _ = self._benchmark_with_opener(opener, cancel_event=event)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["recommended_mirror"])
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("Cancelled", result["results"][0]["error"])

    def test_mirror_index_has_a_hard_one_mib_read_limit(self):
        body = b"x" * (1024 * 1024 + 64)
        opener = _FakeOpener(body, b"wheel")
        result, _ = self._benchmark_with_opener(opener)
        self.assertEqual(result["status"], "failed")
        self.assertIn("1 MiB", result["results"][0]["error"])
        self.assertLessEqual(opener.responses[0].read_bytes, 1024 * 1024)
        self.assertEqual(len(opener.requests), 1)

    def test_total_deadline_is_checked_before_each_bounded_read(self):
        response = _Response(b"data", "https://pypi.org/simple/pip/")
        with self.assertRaisesRegex(TimeoutError, "total deadline"):
            mirrors._read_response(
                response,
                64,
                threading.Event(),
                require_eof=True,
                deadline=mirrors.time.monotonic() - 1,
            )
        self.assertEqual(response.read_bytes, 0)

    def test_proxy_validation_unknown_mirror_and_bounds_fail_without_network(self):
        with patch.object(mirrors, "_make_opener") as make_opener:
            result = mirrors.benchmark_mirrors(
                ["pypi"],
                mirrors.ProxyConfig(mode="http", url="socks5://localhost:1080"),
                threading.Event(),
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("http:// CONNECT", result["warnings"][0])
        make_opener.assert_not_called()

        for args in (
            (["missing"], mirrors.ProxyConfig(mode="none"), threading.Event(), 5, 64),
            (["pypi"], mirrors.ProxyConfig(mode="none"), threading.Event(), 0, 64),
            (["pypi"], mirrors.ProxyConfig(mode="none"), threading.Event(), 5, 0),
            (["pypi"] * 5, mirrors.ProxyConfig(mode="none"), threading.Event(), 5, 64),
        ):
            with self.subTest(args=args):
                with patch.object(mirrors, "_make_opener") as make_opener:
                    result = mirrors.benchmark_mirrors(
                        args[0], args[1], args[2], timeout=args[3], sample_bytes=args[4]
                    )
                self.assertEqual(result["status"], "failed")
                make_opener.assert_not_called()

    def test_all_results_are_reported_and_fastest_reachable_is_recommended(self):
        wheel_urls = {
            "pypi": "https://pypi.org/packages/pip-25.2-py3-none-any.whl",
            "tuna": "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/pip-25.2-py3-none-any.whl",
            "ustc": "https://mirrors.ustc.edu.cn/pypi/packages/pip-25.2-py3-none-any.whl",
        }
        openers = {
            mirror_id: _FakeOpener(_index(wheel_urls[mirror_id]), b"w" * 128)
            for mirror_id in ("pypi", "tuna", "ustc")
        }
        selected = []

        def make_opener(allowed_hosts, proxies):
            # The approved index host identifies which local fake transport to use.
            host = next(
                host for host in allowed_hosts if host != "files.pythonhosted.org"
            )
            mirror_id = {
                "pypi.org": "pypi",
                "mirrors.tuna.tsinghua.edu.cn": "tuna",
                "mirrors.ustc.edu.cn": "ustc",
            }[host]
            selected.append(mirror_id)
            return openers[mirror_id]

        with patch.object(mirrors, "_make_opener", side_effect=make_opener):
            result = mirrors.benchmark_mirrors(
                ["pypi", "tuna", "ustc"],
                mirrors.ProxyConfig(mode="none"),
                threading.Event(),
                sample_bytes=64,
            )
        self.assertEqual(selected, ["pypi", "tuna", "ustc"])
        self.assertEqual(
            [row["mirror"] for row in result["results"]], ["pypi", "tuna", "ustc"]
        )
        self.assertTrue(all(row["reachable"] for row in result["results"]))
        best = min(
            result["results"],
            key=lambda row: (-row["throughput_mbps"], row["latency_ms"], row["mirror"]),
        )
        self.assertEqual(result["recommended_mirror"], best["mirror"])

    def test_mirror_served_from_cdn_never_outranks_a_direct_mirror_download(self):
        cdn_url = "https://files.pythonhosted.org/packages/pip-25.2-py3-none-any.whl?token=private"
        mirror_url = (
            "https://mirrors.ustc.edu.cn/pypi/packages/pip-25.2-py3-none-any.whl"
        )
        openers = {
            "tuna": _FakeOpener(_index(cdn_url), b"c" * 64),
            "ustc": _FakeOpener(_index(mirror_url), b"m" * 64),
        }

        def make_opener(allowed_hosts, proxies):
            host = next(
                host for host in allowed_hosts if host != "files.pythonhosted.org"
            )
            return openers["tuna" if host == "mirrors.tuna.tsinghua.edu.cn" else "ustc"]

        # Give the CDN sample vastly higher throughput than the direct mirror
        # sample. It still must not determine the mirror recommendation.
        clock_values = [0.0, 1.0, 2.0, 2.001, 3.0, 4.0, 5.0, 6.0]
        with (
            patch.object(mirrors, "_make_opener", side_effect=make_opener),
            patch.object(mirrors.time, "perf_counter", side_effect=clock_values),
        ):
            result = mirrors.benchmark_mirrors(
                ["tuna", "ustc"],
                mirrors.ProxyConfig(mode="none"),
                threading.Event(),
                sample_bytes=64,
            )

        tuna_row, ustc_row = result["results"]
        self.assertEqual(tuna_row["sample_origin"], "pypi-cdn")
        self.assertEqual(tuna_row["download_host"], "files.pythonhosted.org")
        self.assertFalse(tuna_row["ranking_eligible"])
        self.assertNotIn("private", tuna_row["download_url"])
        self.assertNotIn("?", tuna_row["download_url"])
        self.assertGreater(tuna_row["throughput_mbps"], ustc_row["throughput_mbps"])
        self.assertEqual(ustc_row["sample_origin"], "mirror")
        self.assertTrue(ustc_row["ranking_eligible"])
        self.assertEqual(ustc_row["download_host"], "mirrors.ustc.edu.cn")
        self.assertEqual(result["recommended_mirror"], "ustc")
        self.assertIn("CDN samples are excluded", result["recommendation_reason"])
        self.assertTrue(any("PyPI CDN" in warning for warning in result["warnings"]))

    def test_official_pypi_cdn_sample_is_eligible_for_pypi_recommendation(self):
        cdn_url = "https://files.pythonhosted.org/packages/pip-25.2-py3-none-any.whl"
        opener = _FakeOpener(_index(cdn_url), b"c" * 64)
        result, _ = self._benchmark_with_opener(
            opener, mirror_ids=("pypi",), sample_bytes=64
        )
        row = result["results"][0]
        self.assertEqual(row["sample_origin"], "pypi-cdn")
        self.assertTrue(row["ranking_eligible"])
        self.assertEqual(result["recommended_mirror"], "pypi")
        self.assertIn(
            "official files.pythonhosted.org", result["recommendation_reason"]
        )

    def test_cdn_redirect_is_reported_and_never_produces_mirror_recommendation(self):
        mirror_url = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/packages/pip-25.2-py3-none-any.whl"
        cdn_final_url = (
            "https://files.pythonhosted.org/packages/pip-25.2-py3-none-any.whl"
        )
        opener = _FakeOpener(
            _index(mirror_url), b"c" * 64, final_wheel_url=cdn_final_url
        )
        result, _ = self._benchmark_with_opener(
            opener, mirror_ids=("tuna",), sample_bytes=64
        )
        row = result["results"][0]
        self.assertEqual(row["sample_origin"], "pypi-cdn")
        self.assertFalse(row["ranking_eligible"])
        self.assertEqual(row["download_host"], "files.pythonhosted.org")
        self.assertIsNone(result["recommended_mirror"])
        self.assertIn(
            "does not measure mirror download speed", result["recommendation_reason"]
        )

    def test_no_reachable_mirror_returns_failed_without_recommendation(self):
        opener = _FakeOpener(b"", b"")
        result, _ = self._benchmark_with_opener(opener)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["recommended_mirror"])
        self.assertFalse(result["results"][0]["reachable"])


if __name__ == "__main__":
    unittest.main()
