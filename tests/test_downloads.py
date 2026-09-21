"""download_file and run_installer: the two net-new file-system-touching
tools, and the safety properties around them (size cap, filename
sanitisation, an installer that's always confirmed individually)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from jarvis.core.errors import ConfirmationDeclined
from jarvis.security import consequence
from jarvis.tools.downloads.installer import RunInstallerTool
from jarvis.tools.downloads.tools import DownloadFileTool, _avoid_collision, _safe_filename
from jarvis.tools.macos.controller import ShellResult

# -- fake httpx plumbing, mirroring the async-context-manager protocol
# DownloadFileTool actually calls: AsyncClient() as client, client.stream() as response.

class _FakeResponse:
    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def raise_for_status(self):
        request = httpx.Request("GET", "https://x.example/file")
        httpx.Response(self.status_code, request=request).raise_for_status()

    async def aiter_bytes(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url):
        return _FakeStream(self._response)


def _install_fake_httpx(monkeypatch, response):
    import jarvis.tools.downloads.tools as downloads_module

    monkeypatch.setattr(downloads_module.httpx, "AsyncClient", lambda **kw: _FakeClient(response))


# -- filename sanitisation ----------------------------------------------------

def test_safe_filename_strips_directory_components_and_traversal():
    assert _safe_filename("report.pdf") == "report.pdf"
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("/etc/passwd") == "passwd"
    assert _safe_filename("..") == ""
    assert _safe_filename("") == ""


def test_avoid_collision_appends_a_counter(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("existing")
    resolved = _avoid_collision(target)
    assert resolved.name == "file-1.txt"


# -- DownloadFileTool -----------------------------------------------------------
#
# Writing inside the JARVIS workspace is LOW risk (see FileSandbox.classify),
# same as any write_file call there — no confirmation is ever needed, so
# most of these tests use a workspace-relative destination to exercise the
# download mechanics in isolation. Writing *outside* the workspace (to
# ~/Downloads, or here to a plain tmp_path) is genuinely HIGH risk, same as
# write_file targeting ~/Documents — and `always_confirm` (default
# ["high"]) means that risk tier always asks, regardless of auto_approve;
# the two tests that care about that path approve or don't approve it
# explicitly, in test_a_download_outside_the_workspace_requires_confirmation
# and test_a_download_is_covered_by_the_task_grant below.

def _workspace_dest(app) -> str:
    dest = app.deps.sandbox.root / "downloads-test"
    dest.mkdir(parents=True, exist_ok=True)
    return str(dest)


async def test_download_rejects_a_non_http_scheme(app, ctx):
    outcome = await DownloadFileTool(app.deps).run({"url": "file:///etc/passwd"}, ctx)
    assert outcome.ok is False


async def test_download_saves_the_body_and_reports_its_size(app, ctx, monkeypatch):
    dest = _workspace_dest(app)
    _install_fake_httpx(monkeypatch, _FakeResponse(body=b"hello world"))
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/hello.txt", "destination": dest}, ctx
    )
    assert outcome.ok is True
    saved = Path(dest) / "hello.txt"
    assert saved.read_bytes() == b"hello world"
    assert outcome.data["bytes"] == 11


async def test_download_prefers_an_explicit_filename_over_the_url(app, ctx, monkeypatch):
    dest = _workspace_dest(app)
    _install_fake_httpx(monkeypatch, _FakeResponse(body=b"data"))
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/hello.txt", "filename": "renamed.bin", "destination": dest}, ctx
    )
    assert outcome.ok is True
    assert (Path(dest) / "renamed.bin").exists()


async def test_download_sanitises_a_path_traversal_filename(app, ctx, monkeypatch):
    dest = _workspace_dest(app)
    _install_fake_httpx(monkeypatch, _FakeResponse(body=b"data"))
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/hello.txt", "filename": "../../etc/evil", "destination": dest}, ctx
    )
    assert outcome.ok is True
    # Never escaped dest, whatever the caller asked for.
    assert (Path(dest) / "evil").exists()
    assert not list(Path(dest).parent.glob("evil"))


async def test_download_sanitises_a_path_traversal_content_disposition(app, ctx, monkeypatch):
    dest = _workspace_dest(app)
    _install_fake_httpx(monkeypatch, _FakeResponse(
        body=b"data", headers={"content-disposition": 'attachment; filename="../../evil.sh"'}
    ))
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/hello", "destination": dest}, ctx
    )
    assert outcome.ok is True
    assert (Path(dest) / "evil.sh").exists()


async def test_download_enforces_the_size_cap_mid_stream(app, ctx, monkeypatch):
    dest = _workspace_dest(app)
    app.config_store.update({"automation": {"max_download_mb": 0}})  # any body at all exceeds it
    _install_fake_httpx(monkeypatch, _FakeResponse(body=b"x" * 1000))
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/big.bin", "destination": dest}, ctx
    )
    assert outcome.ok is False
    assert not list(Path(dest).glob("*.bin"))  # nothing left behind, including the .part file
    assert not list(Path(dest).glob("*.part"))


async def test_download_reports_an_http_error_status(app, ctx, monkeypatch):
    dest = _workspace_dest(app)
    _install_fake_httpx(monkeypatch, _FakeResponse(status_code=404))
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/missing.txt", "destination": dest}, ctx
    )
    assert outcome.ok is False


async def test_a_download_outside_the_workspace_requires_confirmation(app, ctx, monkeypatch, tmp_path):
    """~/Downloads (or any non-workspace destination) is graded by the same
    FileSandbox every write_file call already goes through — a download is
    not exempt from that."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15,
                                          "readable_roots": [str(tmp_path)]}})
    _install_fake_httpx(monkeypatch, _FakeResponse(body=b"data"))
    with pytest.raises(ConfirmationDeclined):
        await DownloadFileTool(app.deps).run(
            {"url": "https://x.example/hello.txt", "destination": str(tmp_path)}, ctx
        )


