# Copyright 2025 Google LLC All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

from analytics_mcp import http_server


class HttpServerConfigTest(unittest.TestCase):
    def test_defaults_bind_locally(self):
        config = http_server.parse_http_config([], {})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.path, "/mcp")

    def test_port_environment_variable_sets_default(self):
        config = http_server.parse_http_config([], {"PORT": "8080"})
        self.assertEqual(config.port, 8080)

    def test_cli_port_overrides_environment(self):
        config = http_server.parse_http_config(
            ["--port", "9000"], {"PORT": "8080"}
        )
        self.assertEqual(config.port, 9000)

    def test_normalizes_trailing_path_slash(self):
        config = http_server.parse_http_config(
            ["--path", "/analytics/"], {}
        )
        self.assertEqual(config.path, "/analytics")

    def test_rejects_path_without_leading_slash(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config(["--path", "mcp"], {})

    def test_rejects_out_of_range_cli_port(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config(["--port", "70000"], {})

    def test_rejects_invalid_port_environment_variable(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config([], {"PORT": "not-a-port"})
