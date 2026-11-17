"""An update or an install whose git step fails is a failure, and can be done again.

The cases run git on repositories of a temporary directory, in the isolated child of
tests/test_install_flags_config.py. The node pack has a submodule, and the repository of
the submodule is moved away to make the step fail after the node pack itself was pulled
or cloned.
"""

import os

import pytest

from test_install_flags_config import _run_child


PRELUDE = '''
import asyncio, os, pathlib, subprocess
from unittest.mock import AsyncMock, Mock
core = manager_core
tmp = pathlib.Path(tmp)
# the git that the Manager runs is to clone a submodule from a directory
os.environ.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='protocol.file.allow', GIT_CONFIG_VALUE_0='always')
# the git helper looks for this file where ComfyUI is said to be
(tmp / 'folder_paths.py').touch()
os.environ['COMFYUI_PATH'] = str(tmp)
nodes = tmp / 'custom_nodes'
nodes.mkdir()
core.get_default_custom_nodes_path = lambda: str(nodes)
core.get_custom_nodes_paths = lambda: [str(nodes)]
core.is_valid_url = lambda url: True
manager = core.UnifiedManager()
manager.reload = AsyncMock()
manager.get_custom_nodes = AsyncMock(return_value={})
manager.get_cnr_by_repo = Mock(return_value=None)
manager.execute_install_script = Mock(return_value=True)
core.unified_manager = manager
core.execute_install_script = Mock(return_value=True)

def git(path, *args):
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', *args], cwd=path, check=True, capture_output=True)

def head(path):
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=path, check=True, capture_output=True, text=True).stdout.strip()

def failed(call, *args, **options):
    try:
        call(*args, **options)
    except Exception:
        return True
    return False

# `origin` has the submodules `asset` and `library`; its two commits name the two commits of each.
# `pack` is a clone made at the first commit: it is one commit behind, and so are its submodules.
# The repository of `library` is away: `asset` is updated before the update fails.
asset, library, origin, pack = tmp / 'asset', tmp / 'library', tmp / 'origin', tmp / 'pack'
for repository in (asset, library, origin):
    repository.mkdir()
    git(repository, 'init', '-q')
commits = []
for version in (1, 2):
    for submodule in (asset, library):
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
        git(tmp, 'clone', '-q', '--recursive', str(origin), 'pack')
away = tmp / 'library.away'
library.rename(away)
'''

# a pull that failed, the commit after it, and the pull done again with the repository of the submodule back
PULL_TWICE = '''
first = failed(core.git_pull, str(pack))
after_first = head(pack) == commits[0]
asset_after_first = (pack / 'asset/__init__.py').read_text()
away.rename(library)
second = failed(core.git_pull, str(pack))
print(json.dumps({'first failed': first, 'at its commit': after_first, 'asset at its commit': asset_after_first, 'second failed': second,
                  'updated': head(pack) == commits[1], 'library': (pack / 'library/__init__.py').read_text()}))
'''
# `asset`, which was updated before the update failed, is back at the commit that the pack names
PULLED = {'first failed': True, 'at its commit': True, 'asset at its commit': 'VERSION = 1', 'second failed': False, 'updated': True, 'library': 'VERSION = 2'}


def run(body):
    return _run_child(PRELUDE + body)


def test_pull_that_fails_at_the_submodules_leaves_the_pack_at_its_commit():
    """At the new commit the next update finds nothing to do, and the install script of the update is never run."""
    assert run(PULL_TWICE) == PULLED


def test_pull_of_the_git_helper_that_fails_is_a_failure():
    """On Windows the pull runs in the git helper, which ends with 0 and says in its output that it failed."""
    assert run("core.platform.system = lambda: 'Windows'\n" + PULL_TWICE) == PULLED


# `library` is not cloned, so that the fetch of the git helper passes it and the update of the submodules is what fails
LIBRARY_NOT_CLONED = '''
git(pack, 'submodule', 'deinit', '-q', '-f', 'library')
# moved, as git makes files read-only that Windows does not remove
(pack / '.git/modules/library').rename(tmp / 'library.modules')
'''


