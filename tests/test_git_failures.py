"""An update or an install whose git step fails is a failure, and can be done again.

The cases run git on repositories of a temporary directory. The node pack has a submodule, and
the repository of the submodule is moved away to make the step fail after the node pack itself
was pulled or cloned.
"""

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from _install_flags_testutil import GIT_LOCATION
from test_snapshot_restore_pins import core, manager  # noqa: F401


def git(path, *args):
    subprocess.run(['git', '-c', 'protocol.file.allow=always', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', *args],
                   cwd=path, check=True, capture_output=True)


def head(path):
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def git_of_the_case(monkeypatch):
    for name in GIT_LOCATION:
        monkeypatch.delenv(name, raising=False)
    # the git that Manager runs is to clone a submodule from a directory
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'protocol.file.allow')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'always')


@pytest.fixture
def origin(tmp_path):
    """A repository with the submodules `asset` and `library`; its two commits name the two commits of each.

    `pack` is a clone made at the first commit: it is one commit behind, and so are its submodules.
    """
    submodules = tmp_path / 'asset', tmp_path / 'library'
    origin = tmp_path / 'origin'
    for repository in (*submodules, origin):
        repository.mkdir()
        git(repository, 'init', '-q')
    commits = []
    for version in (1, 2):
        for submodule in submodules:
            (submodule / '__init__.py').write_text('VERSION = %d' % version)
            git(submodule, 'add', '.')
            git(submodule, 'commit', '-q', '-m', 'version %d' % version)
            if version == 1:
                git(origin, 'submodule', 'add', '-q', str(submodule), submodule.name)
            else:
                git(origin / submodule.name, 'pull', '-q')
        git(origin, 'commit', '-q', '-a', '-m', 'library %d' % version)
        commits.append(head(origin))
        if version == 1:
            git(tmp_path, 'clone', '-q', '--recursive', str(origin), 'pack')
    return commits


@pytest.fixture
def pack(tmp_path, origin):
    """The clone that is behind, with the repository of its submodule `library` away: `asset` is updated before it fails."""
    (tmp_path / 'library').rename(tmp_path / 'library.away')
    return tmp_path / 'pack'


def test_pull_that_fails_at_the_submodules_leaves_the_pack_at_its_commit(core, tmp_path, origin, pack):  # noqa: F811
    """At the new commit the next update finds nothing to do, and the install script of the update is never run."""
    with pytest.raises(Exception):
        core.git_pull(str(pack))

    assert head(pack) == origin[0]
    # the submodule that was updated before the one that failed is back at the commit that the pack names
    assert (pack / 'asset/__init__.py').read_text() == 'VERSION = 1'

    (tmp_path / 'library.away').rename(tmp_path / 'library')
    core.git_pull(str(pack))

    assert head(pack) == origin[1]
    assert (pack / 'library/__init__.py').read_text() == 'VERSION = 2'
    assert (pack / 'asset/__init__.py').read_text() == 'VERSION = 2'


def test_pull_of_the_git_helper_that_fails_is_a_failure(core, monkeypatch, tmp_path, origin, pack):  # noqa: F811
    """On Windows the pull runs in the git helper, which ends with 0 and says in its output that it failed."""
    monkeypatch.setattr(core.platform, 'system', lambda: 'Windows')
    # the helper looks for this file where ComfyUI is said to be
    (tmp_path / 'folder_paths.py').touch()
    monkeypatch.setenv('COMFYUI_PATH', str(tmp_path))

    with pytest.raises(Exception):
        core.git_pull(str(pack))

    assert head(pack) == origin[0]
    assert (pack / 'asset/__init__.py').read_text() == 'VERSION = 1'

    (tmp_path / 'library.away').rename(tmp_path / 'library')
    core.git_pull(str(pack))

    assert head(pack) == origin[1]


def library_not_cloned(pack):
    """So that the fetch of the git helper passes it, and the update of the submodules is what fails."""
    git(pack, 'submodule', 'deinit', '-q', '-f', 'library')
    # moved, as git makes files read-only that Windows does not remove
    (pack / '.git/modules/library').rename(pack.parent / 'library.modules')


@pytest.fixture
def helper(core, monkeypatch, tmp_path):  # noqa: F811
    """On Windows the pull runs in the git helper."""
    monkeypatch.setattr(core.platform, 'system', lambda: 'Windows')
    # the helper looks for this file where ComfyUI is said to be
    (tmp_path / 'folder_paths.py').touch()
    monkeypatch.setenv('COMFYUI_PATH', str(tmp_path))


