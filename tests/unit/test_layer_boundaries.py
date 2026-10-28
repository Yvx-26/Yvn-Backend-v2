import ast
import subprocess
import sys
import unittest
from pathlib import Path

from app.services.compat.infrastructure import normalize_device_config
from app.services.payload import is_allowed_raw_length, normalize_payload_to_hex
from app.services.compat.waveform import payload_to_samples
from app.utils.hex_codec import compact_hex_text


ROOT = Path(__file__).resolve().parents[2]


class LayerBoundaryTests(unittest.TestCase):
    def test_existing_http_routes_are_still_registered_without_new_business_routes(self):
        from app.main import app
        expected = {
            ("POST", "/emqx/webhook"), ("POST", "/emqx/gps"), ("GET", "/emqx/ingestion-health"),
            ("GET", "/api/location/latest"), ("GET", "/api/devices"), ("GET", "/api/frames"),
            ("GET", "/api/waveform"), ("GET", "/api/cable_info"), ("POST", "/api/cable_info"),
            ("POST", "/api/login"), ("GET", "/api/me"), ("POST", "/api/logout"),
            ("GET", "/api/device_bind"), ("POST", "/api/device_bind"),
            ("GET", "/api/dashboard/summary"), ("GET", "/api/dashboard/devices"),
            ("GET", "/api/dashboard/wave/latest"), ("GET", "/api/dashboard/map"),
            ("GET", "/api/dashboard/operation"), ("GET", "/api/infrastructure"),
            ("PUT", "/api/infrastructure/devices/{device_id}"),
            ("PUT", "/api/infrastructure/lines/{line_id}"), ("GET", "/api/infrastructure/health"),
        }
        actual = {(method, route.path) for route in app.routes if hasattr(route, "dependant")
                  for method in route.methods}
        self.assertEqual(actual, expected)

    def test_protocol_can_import_without_third_party_packages_or_deployment_config(self):
        result = subprocess.run([
            sys.executable, "-S", "-c",
            "import sys; from app.protocols.tdr import parse_tdr_payload_hex; "
            "assert 'app.core.config' not in sys.modules; "
            "assert 'fastapi' not in sys.modules; assert 'pymysql' not in sys.modules; print('protocol-only')",
        ], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "protocol-only")

    def test_main_routes_do_not_execute_sql_or_parse_protocol_directly(self):
        for name in ("emqx", "waveform", "dashboard"):
            tree = ast.parse((ROOT / "app" / "api" / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.assertFalse(isinstance(node.func, ast.Attribute) and node.func.attr == "execute", name)
                    self.assertFalse(isinstance(node.func, ast.Name) and node.func.id == "parse_tdr_payload_hex", name)

    def test_repositories_neither_depend_on_services_nor_own_transactions(self):
        for path in (ROOT / "app" / "repositories").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("app.api", "app.services", "fastapi")), path.name)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, ("commit", "rollback"), path.name)

    def test_services_do_not_raise_framework_http_exceptions(self):
        for path in (ROOT / "app" / "services").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("fastapi", "starlette", "app.api")), str(path))

    def test_retired_modules_have_no_source_or_remaining_runtime_imports(self):
        old = ("app.services.tdr", "app.services.mqtt_bridge", "app.services.line_binding", "app.services.ingestion_receipts")
        for name in old[1:]:
            self.assertFalse((ROOT / (name.replace(".", "/") + ".py")).exists())
        self.assertFalse((ROOT / "app/services/tdr/parser.py").exists())
        for path in (ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(old), str(path))

    def test_local_module_graph_has_no_circular_dependencies(self):
        modules = {}
        for path in (ROOT / "app").rglob("*.py"):
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            if name.endswith(".__init__"):
                name = name[:-9]
            modules[name] = ast.parse(path.read_text(encoding="utf-8"))
        graph = {name: set() for name in modules}
        for name, tree in modules.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    candidates = [node.module, *(node.module + "." + alias.name for alias in node.names)]
                elif isinstance(node, ast.Import):
                    candidates = [alias.name for alias in node.names]
                else:
                    continue
                graph[name].update(candidate for candidate in candidates if candidate in modules)
        visited, active = set(), set()

        def visit(name):
            self.assertNotIn(name, active, f"circular dependency: {name}")
            if name in visited:
                return
            active.add(name)
            for target in graph[name]:
                visit(target)
            active.remove(name)
            visited.add(name)

        for name in graph:
            visit(name)

    def test_shared_hex_cleaner_keeps_existing_whitespace_and_fallback_rules(self):
        self.assertEqual(compact_hex_text(" \tAA 01\r\nFE 00\t "), "AA01FE00")
        self.assertEqual(normalize_payload_to_hex(" AA 01\nFE 00", "hex")["hex"], "aa01fe00")
        self.assertEqual(payload_to_samples(" AA 01\nFE 00"), [170, 1, 254, 0])
        self.assertEqual(payload_to_samples("AA0"), [170])
        self.assertEqual(normalize_payload_to_hex("!invalid!", "base64")["encoding"], "utf8_fallback")

    def test_length_policy_and_legacy_config_fixes_have_not_been_tightened(self):
        for value, expected in ((None, False), (999, False), (1000, True), (8192, True), (1000000, True), ("1000", True)):
            self.assertEqual(is_allowed_raw_length(value), expected)
        result = normalize_device_config({"acquisition": {"batchSize": 10, "triggerThreshold": "2.50 V"}})
        self.assertEqual(result["acquisition"], {"batchSize": 4000, "triggerThreshold": ""})
