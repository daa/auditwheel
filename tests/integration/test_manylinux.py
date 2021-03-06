from contextlib import contextmanager
import docker
from subprocess import CalledProcessError
import pytest
import io
import os
import os.path as op
import shutil
import logging
import zipfile
from auditwheel.policy import get_priority_by_name
from elftools.elf.elffile import ELFFile


logger = logging.getLogger(__name__)


ENCODING = 'utf-8'
MANYLINUX1_IMAGE_ID = 'quay.io/pypa/manylinux1_x86_64'
MANYLINUX2010_IMAGE_ID = 'quay.io/pypa/manylinux2010_x86_64'
MANYLINUX_IMAGES = {
    'manylinux1': MANYLINUX1_IMAGE_ID,
    'manylinux2010': MANYLINUX2010_IMAGE_ID,
}
DOCKER_CONTAINER_NAME = 'auditwheel-test-manylinux'
PYTHON_IMAGE_ID = 'python:3.5'
DEVTOOLSET = {
    'manylinux1': 'devtoolset-2',
    'manylinux2010': 'devtoolset-8',
}
PATH_DIRS = [
    '/opt/python/cp35-cp35m/bin',
    '/opt/rh/{devtoolset}/root/usr/bin',
    '/usr/local/sbin',
    '/usr/local/bin',
    '/usr/sbin',
    '/usr/bin',
    '/sbin',
    '/bin',
]
PATH = {k: ':'.join(PATH_DIRS).format(devtoolset=v)
        for k, v in DEVTOOLSET.items()}
WHEEL_CACHE_FOLDER = op.expanduser('~/.cache/auditwheel_tests')
ORIGINAL_NUMPY_WHEEL = 'numpy-1.11.0-cp35-cp35m-linux_x86_64.whl'
ORIGINAL_SIX_WHEEL = 'six-1.11.0-py2.py3-none-any.whl'


def find_src_folder():
    candidate = op.abspath(op.join(op.dirname(__file__), '../..'))
    contents = os.listdir(candidate)
    if 'setup.py' in contents and 'auditwheel' in contents:
        return candidate


def docker_start(image, volumes={}, env_variables={}):
    """Start a long waiting idle program in container

    Return the container object to be used for 'docker exec' commands.
    """
    # Make sure to use the latest public version of the docker image
    client = docker.from_env()
    logger.info("Pulling docker image %r", image)
    client.images.pull(image)

    dvolumes = {host: {'bind': ctr, 'mode': 'rw'}
                for (ctr, host) in volumes.items()}

    logger.info("Starting container with image %r", image)
    con = client.containers.run(image, ['sleep', '10000'], detach=True,
                                volumes=dvolumes, environment=env_variables)
    logger.info("Started container %s", con.id[:12])
    return con


@contextmanager
def docker_container_ctx(image, io_dir, env_variables={}):
    src_folder = find_src_folder()
    if src_folder is None:
        pytest.skip('Can only be run from the source folder')
    vols = {'/io': io_dir, '/auditwheel_src': src_folder}

    container = docker_start(image, vols, env_variables)
    try:
        yield container
    finally:
        container.remove(force=True)


def docker_exec(container, cmd):
    logger.info("docker exec %s: %r", container.id[:12], cmd)
    ec, output = container.exec_run(cmd)
    output = output.decode(ENCODING)
    if ec != 0:
        raise CalledProcessError(ec, cmd, output=output)
    return output


@pytest.fixture()
def io_folder(tmp_path):
    d = tmp_path / 'io'
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture()
def docker_python(io_folder):
    with docker_container_ctx(PYTHON_IMAGE_ID, io_folder) as container:
        docker_exec(container, 'pip install -U pip')
        yield container


@pytest.fixture(params=MANYLINUX_IMAGES.keys())
def any_manylinux_container(request, io_folder):
    policy = request.param
    image = MANYLINUX_IMAGES[policy]
    env = {'PATH': PATH[policy]}

    with docker_container_ctx(image, io_folder, env) as container:
        docker_exec(container, 'pip install -U pip setuptools')
        docker_exec(container, 'pip install -U /auditwheel_src')
        yield policy, container


