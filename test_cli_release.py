"""Offline checks of the release archive trust boundary."""
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from scripts import cli_release


class ReleaseTests(unittest.TestCase):
    def metadata(self, digest):
        return json.dumps({"tag_name": "v1.96.0", "draft": False, "prerelease": False,
                           "assets": [{"name": "dependabot-v1.96.0-linux-amd64.tar.gz",
                                       "digest": digest, "browser_download_url": "https://example.test/archive"}]}).encode()

    def test_archive_matches_upstream_digest(self):
        archive = b"archive bytes"
        digest = hashlib.sha256(archive).hexdigest()
        with patch.object(cli_release, "fetch", side_effect=[self.metadata("sha256:" + digest), archive]):
            self.assertEqual(cli_release.release("1.96.0"), ("1.96.0", digest, archive))

    def test_missing_or_wrong_digest_fails_closed(self):
        for digest in (None, "sha256:" + "0" * 64):
            with self.subTest(digest=digest), patch.object(cli_release, "fetch", side_effect=[self.metadata(digest), b"archive"]):
                with self.assertRaisesRegex(ValueError, "digest"):
                    cli_release.release("1.96.0")
