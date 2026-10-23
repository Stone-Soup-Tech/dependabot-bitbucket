# Docker image for Dependabot on Bitbucket

Run grouped Gradle and Pub dependency updates using a shared Bitbucket Pipelines step image. Python, Git, Docker client and a verified pinned Dependabot CLI are installed at image build time. Consumers use inline command arguments; no copied scripts, Bitbucket Pipe or hosted server is required.

## Usage

Source: [Stone-Soup-Tech/docker-images-dependabot-bitbucket](https://github.com/Stone-Soup-Tech/docker-images-dependabot-bitbucket). Images are published to `ghcr.io/stone-soup-tech/dependabot-bitbucket`. Use the following image after its first validated publication.

```yaml
image: ghcr.io/stone-soup-tech/dependabot-bitbucket:latest
script:
  - dependabot-bitbucket --manager gradle --directory /android --group android --base-branch dev
```

Use the complete [parallel pipeline example](examples/bitbucket-pipelines.yml), which includes the required Docker service, Runtime v3, independent concurrency groups, dry-run control and Renovate pipeline-image updates. It configures `size: 1x`, `max-time: 30`, `fail-fast: false` and a Docker service with 3072 MB. Merge its sections into your pipeline rather than replacing unrelated steps.

## Credentials and setup

Keep credentials in secured Bitbucket repository/workspace variables:

| Variable | Purpose |
| --- | --- |
| `BOT_USERNAME` | Atlassian account **email** for Bitbucket REST Basic authentication. |
| `BOT_API_TOKEN` | Account API token for REST requests and Git publishing. |
| `DEPENDABOT_READ_TOKEN` | Optional separate read-only Git token used by the updater credential proxy; defaults to `BOT_API_TOKEN`. |
| `DEPENDABOT_READ_USERNAME` | Optional Git identity for the read token; defaults to `x-bitbucket-api-token-auth`. Use that literal or the exact Bitbucket username for API-token Git authentication, **not the REST email**. |

The publishing account needs repository read/write, pull request read/write and user read permissions, plus permission to push `dependabot/*`. Updater read credentials need access to the source repository. Git uses `x-bitbucket-api-token-auth` with the publishing token; the REST client uses the email. These are separate authentication paths even when they share a token.

Repository identity comes from Bitbucket's standard `BITBUCKET_REPO_FULL_NAME`. Docker connectivity comes from the Docker service environment, including `BITBUCKET_DOCKER_HOST_INTERNAL`. Do not override these with ordinary repository settings. For public GHCR packages no image pull secret is required. Private packages require Bitbucket image registry authentication separately from the runner's Git/REST secrets.

## Configuration

Each command runs exactly one manager, directory and group:

```sh
dependabot-bitbucket --manager pub --directory / --group flutter --base-branch dev \
  --reviewer '{reviewer-uuid}' --ignore 'example-package' --dry-run
```

| Argument | Behavior |
| --- | --- |
| `--manager` | Required: `gradle` or `pub`. |
| `--directory` | Required repository-relative absolute path, such as `/android` or `/`. |
| `--group` | Required stable group name; letters, digits, underscores and hyphens. |
| `--base-branch` | Defaults to `main`; independent of the pipeline launch branch. |
| `--ignore` | Repeatable dependency name/pattern ignored for major, minor and patch updates. Empty by default for **both** managers. |
| `--cooldown-days` | Optional nonnegative number. Omitted means upstream default behavior. `0` disables cooldown, allowing undated releases **and newly published dated releases immediately**. |
| `--reviewer` | Repeatable Bitbucket UUID, including braces. Defaults to repository default reviewers; the authenticated account is excluded. |
| `--dry-run` | Resolve updates and inspect existing PRs without publishing branches or PRs. |

The legacy `DEPENDABOT_REVIEWER_UUID` remains a fallback when no `--reviewer` is provided; prefer inline arguments. `DEPENDABOT_DRY_RUN=true|false` remains supported for the pipeline's manual dry-run control. `--dry-run` always enables it. No other ordinary configuration requires repository variables.

Major, minor and patch updates are grouped into one PR per command. Pub uses `bump_versions` for constraints and has **no default ignore list**. PR/commit messages use `chore(deps)` to avoid inferring prefixes through private Bitbucket commit history.

## Scheduling

1. Use the published image and ensure its GHCR package is accessible from Bitbucket. Configure the credentials and add the [parallel pipeline example](examples/bitbucket-pipelines.yml) to your pipeline.
2. Manually run the custom `dependabot` pipeline on the intended base branch with `DEPENDABOT_DRY_RUN=true`. Confirm both managers finish, callbacks arrive, images verify and dependencies resolve. Dry-run still uses credentials, network access and upstream containers.
3. Run with `DEPENDABOT_DRY_RUN=false` to validate branch/PR creation, reviewer selection and updates to existing PRs. An unchanged rerun should not create a duplicate commit; declining a PR suppresses that dependency/version set.
4. In **Pipelines → Schedules**, schedule the custom pipeline on that base branch. Committing YAML does not create a schedule. Keep Renovate's `bitbucket-pipelines` step and configuration; it covers pipeline images and pipes that Dependabot does not manage.

The parallel steps publish independently. One manager can publish even if the other fails; `fail-fast: false` lets both finish. The following Renovate step runs only when both succeed. Keep distinct concurrency groups; use additional unique groups if adding commands that could overlap the same update branch.

## Networking and runtime

The CLI runs inside the build container and starts upstream updater/proxy containers through Bitbucket's Docker service. Its callback HTTP server binds to `0.0.0.0:8080` using `FAKE_API_HOST` and `FAKE_API_PORT`; the CLI receives `--api-url http://$BITBUCKET_DOCKER_HOST_INTERNAL:8080`, routing the proxy back to the build container. Do not substitute `localhost` or a host-Docker socket arrangement. Each parallel step has a separate service and callback server.

At `size: 1x`, 3072 MB goes to Docker and the remaining 1024 MB to the build container. Resolution, especially Flutter bootstrap, can consume substantial memory. Increasing step size alone does not increase the service limit. Inspect each manager's Docker memory metrics before changing its allocation. The CLI timeout is 45 minutes inside the 60-minute step budget.

**An image build or mocked callback test does not prove Bitbucket connectivity.** A real Runtime v3 pipeline run is required to validate the callback route, Docker permissions, credentials, attestation verification and package resolution. This checkout has not yet passed that end-to-end validation.

## Image versions and updates

`versions.yml` is JSON-compatible YAML containing the runner image version and Linux AMD64 CLI version/SHA256. The supplied initial CLI pin is `1.96.0`, SHA256 `44bec839eb57d24a7a8aec36d3cd3d97dc12a99d2b8fb23637add6f4f8142613`. [The upstream release](https://github.com/dependabot/cli/releases/tag/v1.96.0) exists and includes image attestation verification. The build downloads the actual archive, compares its SHA256 with GitHub's release asset digest and the local pin, and fails closed on any mismatch or missing digest. Never substitute a checksum calculated from an unverified download alone.

CI runs offline tests, builds the image with release verification, and smoke-tests its shell and tools. The publishing workflow repeats required validation before publishing on `main` or manual dispatch. It publishes `ghcr.io/stone-soup-tech/dependabot-bitbucket:1.0.0` and a moving `:latest` tag; it refuses to overwrite an existing version tag. Every runner/build change intended for publication must increment `image_version`, even if the CLI is unchanged. `latest` points to the most recently published release; consumers can pin an immutable version instead. Registry administrators must also avoid manually overwriting/deleting version tags; use digests when strict immutability is required.

For manual publication, select **Actions → Publish Dependabot image → Run workflow** on `main`. The `increase-type` input defaults to `NONE`: publish the current `image_version`, or skip successfully if its image already exists. Choose `patch`, `minor` or `major` to increase the version before publishing. The `auto-increase` boolean defaults to `true`, carrying patch and minor at 10 (`1.0.9` → `1.1.0`; `1.9.9` → `2.0.0` for patch). Disable it for standard version increments (`1.0.9` → `1.0.10` for patch). Minor and major increases reset lower components. `NONE` leaves the version unchanged regardless of `auto-increase`. Automatic push runs use `NONE`.

Explicit increases are committed and pushed to `versions.yml` using `GITHUB_TOKEN` before image publication. Repository branch rules must allow this push; the workflow does not bypass protections. Stale runs fail if `main` has advanced. If publication fails after the version commit, rerun with `NONE` to retry that version. The workflow's own token push does not trigger a second push workflow.

For rollback, change the consumer image to a previously validated immutable version, such as `:1.0.0`, or `ghcr.io/stone-soup-tech/dependabot-bitbucket@sha256:<published-digest>`. The `latest` tag follows newly published releases, including major versions. Publication is restricted to `main`. For rollback, pin the consumer rather than moving `latest` backwards.

The daily `check_dependabot_versions.yml` checks **Dependabot CLI releases**, not Renovate. It verifies the latest stable Linux AMD64 archive against the upstream asset digest, updates both version and checksum, increments the runner patch version, tests/builds, and opens an update PR. The normal publishing workflow runs after merge. Breaking runner changes require a manual major-version bump; upstream CLI changes should still be reviewed for compatibility.

For scheduled update PRs:

- The workflow uses the built-in `GITHUB_TOKEN`; no GitHub App, App ID or private key is required. In **Settings → Actions → General → Workflow permissions**, enable **Allow GitHub Actions to create and approve pull requests**. Organization policy must permit it.
- Update PRs request review from `mihaiborcansst`. Tests and the image build run before PR creation. PR workflows created through `GITHUB_TOKEN` may require a maintainer to select **Approve workflows to run**. Review the PR and successful checks before merging; the active workflow does not enable auto-merge.
- Protect `main` and require the `validate` check. Give the publishing workflow package write access and make the GHCR package public if consumers should pull anonymously. Enable Actions and scheduled workflows.
- The previous GitHub App-based workflow is retained as comments at the end of `check_dependabot_versions.yml` for reference only.

For a manual update, run `python3 scripts/cli_release.py --update`, review `versions.yml`, run tests and build the image, then submit a PR. To verify the current pin without changing it, run `python3 scripts/cli_release.py`.

Updater/proxy images remain official upstream images, pulled and attestation-verified by the CLI. They evolve independently of the CLI pin. Pinning this runner image or rolling it back does **not** freeze future updater behavior. They are not bundled or rebuilt here. The image targets `linux/amd64` Bitbucket Cloud steps only.

## PR behavior and limitations

Branches follow `dependabot/<group>-<base-branch>`. Use a unique group for each manager/directory on a base branch. Each command maintains at most one open PR, compares generated trees to avoid unnecessary commits, and uses an explicit force-with-lease to reject concurrent branch changes. Treat these branches as bot-owned. Declining a PR suppresses the same dependency/version fingerprint until versions change. No-update runs leave existing PRs open.

Nonzero CLI exits, updater errors, incomplete processing, multiple grouped results and unexpected base commits prevent publication. Text, base64 binary, executable modes and deletions are supported; traversal, Git metadata paths and symlink updates are rejected. Job files, event output and publishing checkouts are temporary and cleaned up on exit, including handled SIGTERM. Forced termination relies on Bitbucket disposing of the step filesystem. Credentials are passed through environment/proxy authentication, not Git command arguments or stored Git config. API error messages redact tokens; avoid enabling shell tracing around secrets.

This wrapper does not implement Renovate's full PR lifecycle, rate accounting or Dart SDK range strategy. It runs version updates without GitHub Dependabot alerts or an advisory feed. Each example run creates at most two Dependabot PRs; Renovate retains its own limits. Keep consumer branch lint/test checks for `dependabot/*`.

## Development and troubleshooting

```sh
python3 -B -m unittest discover -s test -p 'test_*.py'
docker build -f sdk/Dockerfile -t dependabot-bitbucket:test .
docker run --rm dependabot-bitbucket:test sh -ec 'git --version; docker --version; dependabot --version; dependabot-bitbucket --help'
```

Callback connection errors: confirm Runtime v3, the named Docker service, the internal host variable and port 8080. Docker becoming unreachable: inspect service memory and preceding updater logs. Authentication failures: distinguish Git fetch from REST PR/reviewer API failures; REST email and Git username are different. `Bitbucket::NotFound` in updater metadata lookups does not establish that Git proxy authentication failed. HTTP 200/advisory lookups alone do not establish an update error. Check the final job events and exit status.

Release verification failures: check the upstream release asset metadata and network access; do not disable digest/attestation verification. If the immutable version tag already exists, publication succeeds with a skip notice and leaves all tags unchanged. This checks tag existence, not image content equality. Increment `image_version` to publish new changes. Registry lookup errors still fail the workflow rather than risk overwriting a release.

References: [Dependabot CLI](https://github.com/dependabot/cli), [callback host/API URL explanation](https://github.com/dependabot/cli/issues/377), [Runtime v3](https://support.atlassian.com/bitbucket-cloud/docs/enable-and-use-runtime-v3/), [Docker service memory](https://support.atlassian.com/bitbucket-cloud/kb/pipeline-build-failed-with-container-docker-exceeded-memory-limit-error/).