def test_build_repair_numpy(any_manylinux_container, docker_python, io_folder):
    # Integration test: repair numpy built from scratch

    # First build numpy from source as a naive linux wheel that is tied
    # to system libraries (atlas, libgfortran...)
    policy, manylinux_ctr = any_manylinux_container
    docker_exec(manylinux_ctr, 'yum install -y atlas atlas-devel')

    if op.exists(op.join(WHEEL_CACHE_FOLDER, policy, ORIGINAL_NUMPY_WHEEL)):
        # If numpy has already been built and put in cache, let's reuse this.
        shutil.copy2(op.join(WHEEL_CACHE_FOLDER, policy, ORIGINAL_NUMPY_WHEEL),
                     op.join(io_folder, ORIGINAL_NUMPY_WHEEL))
    else:
        # otherwise build the original linux_x86_64 numpy wheel from source
        # and put the result in the cache folder to speed-up future build.
        # This part of the build is independent of the auditwheel code-base
        # so it's safe to put it in cache.
        docker_exec(manylinux_ctr,
            'pip wheel -w /io --no-binary=:all: numpy==1.11.0')
        os.makedirs(op.join(WHEEL_CACHE_FOLDER, policy), exist_ok=True)
        shutil.copy2(op.join(io_folder, ORIGINAL_NUMPY_WHEEL),
                     op.join(WHEEL_CACHE_FOLDER, policy, ORIGINAL_NUMPY_WHEEL))
    filenames = os.listdir(io_folder)
    assert filenames == [ORIGINAL_NUMPY_WHEEL]
    orig_wheel = filenames[0]
    assert 'manylinux' not in orig_wheel

    # Repair the wheel using the manylinux container
    repair_command = (
        'auditwheel repair --plat {policy}_x86_64 -w /io /io/{orig_wheel}'
    ).format(policy=policy, orig_wheel=orig_wheel)
    docker_exec(manylinux_ctr, repair_command)
    filenames = os.listdir(io_folder)

    assert len(filenames) == 2
    repaired_wheels = [fn for fn in filenames if 'manylinux' in fn]
    assert repaired_wheels == ['numpy-1.11.0-cp35-cp35m-{}_x86_64.whl'.format(policy)]
    repaired_wheel = repaired_wheels[0]
    output = docker_exec(manylinux_ctr, 'auditwheel show /io/' + repaired_wheel)
    assert (
        'numpy-1.11.0-cp35-cp35m-{policy}_x86_64.whl is consistent'
        ' with the following platform tag: "{policy}_x86_64"'
    ).format(policy=policy) in output.replace('\n', ' ')

    # Check that the repaired numpy wheel can be installed and executed
    # on a modern linux image.
    docker_exec(docker_python, 'pip install /io/' + repaired_wheel)
    output = docker_exec(docker_python,
        'python /auditwheel_src/tests/integration/quick_check_numpy.py')
    assert output.strip() == 'ok'

    # Check that numpy f2py works with a more recent version of gfortran
    docker_exec(docker_python, 'apt-get update -yqq')
    docker_exec(docker_python, 'apt-get install -y gfortran')
    docker_exec(docker_python, 'python -m numpy.f2py'
                           ' -c /auditwheel_src/tests/integration/foo.f90 -m foo')

    # Check that the 2 fortran runtimes are well isolated and can be loaded
    # at once in the same Python program:
    docker_exec(docker_python, ["python", "-c", "'import numpy; import foo'"])


def test_build_wheel_with_binary_executable(any_manylinux_container, docker_python,
                                            io_folder):
    # Test building a wheel that contains a binary executable (e.g., a program)

    policy, manylinux_ctr = any_manylinux_container
    docker_exec(manylinux_ctr, 'yum install -y gsl-devel')

    docker_exec(
        manylinux_ctr,
        ['bash', '-c', 'cd /auditwheel_src/tests/integration/testpackage && python -m pip wheel --no-deps -w /io .']
    )

    filenames = os.listdir(io_folder)
    assert filenames == ['testpackage-0.0.1-py3-none-any.whl']
    orig_wheel = filenames[0]
    assert 'manylinux' not in orig_wheel

    # Repair the wheel using the appropriate manylinux container
    repair_command = (
        'auditwheel repair --plat {policy}_x86_64 -w /io /io/{orig_wheel}'
    ).format(policy=policy, orig_wheel=orig_wheel)
    docker_exec(manylinux_ctr, repair_command)
    filenames = os.listdir(io_folder)
    assert len(filenames) == 2
    repaired_wheels = [fn for fn in filenames if policy in fn]
    # Wheel picks up newer symbols when built in manylinux2010
    expected_wheel_name = 'testpackage-0.0.1-py3-none-%s_x86_64.whl' % policy
    assert repaired_wheels == [expected_wheel_name]
    repaired_wheel = repaired_wheels[0]
    output = docker_exec(manylinux_ctr, 'auditwheel show /io/' + repaired_wheel)
    assert (
        'testpackage-0.0.1-py3-none-{policy}_x86_64.whl is consistent'
        ' with the following platform tag: "{policy}_x86_64"'
    ).format(policy=policy) in output.replace('\n', ' ')

    docker_exec(docker_python, 'pip install /io/' + repaired_wheel)
    output = docker_exec(
        docker_python,
        ['python', '-c', 'from testpackage import runit; print(runit(1.5))']
    )
    assert output.strip() == '2.25'


