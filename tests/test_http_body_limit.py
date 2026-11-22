"""The request body limit: 1 MB everywhere, raised only for the paths a server is told about (the Drive scan, #549)."""
import threading
import unittest
import urllib.error
import urllib.request

from t6.server.app import Request, Response
from t6.server.http import CATALOG_BODY_BYTES, CATALOG_SNAPSHOT_PATH, MAX_BODY_BYTES, make_server


class BodyLimitTests(unittest.TestCase):
    def serve(self, **options: object) -> str:
        server = make_server(('127.0.0.1', 0), lambda request: Response(200, {'bytes': len(request.body)}), **options)  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    @staticmethod
    def post(base: str, path: str, size: int) -> int | str:
        """The status; a 413 is answered before the body is read, so the server may also drop the connection under a large upload."""
        request = urllib.request.Request(base + path, data=b'x' * size, method='POST')
        try:
            with urllib.request.urlopen(request) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return error.code
        except (urllib.error.URLError, ConnectionError):
            return 413

    def test_the_default_limit_is_one_megabyte_on_every_path(self) -> None:
        base = self.serve()
        self.assertEqual(self.post(base, '/v1/drive/get', MAX_BODY_BYTES), 200)
        self.assertEqual(self.post(base, '/v1/drive/get', MAX_BODY_BYTES + 1), 413)
        self.assertEqual(self.post(base, CATALOG_SNAPSHOT_PATH, MAX_BODY_BYTES + 1), 413)

    def test_a_named_path_may_be_larger_and_no_other_path(self) -> None:
        base = self.serve(body_limits={CATALOG_SNAPSHOT_PATH: CATALOG_BODY_BYTES})
        self.assertEqual(self.post(base, CATALOG_SNAPSHOT_PATH, 1_300_000), 200)
        self.assertEqual(self.post(base, CATALOG_SNAPSHOT_PATH + '?x=1', 1_300_000), 200)
        self.assertEqual(self.post(base, CATALOG_SNAPSHOT_PATH, CATALOG_BODY_BYTES + 1), 413)
        self.assertEqual(self.post(base, '/v1/drive/add', 1_300_000), 413)


if __name__ == '__main__':
    unittest.main()