def test_pull_of_the_git_helper_that_fails_at_the_submodules_puts_them_back(core, helper, tmp_path, origin):  # noqa: F811
    """The next commit of `asset` has a submodule of its own, whose repository is away: `asset` is checked out before that one fails."""
    asset, nested, pack = tmp_path / 'asset', tmp_path / 'nested', tmp_path / 'pack'
    nested.mkdir()
    git(nested, 'init', '-q')
    git(nested, 'commit', '-q', '--allow-empty', '-m', 'nested')
    git(asset, 'submodule', 'add', '-q', str(nested), 'nested')
    git(asset, 'commit', '-q', '-m', 'a submodule')
    git(tmp_path / 'origin/asset', 'pull', '-q')
    git(tmp_path / 'origin', 'commit', '-q', '-a', '-m', 'asset with a submodule')
    nested.rename(tmp_path / 'nested.away')

    with pytest.raises(Exception):
        core.git_pull(str(pack))

    assert head(pack) == origin[0]
    assert not (pack / 'asset/.gitmodules').exists()


@pytest.mark.parametrize('where', ['in the server process', 'in the git helper'])
def test_pull_that_fails_leaves_a_pack_that_was_not_on_its_branch_as_it_was(core, request, tmp_path, origin, where):  # noqa: F811
    """The pack is at an older commit than its branch, which has a commit of its own: both are where they were."""
    pack = tmp_path / 'pack'
    branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=pack, check=True, capture_output=True, text=True).stdout.strip()
    git(pack, 'pull', '-q')
    git(pack, 'commit', '-q', '--allow-empty', '-m', 'a commit of the pack alone')
    tip = head(pack)
    git(pack, 'checkout', '-q', '--detach', origin[0])
    library_not_cloned(pack)
    (tmp_path / 'library').rename(tmp_path / 'library.away')
    if where == 'in the git helper':
        request.getfixturevalue('helper')

    with pytest.raises(Exception):
        core.git_pull(str(pack))

    assert subprocess.run(['git', 'rev-parse', branch], cwd=pack, check=True, capture_output=True, text=True).stdout.strip() == tip
    assert head(pack) == origin[0]
    assert (pack / 'asset/__init__.py').read_text() == 'VERSION = 1'


def test_install_that_fails_at_the_submodules_leaves_no_directory(core, manager, monkeypatch, tmp_path, origin):  # noqa: F811
    """What is left of the clone is in the way of the next install, which is answered with `Already exists`."""
    (tmp_path / 'library').rename(tmp_path / 'library.away')
    monkeypatch.setattr(core, 'is_valid_url', lambda url: True)
    manager.execute_install_script = Mock(return_value=True)
    url, path = str(tmp_path / 'origin'), tmp_path / 'installed'

    assert manager.repo_install(url, str(path), instant_execution=True).result is False

    assert not path.exists()
    manager.execute_install_script.assert_not_called()

    (tmp_path / 'library.away').rename(tmp_path / 'library')

    assert manager.repo_install(url, str(path), instant_execution=True).result is True
    assert (path / 'library/__init__.py').read_text() == 'VERSION = 2'


# the runner of the server returns the exit code of the git helper, and the default one, which cm-cli has, raises for it
RUNNER_FAILURES = ['returns', 'raises']


def failed_clone(failure):
    def clone_that_fails(command, cwd):
        os.makedirs(command[-1], exist_ok=True)
        # as git leaves the files of its objects
        (Path(command[-1]) / 'object').touch(stat.S_IREAD)
        if failure == 'raises':
            raise subprocess.CalledProcessError(1, command)
        return 1

    return clone_that_fails


