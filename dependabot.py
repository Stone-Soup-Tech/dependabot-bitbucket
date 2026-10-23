#!/usr/bin/env python3

"""Run Dependabot and maintain one grouped Bitbucket PR."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import re
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


GIT_API_TOKEN_USERNAME = "x-bitbucket-api-token-auth"
API_ROOT = "https://api.bitbucket.org/2.0/"


class NoRedirect(HTTPRedirectHandler):
    """Never forward authenticated API requests to a redirect destination."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout):
    return build_opener(NoRedirect).open(request, timeout=timeout)


class Bitbucket:
    """Access Bitbucket with the configured bot account and API token."""

    def __init__(self, base_branch="main", reviewers=None):
        self.base_branch = base_branch
        self.explicit_reviewers = reviewers
        self.repository = os.environ["BITBUCKET_REPO_FULL_NAME"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid BITBUCKET_REPO_FULL_NAME")

        self.username = os.environ["BOT_USERNAME"]
        self.token = os.environ["BOT_API_TOKEN"]
        self.authorization = "Basic " + base64.b64encode(
            f"{self.username}:{self.token}".encode()
        ).decode()
        self.repo_path = "repositories/" + quote(self.repository, safe="/")

    def request(self, path, method="GET", data=None):
        """Make an authenticated request without following external pagination URLs."""
        url = path if path.startswith("https://") else API_ROOT + path
        if urlparse(url).netloc != "api.bitbucket.org":
            raise ValueError("Unexpected Bitbucket API host")

        body = json.dumps(data).encode() if data is not None else None
        request = Request(url, data=body, method=method, headers={
            "Authorization": self.authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            # Retain the API explanation instead of hiding invalid-query details.
            detail = ""
            try:
                payload = json.loads(error.read(16384))
                detail = payload.get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                pass
            finally:
                error.close()

            if isinstance(detail, str) and detail:
                for secret in (self.token, self.authorization,
                               os.environ.get("DEPENDABOT_READ_TOKEN")):
                    if secret:
                        detail = detail.replace(secret, "[REDACTED]")
                detail = " — " + " ".join(detail.split())[:1000]
            else:
                detail = ""
            raise RuntimeError(
                f"Bitbucket {method} {urlparse(url).path} failed: HTTP {error.code}{detail}"
            ) from None

    def values(self, path):
        """Read every page from a Bitbucket list endpoint."""
        while path:
            page = self.request(path)
            yield from page["values"]
            path = page.get("next")

    def branch(self, name):
        """Return a branch SHA, or None when the branch does not exist."""
        query = urlencode({"q": f'name = "{name}"', "pagelen": 100})
        branches = list(self.values(f"{self.repo_path}/refs/branches?{query}"))
        return branches[0]["target"]["hash"] if branches else None

    def pull_requests(self, update_branch):
        """Find current and declined PRs owned by this updater."""
        query = urlencode({
            "q": f'source.branch.name = "{update_branch}" AND '
                 f'destination.branch.name = "{self.base_branch}"',
            # This endpoint caps page size at 50 (unlike branch listings).
            "state": ["OPEN", "DECLINED"], "pagelen": 50,
        }, doseq=True)
        return list(self.values(f"{self.repo_path}/pullrequests?{query}"))

    def reviewers(self):
        """Use an explicit reviewer UUID or the repository's default reviewers."""
        current_user = self.request("user")["uuid"]
        reviewers = self.explicit_reviewers
        if reviewers is None:
            reviewer = os.environ.get("DEPENDABOT_REVIEWER_UUID")
            reviewers = [reviewer] if reviewer else None
        candidates = ([{"uuid": reviewer} for reviewer in reviewers] if reviewers is not None else
                      list(self.values(f"{self.repo_path}/default-reviewers?pagelen=100")))
        return [{"uuid": user["uuid"]} for user in candidates
                if user["uuid"] != current_user]


def git(directory, *arguments, env=None):
    """Run Git without storing credentials in command arguments or config files."""
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], env=env,
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def run_updates(directory, client, base_sha, configuration, executable):
    """Run a grouped update job; keep credentials in the CLI credential proxy."""
    job = {
        "job": {
            "package-manager": configuration["manager"],
            "allowed-updates": [{"update-type": "all"}],
            "dependency-groups": [{"name": configuration["group"], "rules": {"patterns": ["*"]}}],
            "source": {"provider": "bitbucket", "repo": client.repository,
                       "directory": configuration["directory"], "branch": client.base_branch, "commit": base_sha},
            "security-updates-only": False,
            # Avoid inferring a prefix via private Bitbucket commit-history API calls.
            "commit-message-options": {"prefix": "chore(deps)"},
        },
        "credentials": [{"type": "git_source", "host": "bitbucket.org",
                         "username": "$DEPENDABOT_READ_USERNAME",
                         "password": "$DEPENDABOT_READ_TOKEN"}],
    }
    if "cooldown_days" in configuration and configuration["cooldown_days"] is not None:
        job["job"]["cooldown"] = {"default-days": configuration["cooldown_days"]}
    if configuration["manager"] == "pub":
        job["job"]["requirements-update-strategy"] = "bump_versions"
    job["job"]["ignore-conditions"] = [
            {"dependency-name": name, "update-types": [
                "version-update:semver-major", "version-update:semver-minor",
                "version-update:semver-patch"]}
            for name in configuration.get("ignore", [])
        ]

    input_file = directory / "job.json"
    input_file.write_text(json.dumps(job))

    environment = os.environ.copy()
    environment["DEPENDABOT_READ_USERNAME"] = os.environ.get(
        "DEPENDABOT_READ_USERNAME", GIT_API_TOKEN_USERNAME)
    environment["DEPENDABOT_READ_TOKEN"] = os.environ.get(
        "DEPENDABOT_READ_TOKEN", client.token)

    # The proxy lives in the Docker service; route callbacks to the build container.
    docker_host = os.environ["BITBUCKET_DOCKER_HOST_INTERNAL"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", docker_host):
        raise ValueError("Invalid BITBUCKET_DOCKER_HOST_INTERNAL")
    environment["FAKE_API_HOST"] = "0.0.0.0"
    environment["FAKE_API_PORT"] = "8080"

    output_file = directory / "events.jsonl"
    with output_file.open("w") as output:
        result = subprocess.run([
            str(executable), "update", "-f", str(input_file), "--timeout", "45m",
            "--api-url", f"http://{docker_host}:8080",
        ], env=environment, stdout=output, check=False)

    events = [json.loads(line) for line in output_file.read_text().splitlines() if line]
    errors = [event["data"].get("error-type", event["type"]) for event in events
              if event["type"] in ("record_update_job_error", "record_update_job_unknown_error")]
    if errors:
        raise RuntimeError("Dependabot reported errors: " + ", ".join(
            re.sub(r"[^A-Za-z0-9_-]", "?", str(error))[:100] for error in errors))
    if result.returncode:
        raise RuntimeError(
            f"Dependabot {configuration['manager']} CLI exited with code {result.returncode}; "
            "check the updater and Docker service logs above. If Docker became unreachable, "
            "check the service memory limit in Bitbucket."
        )
    if not any(event["type"] == "mark_as_processed" for event in events):
        raise RuntimeError("Dependabot did not finish processing the job")

    updates = [event["data"] for event in events if event["type"] == "create_pull_request"]
    if len(updates) > 1:
        raise RuntimeError("Expected a single grouped update; refusing to publish partial updates")
    if updates and updates[0]["base-commit-sha"] != base_sha:
        raise ValueError("Dependabot returned an unexpected base commit")
    return updates[0] if updates else None


def apply_files(directory, files):
    """Apply text, binary and executable changes, rejecting paths outside the checkout."""
    for file in files:
        if file.get("support_file"):
            continue

        relative = PurePosixPath(file["directory"].lstrip("/")) / file["name"]
        if relative.is_absolute() or ".." in relative.parts or any(part.lower() == ".git" for part in relative.parts):
            raise ValueError("Unsafe dependency file path")
        target = directory / str(relative)
        if any(parent.is_symlink() for parent in (target, *target.parents)
               if parent != directory and parent.is_relative_to(directory)):
            raise ValueError("Symlink dependency paths are unsupported")
        if not target.resolve().is_relative_to(directory.resolve()) or target.is_symlink():
            raise ValueError("Dependency file escapes the checkout")
        if file.get("symlink_target"):
            raise ValueError("Symlink dependency updates are unsupported")

        if file.get("deleted") or file.get("operation") == "delete":
            target.unlink(missing_ok=True)
        else:
            encoding = file.get("content_encoding") or "utf-8"
            if encoding == "base64":
                content = base64.b64decode(file["content"], validate=True)
            elif encoding == "utf-8":
                content = file["content"].encode()
            else:
                raise ValueError(f"Unsupported dependency file encoding: {encoding}")
            mode = int(file["mode"], 8) if file.get("mode") else None
            if mode is not None and mode not in (0o644, 0o755, 0o100644, 0o100755):
                raise ValueError("Unsupported dependency file mode")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if mode is not None:
                target.chmod(mode & 0o777)


def publish_update(directory, client, update, base_sha, dry_run, configuration):
    """Create or refresh one grouped PR, using a lease to protect concurrent pushes."""
    update_branch = f"dependabot/{configuration['group']}-{client.base_branch}"
    fingerprint = hashlib.sha256(json.dumps(sorted(
        (dependency["name"], dependency.get("version"))
        for dependency in update["dependencies"]
    )).encode()).hexdigest()
    marker = f"<!-- dependabot-update:{fingerprint} -->"

    pull_requests = client.pull_requests(update_branch)
    opened = [pr for pr in pull_requests if pr["state"] == "OPEN"]
    if len(opened) > 1:
        raise RuntimeError("Multiple open Dependabot PRs found")
    if not opened and any(marker in (pr.get("description") or "") for pr in pull_requests):
        print("This update was declined; skipping until dependency versions change.")
        return

    title = update.get("pr-title") or f"Update {configuration['group']} dependencies"
    description = (update.get("pr-body") or title) + "\n\n" + marker
    print(title)
    if dry_run:
        print("Dry run: no branches or pull requests changed.")
        return

    reviewers = client.reviewers() if not opened else None
    previous_sha = client.branch(update_branch)
    checkout = directory / "repository"
    checkout.mkdir()
    environment = os.environ.copy()
    environment.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://bitbucket.org/.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + base64.b64encode(
            f"{GIT_API_TOKEN_USERNAME}:{client.token}".encode()
        ).decode(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Dependabot", "GIT_COMMITTER_NAME": "Dependabot",
        "GIT_AUTHOR_EMAIL": "dependabot@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "dependabot@users.noreply.github.com",
    })

    git(checkout, "init", "--quiet")
    git(checkout, "remote", "add", "origin", f"https://bitbucket.org/{client.repository}.git")
    git(checkout, "fetch", "--depth=1", "origin", client.base_branch, env=environment)
    if git(checkout, "rev-parse", "FETCH_HEAD") != base_sha:
        raise RuntimeError("Base branch changed during the update; rerun the pipeline")
    git(checkout, "checkout", "--detach", base_sha)

    apply_files(checkout, update["updated-dependency-files"])
    git(checkout, "add", "--all")
    if not git(checkout, "diff", "--cached", "--name-only"):
        print("No file changes to publish.")
        return

    tree = git(checkout, "write-tree")
    unchanged = False
    if previous_sha:
        git(checkout, "fetch", "--depth=1", "origin", update_branch, env=environment)
        if git(checkout, "rev-parse", "FETCH_HEAD") != previous_sha:
            raise RuntimeError("Dependabot branch changed during the update; rerun the pipeline")
        unchanged = tree == git(checkout, "rev-parse", f"{previous_sha}^{{tree}}")

    if not unchanged:
        git(checkout, "commit", "--quiet", "-m",
            update.get("commit-message") or title, env=environment)
        git(checkout, "push", f"--force-with-lease=refs/heads/{update_branch}:{previous_sha or ''}",
            "origin", f"HEAD:refs/heads/{update_branch}", env=environment)

    payload = {"title": title, "description": description}
    if opened:
        result = client.request(f"{client.repo_path}/pullrequests/{opened[0]['id']}",
                                "PUT", payload)
    else:
        payload.update({"source": {"branch": {"name": update_branch}},
                        "destination": {"branch": {"name": client.base_branch}},
                        "close_source_branch": True, "reviewers": reviewers})
        result = client.request(f"{client.repo_path}/pullrequests", "POST", payload)
    print("Pull request: " + result["links"]["html"]["href"])


def terminate(signum, frame):
    """Unwind temporary-directory cleanup when the pipeline is terminated."""
    raise SystemExit(128 + signum)


def main(argv=None):
    """Run the scheduled job and remove every temporary file on exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager", required=True, choices=("gradle", "pub"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--ignore", action="append", default=[], help="Dependency name or pattern; repeatable")
    parser.add_argument("--cooldown-days", type=int, help="Omit for upstream behavior; 0 disables cooldown")
    parser.add_argument("--reviewer", action="append", help="Reviewer UUID; repeatable")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", arguments.group):
        parser.error("group must contain only letters, digits, underscores and hyphens")
    if not arguments.directory.startswith("/") or ".." in PurePosixPath(arguments.directory).parts:
        parser.error("directory must be an absolute repository path without ..")
    if arguments.cooldown_days is not None and arguments.cooldown_days < 0:
        parser.error("cooldown-days must be nonnegative")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", arguments.base_branch):
        parser.error("unsupported base branch")
    subprocess.run(["git", "check-ref-format", "--branch", arguments.base_branch],
                   check=True, capture_output=True)

    dry_run = os.environ.get("DEPENDABOT_DRY_RUN", "false")
    if dry_run not in ("true", "false"):
        parser.error("DEPENDABOT_DRY_RUN must be true or false")

    signal.signal(signal.SIGTERM, terminate)
    client = Bitbucket(arguments.base_branch, arguments.reviewer)
    base_sha = client.branch(client.base_branch)
    if not base_sha:
        raise RuntimeError("Base branch does not exist")

    configuration = vars(arguments)
    with tempfile.TemporaryDirectory(prefix="dependabot-") as temporary:
        directory = Path(temporary)
        update = run_updates(directory, client, base_sha, configuration,
                             Path("/usr/local/bin/dependabot"))
        if update:
            publish_update(directory, client, update, base_sha,
                           arguments.dry_run or dry_run == "true", configuration)
        else:
            print("No dependency updates found.")


if __name__ == "__main__":
    main()
