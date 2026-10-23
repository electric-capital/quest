"""Headless LibreOffice in the sandbox images: the Dockerfile's shared
``base`` stage installs it with the fonts, the ``soffice`` wrapper is shipped
and tracked as a rebuild source, and every model-facing instruction that
talks about document conversion points at it (never at a python-docx-built
PDF)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = ("Dockerfile.script-runner",)
SOFFICE_CMD = "soffice --headless --convert-to pdf --outdir /workspace"


class TestImage:
    def test_dockerfiles_install_libreoffice_and_fonts(self):
        for name in DOCKERFILES:
            text = (ROOT / name).read_text()
            for pkg in ("libreoffice-writer-nogui", "libreoffice-calc-nogui",
                        "libreoffice-impress-nogui", "fonts-crosextra-carlito",
                        "fonts-crosextra-caladea", "fonts-liberation",
                        "ttf-mscorefonts-installer", "fontconfig"):
                assert pkg in text, (name, pkg)
            assert "fc-cache -f" in text
            assert "COPY script-runner-soffice.sh /usr/local/bin/soffice" in text
            assert "LIBREOFFICE_PROFILE_DIR=/opt/libreoffice-profile" in text
            # The MS core fonts install must not fail the build offline.
            assert "|| echo" in text.split("ttf-mscorefonts-installer")[-1].split("\n\n")[0]

    def test_fonts_land_before_matplotlib_cache_prebuild(self):
        for name in DOCKERFILES:
            text = (ROOT / name).read_text()
            assert text.index("fc-cache -f") < text.index("MPLCONFIGDIR=/opt/matplotlib"), name

    def test_libreoffice_is_in_the_shared_base_stage(self):
        # Both sandbox images must get LibreOffice from the one `base` stage
        # (built once, layers shared), not from a per-target install.
        text = (ROOT / "Dockerfile.script-runner").read_text()
        base_end = text.index("FROM base AS")
        assert text.count("libreoffice-writer-nogui") == 1
        assert text.index("libreoffice-writer-nogui") < base_end
        assert text.index("COPY script-runner-soffice.sh") < base_end

    def test_wrapper_script(self):
        wrapper = ROOT / "script-runner-soffice.sh"
        text = wrapper.read_text()
        assert text.startswith("#!/bin/bash")
        assert os.access(wrapper, os.X_OK)
        assert "-env:UserInstallation=file://" in text
        assert "/opt/libreoffice-profile" in text
        assert 'exec /usr/bin/soffice' in text

    def test_run_py_tracks_wrapper_as_rebuild_source(self):
        text = (ROOT / "run.py").read_text()
        assert text.count('"script-runner-soffice.sh"') == 2


class TestInstructions:
    def test_workspace_skill_prescribes_libreoffice(self):
        from chat.system_skills.catalog import _workspace_content
        text = _workspace_content("http://localhost", "")
        assert '"soffice", "--headless", "--convert-to", "pdf"' in text
        assert "python-docx" in text and "never stitch" in text
        assert "unless the user asks" in text
        assert "download_drive_file" in text and "google_export_doc" in text

    def test_system_prompts_prescribe_libreoffice(self):
        text = (ROOT / "chat/gemini_api/system_prompt.py").read_text()
        # Top-level rule and sub-agent rule.
        assert text.count(SOFFICE_CMD) >= 2
        assert text.count("build a PDF from `python-docx` output") >= 2

    def test_docs_and_drive_skills_point_at_libreoffice(self):
        from api.docs import get_instructions as docs
        from api.drive import get_instructions as drive
        assert SOFFICE_CMD in docs("http://localhost")
        assert "soffice --headless --convert-to pdf --outdir /workspace report.docx" in drive("http://localhost")
        assert "python-docx" in drive("http://localhost")
