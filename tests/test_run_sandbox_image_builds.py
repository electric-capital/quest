"""run.py's container image table (``sandbox_image_builds``) against the
shipped Dockerfiles: the two sandbox images are two targets of one
multi-stage ``Dockerfile.script-runner`` (so the LibreOffice + pip base is
built once and shared), the browser sidecar is its own single-stage file,
and every target's COPY'd files are tracked as rebuild sources.
"""

import re
from pathlib import Path

import run

ROOT = Path(__file__).resolve().parents[1]


def _stages(dockerfile: Path) -> list:
    return re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", dockerfile.read_text(), flags=re.M | re.I)


def test_sandbox_images_are_targets_of_one_dockerfile():
    builds = run.sandbox_image_builds("local")
    by_name = {name: (dockerfile, target) for name, dockerfile, target, _ in builds}
    assert by_name["quest-script-runner-local"] == ("Dockerfile.script-runner", "restricted")
    assert by_name["quest-script-runner-public-local"] == ("Dockerfile.script-runner", "public")
    assert by_name["quest-browser-sidecar-local"] == ("Dockerfile.browser-sidecar", None)


def test_every_target_exists_in_its_dockerfile():
    for _, dockerfile, target, _ in run.sandbox_image_builds("local"):
        stages = _stages(ROOT / dockerfile)
        if target is None:
            assert stages == [], dockerfile
        else:
            assert target in stages, (dockerfile, target)
            assert stages[0] == "base"


def test_copied_files_are_rebuild_sources():
    for _, dockerfile, target, extra_sources in run.sandbox_image_builds("local"):
        text = (ROOT / dockerfile).read_text()
        if target:
            # The target's own stage plus the shared base stage.
            parts = re.split(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", text, flags=re.M)
            stages = dict(zip(parts[1::2], parts[2::2]))
            text = stages["base"] + stages[target]
        copied = re.findall(r"^COPY\s+(\S+)\s", text, flags=re.M)
        assert sorted(copied) == sorted(extra_sources), (dockerfile, target)
        for source in extra_sources:
            assert (ROOT / source).exists(), source


def test_build_command_passes_target_only_for_multi_stage():
    assert run.podman_build_command("img", "Dockerfile.x", "public") == [
        "podman", "build", "--network=host", "--target", "public",
        "-t", "img", "-f", "Dockerfile.x", ".",
    ]
    assert run.podman_build_command("img", "Dockerfile.x", None) == [
        "podman", "build", "--network=host", "-t", "img", "-f", "Dockerfile.x", ".",
    ]
