"""Offline checks for the Bitbucket Dependabot wrapper."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import dependabot

JOBS = ({"manager": "gradle", "directory": "/android", "group": "android"},
        {"manager": "pub", "directory": "/", "group": "flutter"})


class DependabotTests(unittest.TestCase):
    def make_client(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_USERNAME": "bot@example.com",
                                      "BOT_API_TOKEN": "secret-token"}):
            return dependabot.Bitbucket("dev")

    def test_pr_query_uses_supported_page_size_and_follows_pagination(self):
        client = self.make_client()
        first = {"id": 1, "state": "OPEN"}
        second = {"id": 2, "state": "DECLINED"}
        next_url = dependabot.API_ROOT + client.repo_path + "/pullrequests?page=2"
        with patch.object(client, "request", side_effect=[
                {"values": [first], "next": next_url}, {"values": [second]}]) as request:
            self.assertEqual(client.pull_requests("dependabot/android-dev"), [first, second])
        query = parse_qs(urlparse(request.call_args_list[0].args[0]).query)
        self.assertEqual(query["pagelen"], ["50"])
        self.assertEqual(query["state"], ["OPEN", "DECLINED"])
        self.assertEqual(query["q"], ['source.branch.name = "dependabot/android-dev" AND '
                                     'destination.branch.name = "dev"'])
        self.assertEqual(request.call_args_list[1].args[0], next_url)

    def test_api_errors_keep_message_and_redact_token(self):
        client = self.make_client()
        body = json.dumps({"error": {"message": "Invalid pagelen; secret-token"}}).encode()
        error = HTTPError("https://api.bitbucket.org/2.0/test", 400, "Bad Request", {}, io.BytesIO(body))
        with patch("dependabot.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                client.request("test")
        self.assertIn("HTTP 400 — Invalid pagelen; [REDACTED]", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_non_json_api_errors_keep_status(self):
        client = self.make_client()
        error = HTTPError("https://api.bitbucket.org/2.0/test", 502, "Bad Gateway", {},
                          io.BytesIO(b"<html>Bad gateway</html>"))
        with patch("dependabot.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                client.request("test")

    def run_job(self, configuration, events, returncode=0):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            client = Mock(repository="workspace/repository", username="bot", token="secret", base_branch="dev")

            def execute(arguments, **kwargs):
                kwargs["stdout"].write("\n".join(json.dumps(event) for event in events))
                job = json.loads((directory / "job.json").read_text())
                self.assertNotIn("secret", json.dumps(job))
                self.job = job["job"]
                self.environment = kwargs["env"]
                self.command = arguments
                return subprocess.CompletedProcess(arguments, returncode)

            with patch.dict("os.environ", {"BITBUCKET_DOCKER_HOST_INTERNAL": "docker"}), \
                    patch("dependabot.subprocess.run", side_effect=execute):
                return dependabot.run_updates(directory, client, "base", configuration, Path("cli"))

    def test_android_source_and_group(self):
        self.assertIsNone(self.run_job(JOBS[0], [{"type": "mark_as_processed"}]))
        self.assertEqual(self.job["source"]["directory"], "/android")
        self.assertEqual(self.job["source"]["branch"], "dev")
        self.assertEqual(self.job["dependency-groups"][0]["name"], "android")
        self.assertEqual(self.job["commit-message-options"], {"prefix": "chore(deps)"})

    def test_git_api_token_username_default_and_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.run_job(JOBS[0], [{"type": "mark_as_processed"}])
        self.assertEqual(self.environment["DEPENDABOT_READ_USERNAME"],
                         "x-bitbucket-api-token-auth")
        with patch.dict("os.environ", {"DEPENDABOT_READ_USERNAME": "custom-user"}):
            self.run_job(JOBS[0], [{"type": "mark_as_processed"}])
        self.assertEqual(self.environment["DEPENDABOT_READ_USERNAME"], "custom-user")

    def test_flutter_ignores_and_constraint_bumps(self):
        self.run_job(dict(JOBS[1], ignore=["async", "flutter_localizations", "flutter_test", "intl"]),
                     [{"type": "mark_as_processed"}])
        self.assertEqual(self.job["package-manager"], "pub")
        self.assertEqual(self.job["requirements-update-strategy"], "bump_versions")
        self.assertEqual({item["dependency-name"] for item in self.job["ignore-conditions"]},
                         {"async", "flutter_localizations", "flutter_test", "intl"})

    def test_empty_pub_ignores_are_preserved(self):
        self.run_job(JOBS[1], [{"type": "mark_as_processed"}])
        self.assertEqual(self.job["ignore-conditions"], [])

    def test_cli_failure_never_returns_partial_updates(self):
        events = [{"type": "create_pull_request", "data": {"base-commit-sha": "base"}},
                  {"type": "mark_as_processed"}]
        with self.assertRaisesRegex(RuntimeError, "CLI exited with code 1"):
            self.run_job(JOBS[1], events, returncode=1)

    def test_cli_failure_keeps_updater_error(self):
        events = [{"type": "record_update_job_error", "data": {"error-type": "job_repo_not_found"}}]
        with self.assertRaisesRegex(RuntimeError, "job_repo_not_found"):
            self.run_job(JOBS[0], events, returncode=1)

    def test_manager_selection_runs_and_publishes_only_selected_job(self):
        for manager in ("gradle", "pub"):
            client = Mock()
            client.branch.return_value = "base"
            update = {"dependencies": []}
            with self.subTest(manager=manager), patch("dependabot.Bitbucket", return_value=client), \
                    patch("dependabot.signal.signal"), \
                    patch("dependabot.run_updates", return_value=update) as run, \
                    patch("dependabot.publish_update") as publish:
                dependabot.main(["--manager", manager, "--directory", "/", "--group", "test"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[3]["manager"], manager)
                self.assertEqual(publish.call_count, 1)
                self.assertEqual(publish.call_args.args[-1]["manager"], manager)

    def test_failed_or_incomplete_job_is_rejected(self):
        for events in ([], [{"type": "record_update_job_error", "data": {"error-type": "failure"}},
                            {"type": "mark_as_processed"}]):
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                self.run_job(JOBS[0], events)

    def test_multiple_or_wrong_base_updates_are_rejected(self):
        update = {"type": "create_pull_request", "data": {"base-commit-sha": "wrong"}}
        with self.assertRaises(ValueError):
            self.run_job(JOBS[0], [update, {"type": "mark_as_processed"}])
        with self.assertRaises(RuntimeError):
            self.run_job(JOBS[0], [update, update, {"type": "mark_as_processed"}])

    def test_dry_run_never_writes(self):
        client = Mock(base_branch="dev")
        client.pull_requests.return_value = []
        with patch("dependabot.git") as git:
            dependabot.publish_update(Path("unused"), client,
                {"dependencies": [{"name": "example", "version": "2"}]},
                "base", True, JOBS[1])
            git.assert_not_called()
        client.pull_requests.assert_called_once_with("dependabot/flutter-dev")
        client.request.assert_not_called()
        client.branch.assert_not_called()

    def test_files_cannot_escape_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("../escape", ".git/config", ".GIT/config", "/absolute"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    dependabot.apply_files(directory, [{"directory": "/", "name": name, "content": "bad"}])

    def test_parent_symlink_cannot_modify_git_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / ".git").mkdir()
            (directory / "alias").symlink_to(directory / ".git", target_is_directory=True)
            with self.assertRaises(ValueError):
                dependabot.apply_files(directory, [{"directory": "/alias", "name": "config", "content": "bad"}])
            self.assertFalse((directory / ".git/config").exists())

    def test_api_rejects_external_pagination_and_redirects(self):
        client = self.make_client()
        with patch("dependabot.urlopen") as request:
            with self.assertRaises(ValueError):
                client.request("https://example.test/steal")
            request.assert_not_called()
        self.assertIsNone(dependabot.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test"))

    def test_callback_route_and_cooldown(self):
        self.run_job(JOBS[0], [{"type": "mark_as_processed"}])
        self.assertEqual(self.environment["FAKE_API_HOST"], "0.0.0.0")
        self.assertEqual(self.environment["FAKE_API_PORT"], "8080")
        self.assertEqual(self.command[-2:], ["--api-url", "http://docker:8080"])
        self.assertNotIn("cooldown", self.job)
        self.run_job(dict(JOBS[0], cooldown_days=0), [{"type": "mark_as_processed"}])
        self.assertEqual(self.job["cooldown"], {"default-days": 0})

    def test_reviewers_exclude_bot_and_inline_overrides_legacy(self):
        client = self.make_client()
        client.explicit_reviewers = ["bot", "human"]
        with patch.object(client, "request", return_value={"uuid": "bot"}), \
                patch.object(client, "values") as values:
            self.assertEqual(client.reviewers(), [{"uuid": "human"}])
            values.assert_not_called()
        client.explicit_reviewers = None
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(client, "request", return_value={"uuid": "bot"}), \
                patch.object(client, "values", return_value=[{"uuid": "bot"}, {"uuid": "human"}]):
            self.assertEqual(client.reviewers(), [{"uuid": "human"}])

    def test_declined_fingerprint_prevents_publication(self):
        update = {"dependencies": [{"name": "package", "version": "2"}]}
        fingerprint = dependabot.hashlib.sha256(json.dumps([("package", "2")]).encode()).hexdigest()
        client = Mock(base_branch="dev")
        client.pull_requests.return_value = [{"state": "DECLINED", "description":
            f"<!-- dependabot-update:{fingerprint} -->"}]
        with patch("dependabot.git") as git:
            dependabot.publish_update(Path("unused"), client, update, "base", False, JOBS[1])
            git.assert_not_called()
        client.request.assert_not_called()

    def test_generated_binary_modes_deletion_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            file = {"directory": "/", "name": "file", "content": "AAH/",
                    "content_encoding": "base64", "mode": "100755"}
            dependabot.apply_files(directory, [file])
            self.assertEqual((directory / "file").read_bytes(), b"\x00\x01\xff")
            self.assertEqual((directory / "file").stat().st_mode & 0o777, 0o755)
            dependabot.apply_files(directory, [dict(file, deleted=True)])
            self.assertFalse((directory / "file").exists())
            (directory / "link").symlink_to(directory / "missing")
            for change in (dict(file, name="link"), dict(file, symlink_target="elsewhere"),
                           dict(file, mode="4755"), dict(file, content="not base64!")):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    dependabot.apply_files(directory, [change])

    def test_publish_refresh_skips_unchanged_commit_and_uses_lease_for_changes(self):
        for unchanged in (False, True):
            for opened in (False, True):
                with self.subTest(unchanged=unchanged, opened=opened), tempfile.TemporaryDirectory() as temporary:
                    client = Mock(base_branch="dev", repository="workspace/repository", token="secret")
                    client.branch.return_value = "previous"
                    client.pull_requests.return_value = [{"state": "OPEN", "id": 7}] if opened else []
                    client.request.return_value = {"links": {"html": {"href": "https://example.test/pr"}}}
                    update = {"dependencies": [{"name": "example", "version": "2"}],
                              "updated-dependency-files": []}
                    fetches = iter(["base", "previous"])

                    def git_result(directory, *args, **kwargs):
                        if args == ("rev-parse", "FETCH_HEAD"):
                            return next(fetches)
                        if args == ("diff", "--cached", "--name-only"):
                            return "manifest"
                        if args == ("write-tree",):
                            return "new-tree"
                        if args == ("rev-parse", "previous^{tree}"):
                            return "new-tree" if unchanged else "old-tree"
                        return ""

                    with patch("dependabot.git", side_effect=git_result) as git:
                        dependabot.publish_update(Path(temporary), client, update, "base", False, JOBS[0])
                    pushes = [call for call in git.call_args_list if call.args[1] == "push"]
                    self.assertEqual(len(pushes), 0 if unchanged else 1)
                    if pushes:
                        self.assertIn("--force-with-lease=refs/heads/dependabot/android-dev:previous", pushes[0].args)
                        self.assertNotIn("secret", str(pushes[0].args))
                    self.assertEqual(client.request.call_args.args[1], "PUT" if opened else "POST")

    def test_base_race_prevents_push_and_pr(self):
        client = Mock(base_branch="dev", repository="workspace/repository", token="secret")
        client.pull_requests.return_value = []
        with tempfile.TemporaryDirectory() as temporary, patch("dependabot.git", return_value="new-base") as git:
            with self.assertRaisesRegex(RuntimeError, "Base branch changed"):
                dependabot.publish_update(Path(temporary), client,
                    {"dependencies": []}, "old-base", False, JOBS[0])
            self.assertFalse(any(call.args[1] == "push" for call in git.call_args_list))
        client.request.assert_not_called()

    def test_main_cleans_temporary_directory_on_failure(self):
        client = Mock()
        client.branch.return_value = "base"
        paths = []

        def fail(directory, *args):
            paths.append(directory)
            raise RuntimeError("updater failure")

        with patch("dependabot.Bitbucket", return_value=client), patch("dependabot.run_updates", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "updater failure"):
                dependabot.main(["--manager", "pub", "--directory", "/", "--group", "flutter"])
        self.assertFalse(paths[0].exists())


if __name__ == "__main__":
    unittest.main()
