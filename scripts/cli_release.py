#!/usr/bin/env python3
"""Verify upstream release assets; install the pin or propose a patch update."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
from urllib.request import Request, urlopen


def fetch(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "dependabot-bitbucket"})
    with urlopen(request, timeout=120) as response:
        return response.read()


def release(version):
    endpoint = "latest" if version is None else "tags/v" + version
    data = json.loads(fetch("https://api.github.com/repos/dependabot/cli/releases/" + endpoint))
    version = data["tag_name"].removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or data["draft"] or data["prerelease"]:
        raise ValueError("Expected a stable upstream release")
    name = f"dependabot-v{version}-linux-amd64.tar.gz"
    assets = {asset["name"]: asset for asset in data["assets"]}
    archive = fetch(assets[name]["browser_download_url"])
    digest = hashlib.sha256(archive).hexdigest()
    # Compare the actual download with GitHub's upstream asset digest.
    if assets[name].get("digest") != "sha256:" + digest:
        raise ValueError("Upstream SHA256 asset digest is missing or mismatched; manual review required")
    return version, digest, archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--install", type=Path)
    args = parser.parse_args()
    path = Path("versions.yml")
    pin = json.loads(path.read_text())  # JSON is also YAML; no YAML dependency required.
    version, digest, archive = release(None if args.update else pin["cli_version"])
    if args.update:
        if tuple(map(int, version.split("."))) <= tuple(map(int, pin["cli_version"].split("."))):
            return
        major, minor, patch = map(int, pin["image_version"].split("."))
        pin.update(cli_version=version, cli_sha256=digest, image_version=f"{major}.{minor}.{patch + 1}")
        path.write_text(json.dumps(pin, indent=2) + "\n")
    else:
        if digest != pin["cli_sha256"]:
            raise ValueError("Pinned Dependabot CLI checksum mismatch")
        if args.install:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                members = [m for m in bundle.getmembers() if Path(m.name).name == "dependabot" and m.isfile()]
                if len(members) != 1:
                    raise ValueError("Expected exactly one CLI executable")
                args.install.write_bytes(bundle.extractfile(members[0]).read())
            args.install.chmod(0o755)
    print(f"Verified Dependabot CLI {version}: {digest}")


if __name__ == "__main__":
    main()