@pytest.mark.parametrize('with_dependency', ['0', '1'])
def test_build_wheel_with_image_dependencies(with_dependency, any_manylinux_container, docker_python,
                                             io_folder):
    # try to repair the wheel targeting different policies
    #
    # with_dependency == 0
    #   The python module has no dependencies that should be grafted-in and
    #   uses versioned symbols not available on policies pre-dating the policy
    #   matching the image being tested.
    # with_dependency == 1
    #   The python module itself does not use versioned symbols but has a
    #   dependency that should be grafted-in that uses versioned symbols not
    #   available on policies pre-dating the policy matching the image being
    #   tested.

    policy, manylinux_ctr = any_manylinux_container

    docker_exec(manylinux_ctr, [
        'bash', '-c',
        'cd /auditwheel_src/tests/integration/testdependencies &&'
        'WITH_DEPENDENCY={} python setup.py -v build_ext -f bdist_wheel -d '
        '/io'.format(with_dependency)])

    filenames = os.listdir(io_folder)
    orig_wheel = filenames[0]
    assert 'manylinux' not in orig_wheel

    repair_command = \
        'LD_LIBRARY_PATH=/auditwheel_src/tests/integration/testdependencies '\
        'auditwheel -v repair --plat {policy}_x86_64 -w /io /io/{orig_wheel}'

    policy_priority = get_priority_by_name(policy + '_x86_64')
    older_policies = \
        [p for p in MANYLINUX_IMAGES.keys()
         if policy_priority < get_priority_by_name(p + '_x86_64')]
    for target_policy in older_policies:
        # we shall fail to repair the wheel when targeting an older policy than
        # the one matching the image
        with pytest.raises(CalledProcessError):
            docker_exec(manylinux_ctr, [
                'bash',
                '-c',
                repair_command.format(policy=target_policy,
                                      orig_wheel=orig_wheel)])

    # check all works properly when targeting the policy matching the image
    docker_exec(manylinux_ctr, [
        'bash', '-c',
        repair_command.format(policy=policy, orig_wheel=orig_wheel)])
    filenames = os.listdir(io_folder)
    assert len(filenames) == 2
    repaired_wheels = [fn for fn in filenames if policy in fn]
    expected_wheel_name = \
        'testdependencies-0.0.1-cp35-cp35m-%s_x86_64.whl' % policy
    assert repaired_wheels == [expected_wheel_name]
    repaired_wheel = repaired_wheels[0]
    output = docker_exec(manylinux_ctr, 'auditwheel show /io/' + repaired_wheel)
    assert (
        'testdependencies-0.0.1-cp35-cp35m-{policy}_x86_64.whl is consistent'
        ' with the following platform tag: "{policy}_x86_64"'
    ).format(policy=policy) in output.replace('\n', ' ')

    # check the original wheel with a dependency was not compliant
    # and check the one without a dependency was already compliant
    output = docker_exec(manylinux_ctr, 'auditwheel show /io/' + orig_wheel)
    if with_dependency == '1':
        assert (
            '{orig_wheel} is consistent with the following platform tag: '
            '"linux_x86_64"'
        ).format(orig_wheel=orig_wheel) in output.replace('\n', ' ')
    else:
        assert (
            '{orig_wheel} is consistent with the following platform tag: '
            '"{policy}_x86_64"'
        ).format(orig_wheel=orig_wheel, policy=policy) in output.replace('\n', ' ')

    docker_exec(docker_python, 'pip install /io/' + repaired_wheel)
    docker_exec(
        docker_python,
        ['python', '-c',
         'from sys import exit; from testdependencies import run; exit(run())']
    )


