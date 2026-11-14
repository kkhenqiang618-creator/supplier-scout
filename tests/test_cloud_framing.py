"""Cloud HTTP framing: a malformed Content-Length must be a 4xx, never an uncaught exception."""
import unittest
from cloud_http import CloudBusinessHandler


class Dummy:
    """Just enough of a handler for the framing checks that run before the body is read."""
    def __init__(self, length):
        self.headers = {'Content-Length': length}
        self.sent = []
    def send(self, payload, status=200):
        self.sent.append((status, payload))


class FramingTests(unittest.TestCase):
    def test_unparseable_content_length_is_rejected(self):
        for value in ['abc', '-1', '1e9', '3.5', ' ']:
            dummy = Dummy(value)
            CloudBusinessHandler.do_POST(dummy)
            self.assertEqual(dummy.sent[0][0], 400, value)

    def test_oversized_body_is_rejected_before_it_is_read(self):
        dummy = Dummy(str(5 * 1024 * 1024))
        CloudBusinessHandler.do_POST(dummy)
        self.assertEqual(dummy.sent[0][0], 413)


if __name__ == '__main__':
    unittest.main()