async def test_a_download_is_covered_by_the_task_grant(app, monkeypatch, tmp_path):
    """A download inside an approved automation task is routine — it
    shouldn't need its own fresh confirmation on top of the task's own."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.5,
                                          "readable_roots": [str(tmp_path)]}})
    _install_fake_httpx(monkeypatch, _FakeResponse(body=b"data"))
    task = app.deps.tasks.create("automation", "test")
    app.deps.permissions.grant_task(task.id)
    ctx = app.deps.tool_context(task=task)
    outcome = await DownloadFileTool(app.deps).run(
        {"url": "https://x.example/hello.txt", "destination": str(tmp_path)}, ctx
    )
    assert outcome.ok is True


# -- RunInstallerTool -----------------------------------------------------------

def test_run_installer_confirmation_template_renders_a_human_prompt_not_a_shell_string():
    """The whole point of confirmation_template (vs the generic
    "{description} ({detail})" fallback every other tool gets): the user
    sees something they recognise, not a raw shell invocation."""
    from jarvis.tools.registry import _confirmation_text

    prompt = _confirmation_text(RunInstallerTool.spec, {"path": "/Users/x/Downloads/py.pkg"})
    assert prompt == ("Run the installer at /Users/x/Downloads/py.pkg? This changes your "
                      "system and may ask for your password.")


def test_confirmation_text_falls_back_when_the_template_cites_a_missing_argument():
    """A template referencing a key that isn't in the call's arguments must
    degrade to the generic phrasing rather than raising KeyError out of the
    permission gate itself."""
    from jarvis.tools.base import ToolSpec
    from jarvis.tools.registry import _confirmation_text

    spec = ToolSpec(name="x", description="Do a thing", confirmation_template="Run {missing_key}?")
    assert _confirmation_text(spec, {"path": "/x"}) == "Do a thing (path=/x)"


def test_run_installer_is_always_confirmed_individually():
    spec = RunInstallerTool.spec
    assert spec.always_confirm_individually is True
    assert consequence.classify("run_installer", {"path": "/x.pkg"}, spec) is True


async def test_run_installer_reports_a_missing_file(app, ctx, tmp_path):
    app.config_store.update({"security": {"readable_roots": [str(tmp_path)]}})
    outcome = await RunInstallerTool(app.deps).run({"path": str(tmp_path / "nope.pkg")}, ctx)
    assert outcome.ok is False
    assert "can't find" in outcome.summary.lower()


async def test_run_installer_rejects_an_unsupported_extension(app, ctx, tmp_path):
    app.config_store.update({"security": {"readable_roots": [str(tmp_path)]}})
    target = tmp_path / "readme.txt"
    target.write_text("hi")
    outcome = await RunInstallerTool(app.deps).run({"path": str(target)}, ctx)
    assert outcome.ok is False
    assert "isn't a .pkg" in outcome.summary


async def test_run_installer_refuses_a_forbidden_system_path(app, ctx):
    outcome = await RunInstallerTool(app.deps).run({"path": "/System/Applications/x.pkg"}, ctx)
    assert outcome.ok is False
    assert "protected system location" in outcome.summary.lower()


async def test_run_installer_runs_a_pkg_via_argv_never_a_shell_string(app, ctx, monkeypatch, tmp_path):
    app.config_store.update({"security": {"readable_roots": [str(tmp_path)]}})
    target = tmp_path / "thing.pkg"
    target.write_bytes(b"")
    calls = []

    async def fake_run(argv, timeout=20.0, stdin=None):
        calls.append(argv)
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "run", fake_run)
    outcome = await RunInstallerTool(app.deps).run({"path": str(target)}, ctx)
    assert outcome.ok is True
    assert calls == [["/usr/sbin/installer", "-pkg", str(target), "-target", "/"]]


async def test_run_installer_reports_a_failed_pkg_install(app, ctx, monkeypatch, tmp_path):
    target = tmp_path / "thing.pkg"
    target.write_bytes(b"")

    async def fake_run(argv, timeout=20.0, stdin=None):
        return ShellResult(1, "", "installer: bad package")

    monkeypatch.setattr(app.deps.controller, "run", fake_run)
    outcome = await RunInstallerTool(app.deps).run({"path": str(target)}, ctx)
    assert outcome.ok is False


async def test_run_installer_mounts_a_dmg_finds_the_pkg_and_detaches(app, ctx, monkeypatch, tmp_path):
    app.config_store.update({"security": {"readable_roots": [str(tmp_path)]}})
    dmg = tmp_path / "thing.dmg"
    dmg.write_bytes(b"")
    mount_dir = tmp_path / "Volumes" / "Thing"
    mount_dir.mkdir(parents=True)
    (mount_dir / "Installer.pkg").write_bytes(b"")
    calls = []

    async def fake_run(argv, timeout=20.0, stdin=None):
        calls.append(argv)
        if argv[:2] == ["/usr/bin/hdiutil", "attach"]:
            return ShellResult(0, f"/dev/disk4\t\t\t{mount_dir}\n", "")
        if argv[0] == "/usr/sbin/installer":
            return ShellResult(0, "", "")
        if argv[:2] == ["/usr/bin/hdiutil", "detach"]:
            return ShellResult(0, "", "")
        raise AssertionError(f"unexpected call: {argv}")  # pragma: no cover

    monkeypatch.setattr(app.deps.controller, "run", fake_run)
    outcome = await RunInstallerTool(app.deps).run({"path": str(dmg)}, ctx)
    assert outcome.ok is True
    assert any(c[0] == "/usr/sbin/installer" and str(mount_dir / "Installer.pkg") in c for c in calls)
    assert any(c[:2] == ["/usr/bin/hdiutil", "detach"] for c in calls)


async def test_run_installer_leaves_an_app_only_dmg_mounted_for_the_user(app, ctx, monkeypatch, tmp_path):
    app.config_store.update({"security": {"readable_roots": [str(tmp_path)]}})
    dmg = tmp_path / "thing.dmg"
    dmg.write_bytes(b"")
    mount_dir = tmp_path / "Volumes" / "Thing"
    mount_dir.mkdir(parents=True)
    (mount_dir / "Thing.app").mkdir()
    run_calls = []
    open_calls = []

    async def fake_run(argv, timeout=20.0, stdin=None):
        run_calls.append(argv)
        if argv[:2] == ["/usr/bin/hdiutil", "attach"]:
            return ShellResult(0, f"/dev/disk4\t\t\t{mount_dir}\n", "")
        raise AssertionError(f"unexpected call: {argv}")  # pragma: no cover

    async def fake_open_url(url, browser=None):
        open_calls.append(url)
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "run", fake_run)
    monkeypatch.setattr(app.deps.controller, "open_url", fake_open_url)
    outcome = await RunInstallerTool(app.deps).run({"path": str(dmg)}, ctx)
    assert outcome.ok is True
    assert "stopped" in outcome.summary.lower()
    assert open_calls == [str(mount_dir)]
    # Never detached — the volume must stay mounted for the user to act on.
    assert not any(c[:2] == ["/usr/bin/hdiutil", "detach"] for c in run_calls)