def test_build_repair_pure_wheel(any_manylinux_container, io_folder):
    policy, manylinux_ctr = any_manylinux_container

    if op.exists(op.join(WHEEL_CACHE_FOLDER, policy, ORIGINAL_SIX_WHEEL)):
        # If six has already been built and put in cache, let's reuse this.
        shutil.copy2(op.join(WHEEL_CACHE_FOLDER, policy,  ORIGINAL_SIX_WHEEL),
                     op.join(io_folder, ORIGINAL_SIX_WHEEL))
    else:
        docker_exec(manylinux_ctr, 'pip wheel -w /io --no-binary=:all: six==1.11.0')
        os.makedirs(op.join(WHEEL_CACHE_FOLDER, policy), exist_ok=True)
        shutil.copy2(op.join(io_folder, ORIGINAL_SIX_WHEEL),
                     op.join(WHEEL_CACHE_FOLDER, policy, ORIGINAL_SIX_WHEEL))

    filenames = os.listdir(io_folder)
    assert filenames == [ORIGINAL_SIX_WHEEL]
    orig_wheel = filenames[0]
    assert 'manylinux' not in orig_wheel

    # Repair the wheel using the manylinux container
    repair_command = (
        'auditwheel repair --plat {policy}_x86_64 -w /io /io/{orig_wheel}'
    ).format(policy=policy, orig_wheel=orig_wheel)
    docker_exec(manylinux_ctr, repair_command)
    filenames = os.listdir(io_folder)
    assert len(filenames) == 1  # no new wheels
    assert filenames == [ORIGINAL_SIX_WHEEL]

    output = docker_exec(manylinux_ctr, 'auditwheel show /io/' + filenames[0])
    assert ''.join([
        ORIGINAL_SIX_WHEEL,
        ' is consistent with the following platform tag: ',
        '"manylinux1_x86_64".  ',
        'The wheel references no external versioned symbols from system- ',
        'provided shared libraries.  ',
        'The wheel requires no external shared libraries! :)',
    ]) in output.replace('\n', ' ')


def test_build_wheel_depending_on_library_with_rpath(any_manylinux_container, docker_python,
                                                     io_folder):
    # Test building a wheel that contains an extension depending on a library with RPATH set

    policy, manylinux_ctr = any_manylinux_container

    docker_exec(
        manylinux_ctr,
        [
            'bash',
            '-c',
            (
                'cd /auditwheel_src/tests/integration/testrpath '
                '&& rm -rf build '
                '&& python setup.py bdist_wheel -d /io'
            ),
        ]
    )

    filenames = os.listdir(io_folder)
    assert filenames == ['testrpath-0.0.1-cp35-cp35m-linux_x86_64.whl']
    orig_wheel = filenames[0]
    assert 'manylinux' not in orig_wheel

    # Repair the wheel using the appropriate manylinux container
    repair_command = (
        'auditwheel repair --plat {policy}_x86_64 -w /io /io/{orig_wheel}'
    ).format(policy=policy, orig_wheel=orig_wheel)
    docker_exec(
        manylinux_ctr,
        ['bash', '-c', 'LD_LIBRARY_PATH=/auditwheel_src/tests/integration/testrpath/a ' + repair_command],
    )
    filenames = os.listdir(io_folder)
    repaired_wheels = [fn for fn in filenames if policy in fn]
    # Wheel picks up newer symbols when built in manylinux2010
    expected_wheel_name = (
        'testrpath-0.0.1-cp35-cp35m-{policy}_x86_64.whl'
    ).format(policy=policy)
    assert expected_wheel_name in repaired_wheels
    repaired_wheel = expected_wheel_name
    output = docker_exec(manylinux_ctr, 'auditwheel show /io/' + repaired_wheel)
    assert (
        'testrpath-0.0.1-cp35-cp35m-{policy}_x86_64.whl is consistent'
        ' with the following platform tag: "manylinux1_x86_64"'
    ).format(policy=policy) in output.replace('\n', ' ')

    docker_exec(docker_python, 'pip install /io/' + repaired_wheel)
    output = docker_exec(
        docker_python,
        ['python', '-c', 'from testrpath import testrpath; print(testrpath.func())']
    )
    assert output.strip() == '11'
    with zipfile.ZipFile(os.path.join(io_folder, repaired_wheel)) as w:
        for name in w.namelist():
            if 'testrpath/.libs/lib' in name:
                with w.open(name) as f:
                    elf = ELFFile(io.BytesIO(f.read()))
                    dynamic = elf.get_section_by_name('.dynamic')
                    if '.libs/liba' in name:
                        rpath_tags = [t for t in dynamic.iter_tags() if t.entry.d_tag == 'DT_RPATH']
                        assert len(rpath_tags) == 1
                        assert rpath_tags[0].rpath == '$ORIGIN/.'
