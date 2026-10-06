"""API keys are kept by the operating system, never in the game's files.

Run with: python3 -m unittest discover tests
"""

import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))

from aigame import keystore  # noqa: E402

KEY = "not-a-real-key-0123456789abcdefABCDEF012345"   # made up for the tests


class PrivateFileTest(unittest.TestCase):
    """The store used where the system offers none."""

    def setUp(self):
        self.work = tempfile.mkdtemp()
        keystore.setup(self.work)
        keystore.use("file")

    def tearDown(self):
        keystore.setup(None)
        keystore.use(None)
        shutil.rmtree(self.work)

    def test_a_key_comes_back_and_is_not_written_in_the_clear(self):
        self.assertEqual(keystore.get("connection:main"), "")
        self.assertTrue(keystore.put("connection:main", KEY))
        self.assertTrue(keystore.put("connection:utility", "another-key"))
        self.assertEqual((keystore.get("connection:main"), keystore.get("connection:utility")), (KEY, "another-key"))
        path = os.path.join(self.work, "keys")
        with open(path, "rb") as f:
            written = f.read()
        self.assertNotIn(KEY.encode(), written)
        self.assertNotIn(b"connection", written)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)             # this user only
        self.assertEqual(os.listdir(self.work), ["keys"])

    def test_an_emptied_key_is_removed(self):
        keystore.put("connection:main", KEY)
        self.assertTrue(keystore.put("connection:main", ""))
        self.assertEqual(keystore.get("connection:main"), "")
        self.assertIn("outside the game's folder", keystore.describe())


@unittest.skipUnless(sys.platform == "darwin", "the macOS Keychain")
class KeychainTest(unittest.TestCase):
    def test_a_key_is_kept_by_the_system_and_handed_over_out_of_sight(self):
        keystore.use(None)
        self.assertEqual(keystore.kind(), "keychain")
        name = "test:" + uuid.uuid4().hex                # a throwaway entry, removed again below
        seen = []
        real = keystore._run
        keystore._run = lambda args, text=None: seen.append(args) or real(args, text)
        try:
            self.assertTrue(keystore.put(name, KEY + ' with "quotes" and a \\ slash'))
            self.assertEqual(keystore.get(name), KEY + ' with "quotes" and a \\ slash')
            self.assertTrue(keystore.put(name, KEY))                                   # replacing works
            self.assertEqual(keystore.get(name), KEY)
        finally:
            keystore._run = real
            keystore.drop(name)
        self.assertEqual(keystore.get(name), "")
        self.assertFalse(any(KEY in part for args in seen for part in args), "the key was put on a command line")


class NothingKeepsAKeyTest(unittest.TestCase):
    def test_the_game_never_writes_a_key_into_its_saved_settings(self):
        """The Models screen must edit the key in memory (runtime.keys), not in the settings Ren'Py saves."""
        with open(os.path.join(ROOT, "game", "llm.rpy")) as f:
            source = f.read()
        self.assertNotIn('connection, "api_key"', source)
        self.assertIn('settings_input(_("API key"), runtime.keys, connection["id"], secret=True)', source)
        requests = re.findall(r"aig_llm\.(?:chat_request|models_request)\((\w+)", source)
        self.assertTrue(requests)
        self.assertEqual(set(requests) - {"connection", "request_connection"}, set())


if __name__ == "__main__":
    unittest.main()