@pytest.fixture
def windows(core, monkeypatch):  # noqa: F811
    """Windows, as far as these cases go: the clone runs in the git helper, and a read-only file is not removed."""
    unlink = os.unlink

    def unlink_a_file_that_may_be_written(path, *, dir_fd=None):
        if not os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode & stat.S_IWRITE:
            raise PermissionError(13, 'Access is denied', str(path))
        unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'unlink', unlink_a_file_that_may_be_written)
    monkeypatch.setattr(core.platform, 'system', lambda: 'Windows')


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_whose_clone_in_the_git_helper_fails_leaves_no_directory(core, manager, monkeypatch, windows, tmp_path, failure):  # noqa: F811
    """The git helper is replaced by one that fails after it made the directory."""

    monkeypatch.setattr(core.manager_funcs, 'run_script', failed_clone(failure))

    assert manager.repo_install('https://github.com/owner/fixture', str(tmp_path / 'fixture')).result is False

    assert not (tmp_path / 'fixture').exists()


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_whose_clone_fails_leaves_a_directory_that_was_there(core, manager, monkeypatch, windows, tmp_path, failure):  # noqa: F811
    (tmp_path / 'fixture').mkdir()
    (tmp_path / 'fixture/kept').write_text('a file that was there')
    monkeypatch.setattr(core.manager_funcs, 'run_script', failed_clone(failure))

    assert manager.repo_install('https://github.com/owner/fixture', str(tmp_path / 'fixture')).result is False

    assert (tmp_path / 'fixture/kept').read_text() == 'a file that was there'


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_by_url_whose_clone_in_the_git_helper_fails_leaves_no_directory(core, manager, monkeypatch, windows, tmp_path, failure):  # noqa: F811
    """The git helper is replaced by one that fails after it made the directory."""
    custom_nodes = tmp_path / 'custom_nodes'
    custom_nodes.mkdir()
    monkeypatch.setattr(core.manager_funcs, 'run_script', failed_clone(failure))
    monkeypatch.setattr(core, 'get_default_custom_nodes_path', lambda: str(custom_nodes))
    monkeypatch.setattr(core, 'get_custom_nodes_paths', lambda: [str(custom_nodes)])
    manager.get_cnr_by_repo = Mock(return_value=None)

    assert asyncio.run(core.gitclone_install('https://github.com/owner/fixture')).result is False

    assert os.listdir(custom_nodes) == []


@pytest.fixture
def made_by_another_install(core, monkeypatch, tmp_path):  # noqa: F811
    """A directory that a check does not see: another install can make it after the check, before the clone starts."""
    destination = tmp_path / 'custom_nodes/fixture'
    destination.mkdir(parents=True)
    (destination / 'kept').write_text('of another install')
    exists = os.path.exists
    monkeypatch.setattr(os.path, 'exists', lambda path: False if str(path) == str(destination) else exists(path))
    return destination


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_whose_clone_fails_leaves_a_directory_that_another_install_made(core, manager, monkeypatch, windows, made_by_another_install, failure):  # noqa: F811
    monkeypatch.setattr(core.manager_funcs, 'run_script', failed_clone(failure))

    assert manager.repo_install('https://github.com/owner/fixture', str(made_by_another_install)).result is False

    assert (made_by_another_install / 'kept').is_file()


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_by_url_whose_clone_fails_leaves_a_directory_that_another_install_made(core, manager, monkeypatch, windows, made_by_another_install, failure):  # noqa: F811
    custom_nodes = made_by_another_install.parent
    monkeypatch.setattr(core.manager_funcs, 'run_script', failed_clone(failure))
    monkeypatch.setattr(core, 'get_default_custom_nodes_path', lambda: str(custom_nodes))
    monkeypatch.setattr(core, 'get_custom_nodes_paths', lambda: [str(custom_nodes)])
    manager.get_cnr_by_repo = Mock(return_value=None)

    assert asyncio.run(core.gitclone_install('https://github.com/owner/fixture')).result is False

    assert (made_by_another_install / 'kept').is_file()


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='the folder can be removed here')
def test_what_a_failed_clone_left_and_cannot_be_removed_is_reported(core, manager, monkeypatch, caplog, tmp_path):  # noqa: F811
    """As a file that another program has open on Windows: here a file in a folder that may not be written."""
    def clone_that_fails(command, cwd):
        held = Path(command[-1]) / 'held'
        held.mkdir(parents=True, exist_ok=True)
        (held / 'file').touch()
        held.chmod(0o555)
        return 1

    monkeypatch.setattr(core.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(core.manager_funcs, 'run_script', clone_that_fails)

    try:
        assert manager.repo_install('https://github.com/owner/fixture', str(tmp_path / 'fixture')).result is False
    finally:
        (tmp_path / 'fixture/held').chmod(0o755)

    assert [record for record in caplog.records if str(tmp_path / 'fixture') in record.getMessage()]


def test_install_by_url_that_fails_at_the_submodules_leaves_no_directory(core, manager, monkeypatch, tmp_path, origin):  # noqa: F811
    (tmp_path / 'library').rename(tmp_path / 'library.away')
    custom_nodes = tmp_path / 'custom_nodes'
    custom_nodes.mkdir()
    monkeypatch.setattr(core, 'get_default_custom_nodes_path', lambda: str(custom_nodes))
    monkeypatch.setattr(core, 'get_custom_nodes_paths', lambda: [str(custom_nodes)])
    monkeypatch.setattr(core, 'is_valid_url', lambda url: True)
    monkeypatch.setattr(core, 'execute_install_script', Mock(return_value=True))
    manager.get_cnr_by_repo = Mock(return_value=None)

    assert asyncio.run(core.gitclone_install(str(tmp_path / 'origin'), instant_execution=True)).result is False

    assert os.listdir(custom_nodes) == []
    core.execute_install_script.assert_not_called()
