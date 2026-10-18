import contextlib
import http.client
import io
import json
from pathlib import Path
import shutil
import ssl
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import roomba_cloud.cloud_http as cloud_http
from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.web.dashboard import JS, inventory


A, B = "A" * 32, "B" * 32


class DashboardStorageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard JavaScript tests")
    def test_remember_restore_reject_logout_and_storage_failures(self):
        subprocess.run(
            ["node", str(Path(__file__).with_name("dashboard_storage.js"))],
            input=JS,
            text=True,
            check=True,
            capture_output=True,
            timeout=10,
        )


class InventoryTests(unittest.TestCase):
    def test_persistent_history_metadata_and_connection_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadows.db"
            service = ShadowService(path)
            with patch("roomba_cloud.shadow_service.time.time", return_value=100):
                service.record_registration(A, "192.0.2.1", "505-SKU")
                session = service.start_session(A, 30, peer_ip="192.0.2.2")
                other = service.start_session(B, 30, peer_ip="192.0.2.2")
                for robot_id, name, data in (
                    (A, "rw-software", {"softwareVer": "1.2", "password": "private"}),
                    (
                        A,
                        "ro-currentstate",
                        {"batPct": 42, "cleanMissionStatus": {"phase": "charge"}},
                    ),
                    (B, None, {"sku": "<script>untrusted</script>", "secret": "private"}),
                ):
                    suffix = f"name/{name}/" if name else ""
                    with contextlib.redirect_stdout(io.StringIO()):
                        service.respond(
                            f"$aws/things/{robot_id}/shadow/{suffix}update",
                            json.dumps({"state": {"reported": data}}).encode(),
                            robot_id,
                        )
            with patch("roomba_cloud.web.dashboard.time.time", return_value=110):
                robots = inventory(path)["robots"]
            self.assertEqual(len(robots), 2)
            self.assertEqual(
                robots[0],
                {
                    "robot_id": A,
                    "ip": "192.0.2.2",
                    "sku": "505-SKU",
                    "firmware": "1.2",
                    "battery": 42,
                    "phase": "charge",
                    "online": True,
                    "first_seen": 100,
                    "last_seen": 100,
                    "map_controls": {
                        "allowed": False,
                        "reason": "Map controls require a known idle robot with no active or paused mission.",
                    },
                    "mapping_controls": {
                        "allowed": False,
                        "reason": "This action requires a docked robot with no active or paused mission.",
                    },
                },
            )
            self.assertEqual(robots[1]["sku"], "<script>untrusted</script>")
            self.assertNotIn("private", json.dumps(robots))
            with patch("roomba_cloud.shadow_service.time.time", return_value=120):
                service.touch_session(session, 30)
            service.end_session(session)
            service.end_session(other)
            # Reopening the database retains connection history after sessions end.
            ShadowService(path)
            robots = inventory(path)["robots"]
            self.assertFalse(robots[0]["online"])
            self.assertEqual(robots[0]["last_seen"], 120)
            self.assertEqual(robots[0]["first_seen"], 100)
            with patch("roomba_cloud.shadow_service.time.time", return_value=130):
                service.start_session(B, 1, peer_ip="192.0.2.3")
            with patch("roomba_cloud.web.dashboard.time.time", return_value=132):
                self.assertFalse(inventory(path)["robots"][1]["online"])

    def test_existing_shadows_without_connection_history_and_empty_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadows.db"
            service = ShadowService(path)
            self.assertEqual(inventory(path), {"robots": []})
            with contextlib.redirect_stdout(io.StringIO()):
                service.respond(
                    f"$aws/things/{A}/shadow/update", b'{"state":{"reported":{"sku":"505"}}}', A
                )
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute("DROP TABLE robot_connections")
            item = inventory(path)["robots"][0]
            self.assertEqual(item["sku"], "505")
            self.assertIsNone(item["ip"])
            self.assertIsNone(item["first_seen"])


class DashboardHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.cert, cls.key = cloud_http.make_cert(cls.directory.name, ["localhost"])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_login_assets_protected_inventory_registration_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadows.db"
            ShadowService(path)
            with cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler()) as server:
                server.customer_auth = cloud_http.CustomerAuth(b"a" * 64)
                server.customer_shadow_db = path
                server.uploads = None
                server.discovery = b"{}"
                server.registration_ok = True
                server.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server.tls_context.load_cert_chain(self.cert, self.key)
                thread = threading.Thread(target=server.serve_forever)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    thread.start()
                    try:

                        def request(route, method="GET", body=None, token=None):
                            conn = http.client.HTTPSConnection(
                                "127.0.0.1",
                                server.server_port,
                                context=ssl._create_unverified_context(),
                                timeout=5,
                            )
                            try:
                                headers = {"Authorization": "Bearer " + token} if token else {}
                                conn.request(method, route, body, headers)
                                response = conn.getresponse()
                                return response.status, dict(response.getheaders()), response.read()
                            finally:
                                conn.close()

                        for asset in ("/", "/dashboard.js", "/dashboard.css"):
                            status, headers, body = request(asset)
                            self.assertEqual(status, 200)
                            self.assertEqual(headers["Cache-Control"], "no-store")
                            self.assertIn(
                                "frame-ancestors 'none'", headers["Content-Security-Policy"]
                            )
                            self.assertNotIn(A.encode(), body)
                        for token in (None, "wrong"):
                            self.assertEqual(request("/v1/customer/robots", token=token)[0], 401)
                            self.assertEqual(
                                request(f"/v1/customer/robots/{A}/room-editor", token=token)[0], 401
                            )
                            self.assertEqual(
                                request(
                                    f"/v1/customer/robots/{A}/room-edits",
                                    "POST",
                                    b"{}",
                                    token=token,
                                )[0],
                                401,
                            )
                        self.assertEqual(
                            request(f"/v1/customer/robots/{A}/room-editor", token="a" * 64)[0], 503
                        )
                        self.assertEqual(
                            request(f"/v1/customer/robots/{A}/room-edits", token="a" * 64)[0], 405
                        )
                        self.assertEqual(
                            request("/v1/customer/robots", token="a" * 64)[2], b'{"robots": []}'
                        )
                        self.assertEqual(
                            request(
                                f"/v1/{A}/register",
                                "POST",
                                b'{"sku":"505","password":"do-not-store"}',
                            )[0],
                            200,
                        )
                        status, _, body = request("/v1/customer/robots", token="a" * 64)
                        self.assertEqual(status, 200)
                        item = json.loads(body)["robots"][0]
                        self.assertEqual(item["ip"], "127.0.0.1")
                        self.assertEqual(item["sku"], "505")
                        self.assertNotIn("do-not-store", path.read_bytes().decode("latin1"))
                        self.assertEqual(
                            request("/v1/customer/robots", "POST", token="a" * 64)[0], 405
                        )
                        self.assertEqual(request("/", "POST")[0], 405)
                        server.customer_shadow_db = None
                        self.assertEqual(request("/v1/customer/robots", token="a" * 64)[0], 503)
                        server.customer_shadow_db = Path(directory) / "missing.db"
                        self.assertEqual(request("/v1/customer/robots", token="a" * 64)[0], 500)
                        server.customer_auth = None
                        self.assertEqual(request("/v1/customer/robots")[0], 503)
                    finally:
                        server.shutdown()
                        thread.join(5)
                self.assertNotIn("a" * 64, output.getvalue())