def test_pull_of_the_git_helper_that_fails_at_the_submodules_puts_them_back():
    """The next commit of `asset` has a submodule of its own, whose repository is away: `asset` is checked out before that one fails."""
    data = run('''
away.rename(library)
nested = tmp / 'nested'
nested.mkdir()
git(nested, 'init', '-q')
git(nested, 'commit', '-q', '--allow-empty', '-m', 'nested')
git(asset, 'submodule', 'add', '-q', str(nested), 'nested')
git(asset, 'commit', '-q', '-m', 'a submodule')
git(origin / 'asset', 'pull', '-q')
git(origin, 'commit', '-q', '-a', '-m', 'asset with a submodule')
nested.rename(tmp / 'nested.away')
core.platform.system = lambda: 'Windows'
first = failed(core.git_pull, str(pack))
print(json.dumps({'failed': first, 'at its commit': head(pack) == commits[0], 'asset at its commit': not (pack / 'asset/.gitmodules').exists()}))
''')
    assert data == {'failed': True, 'at its commit': True, 'asset at its commit': True}


@pytest.mark.parametrize('where', ['', "core.platform.system = lambda: 'Windows'\n"], ids=['in the server process', 'in the git helper'])
def test_pull_that_fails_leaves_a_pack_that_was_not_on_its_branch_as_it_was(where):
    """The pack is at an older commit than its branch, which has a commit of its own: both are where they were."""
    data = run('''
away.rename(library)
branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=pack, check=True, capture_output=True, text=True).stdout.strip()
git(pack, 'pull', '-q')
git(pack, 'commit', '-q', '--allow-empty', '-m', 'a commit of the pack alone')
tip = head(pack)
git(pack, 'checkout', '-q', '--detach', commits[0])
library.rename(away)
''' + LIBRARY_NOT_CLONED + where + '''
first = failed(core.git_pull, str(pack))
print(json.dumps({'failed': first, 'branch at its commit': subprocess.run(['git', 'rev-parse', branch], cwd=pack, check=True, capture_output=True, text=True).stdout.strip() == tip,
                  'pack at its commit': head(pack) == commits[0], 'asset': (pack / 'asset/__init__.py').read_text()}))
''')
    assert data == {'failed': True, 'branch at its commit': True, 'pack at its commit': True, 'asset': 'VERSION = 1'}


def test_install_that_fails_at_the_submodules_leaves_no_directory():
    """What is left of the clone is in the way of the next install."""
    data = run('''
path = tmp / 'installed'
first = manager.repo_install(str(origin), str(path), instant_execution=True).result
left = path.exists()
away.rename(library)
second = manager.repo_install(str(origin), str(path), instant_execution=True).result
print(json.dumps({'first': first, 'left': left, 'second': second, 'library': (path / 'library/__init__.py').read_text()}))
''')
    assert data == {'first': False, 'left': False, 'second': True, 'library': 'VERSION = 2'}


def test_install_by_url_that_fails_at_the_submodules_leaves_no_directory():
    data = run('''
result = asyncio.run(core.gitclone_install(str(origin), instant_execution=True)).result
print(json.dumps({'result': result, 'left': os.listdir(nodes), 'install scripts': core.execute_install_script.call_count}))
''')
    assert data == {'result': False, 'left': [], 'install scripts': 0}


# the runner of the server returns the exit code of the git helper, and the default one, which cm-cli has, raises for it
RUNNER_FAILURES = ['return 1', 'raise subprocess.CalledProcessError(1, command)']

# Windows, as far as these cases go: the clone runs in the git helper, and a read-only file is not removed.
# The helper is replaced by one that fails after it made the directory, with a read-only file in it as git leaves its pack files.
WINDOWS = '''
import stat
unlink = os.unlink

def unlink_a_file_that_may_be_written(path, *, dir_fd=None):
    if not os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode & stat.S_IWRITE:
        raise PermissionError(13, 'Access is denied', str(path))
    unlink(path, dir_fd=dir_fd)

def clone_that_fails(command, cwd):
    os.makedirs(command[-1], exist_ok=True)
    (pathlib.Path(command[-1]) / 'pack').touch(stat.S_IREAD)
    FAILURE

os.unlink = unlink_a_file_that_may_be_written
core.platform.system = lambda: 'Windows'
core.manager_funcs.run_script = clone_that_fails
'''


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_whose_clone_in_the_git_helper_fails_leaves_no_directory(failure):
    data = run((WINDOWS + '''
result = manager.repo_install('https://github.com/owner/fixture', str(tmp / 'fixture')).result
print(json.dumps({'result': result, 'left': (tmp / 'fixture').exists()}))
''').replace('FAILURE', failure))
    assert data == {'result': False, 'left': False}


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_whose_clone_fails_leaves_a_directory_that_was_there(failure):
    data = run((WINDOWS + '''
(tmp / 'fixture').mkdir()
(tmp / 'fixture/kept').write_text('a file that was there')
result = manager.repo_install('https://github.com/owner/fixture', str(tmp / 'fixture')).result
print(json.dumps({'result': result, 'kept': (tmp / 'fixture/kept').read_text()}))
''').replace('FAILURE', failure))
    assert data == {'result': False, 'kept': 'a file that was there'}


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_by_url_whose_clone_in_the_git_helper_fails_leaves_no_directory(failure):
    data = run((WINDOWS + '''
result = asyncio.run(core.gitclone_install('https://github.com/owner/fixture')).result
print(json.dumps({'result': result, 'left': os.listdir(nodes)}))
''').replace('FAILURE', failure))
    assert data == {'result': False, 'left': []}


# A check that saw no directory is no proof that this install made it: another install can make it before the clone starts.
MADE_BY_ANOTHER_INSTALL = '''
exists = os.path.exists
DESTINATION.mkdir(parents=True)
(DESTINATION / 'kept').write_text('of another install')
os.path.exists = lambda path: False if str(path) == str(DESTINATION) else exists(path)
'''


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_whose_clone_fails_leaves_a_directory_that_another_install_made(failure):
    data = run((WINDOWS + MADE_BY_ANOTHER_INSTALL.replace('DESTINATION', "(tmp / 'fixture')") + '''
result = manager.repo_install('https://github.com/owner/fixture', str(tmp / 'fixture')).result
print(json.dumps({'result': result, 'kept': (tmp / 'fixture/kept').is_file()}))
''').replace('FAILURE', failure))
    assert data == {'result': False, 'kept': True}


@pytest.mark.parametrize('failure', RUNNER_FAILURES)
def test_install_by_url_whose_clone_fails_leaves_a_directory_that_another_install_made(failure):
    data = run((WINDOWS + MADE_BY_ANOTHER_INSTALL.replace('DESTINATION', "(nodes / 'fixture')") + '''
result = asyncio.run(core.gitclone_install('https://github.com/owner/fixture')).result
print(json.dumps({'result': result, 'kept': (nodes / 'fixture/kept').is_file()}))
''').replace('FAILURE', failure))
    assert data == {'result': False, 'kept': True}


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='the folder can be removed here')
def test_what_a_failed_clone_left_and_cannot_be_removed_is_reported():
    """As a file that another program has open on Windows: here a file in a folder that may not be written."""
    data = run('''
import logging
messages = []

class Messages(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())

def clone_that_fails(command, cwd):
    held = pathlib.Path(command[-1]) / 'held'
    held.mkdir(parents=True, exist_ok=True)
    (held / 'file').touch()
    held.chmod(0o555)
    return 1

logging.getLogger().addHandler(Messages())
core.platform.system = lambda: 'Windows'
core.manager_funcs.run_script = clone_that_fails
result = manager.repo_install('https://github.com/owner/fixture', str(tmp / 'fixture')).result
(tmp / 'fixture/held').chmod(0o755)
print(json.dumps({'result': result, 'reported': [message for message in messages if str(tmp / 'fixture') in message] != []}))
''')
    assert data == {'result': False, 'reported': True}
