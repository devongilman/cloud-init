# Copyright (C) 2011 Canonical Ltd.
# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Haefliger <juerg.haefliger@hp.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

"""Resizefs: cloud-config module which resizes the filesystem"""


import errno
import getopt
import os
import re
import shlex
import stat
from textwrap import dedent

from cloudinit.config.schema import (
    get_schema_doc, validate_cloudconfig_schema)
from cloudinit.settings import PER_ALWAYS
from cloudinit import util

NOBLOCK = "noblock"

frequency = PER_ALWAYS
distros = ['all']

schema = {
    'id': 'cc_resizefs',
    'name': 'Resizefs',
    'title': 'Resize filesystem',
    'description': dedent("""\
        Resize a filesystem to use all avaliable space on partition. This
        module is useful along with ``cc_growpart`` and will ensure that if the
        root partition has been resized the root filesystem will be resized
        along with it. By default, ``cc_resizefs`` will resize the root
        partition and will block the boot process while the resize command is
        running. Optionally, the resize operation can be performed in the
        background while cloud-init continues running modules. This can be
        enabled by setting ``resize_rootfs`` to ``true``. This module can be
        disabled altogether by setting ``resize_rootfs`` to ``false``."""),
    'distros': distros,
    'examples': [
        'resize_rootfs: false  # disable root filesystem resize operation'],
    'frequency': PER_ALWAYS,
    'type': 'object',
    'properties': {
        'resize_rootfs': {
            'enum': [True, False, NOBLOCK],
            'description': dedent("""\
                Whether to resize the root partition. Default: 'true'""")
        }
    }
}

__doc__ = get_schema_doc(schema)  # Supplement python help()


def _resize_btrfs(mount_point, devpth):
    return ('btrfs', 'filesystem', 'resize', 'max', mount_point)


def _resize_ext(mount_point, devpth):
    return ('resize2fs', devpth)


def _resize_xfs(mount_point, devpth):
    return ('xfs_growfs', mount_point)


def _resize_ufs(mount_point, devpth):
    return ('growfs', devpth)


def _get_dumpfs_output(mount_point):
    dumpfs_res, err = util.subp(['dumpfs', '-m', mount_point])
    return dumpfs_res


def _get_gpart_output(part):
    gpart_res, err = util.subp(['gpart', 'show', part])
    return gpart_res


def _can_skip_resize_ufs(mount_point, devpth):
    # extract the current fs sector size
    """
    # dumpfs -m /
    # newfs command for / (/dev/label/rootfs)
      newfs -O 2 -U -a 4 -b 32768 -d 32768 -e 4096 -f 4096 -g 16384
            -h 64 -i 8192 -j -k 6408 -m 8 -o time -s 58719232 /dev/label/rootf
    """
    cur_fs_sz = None
    frag_sz = None
    dumpfs_res = _get_dumpfs_output(mount_point)
    for line in dumpfs_res.splitlines():
        if not line.startswith('#'):
            newfs_cmd = shlex.split(line)
            opt_value = 'O:Ua:s:b:d:e:f:g:h:i:jk:m:o:'
            optlist, args = getopt.getopt(newfs_cmd[1:], opt_value)
            for o, a in optlist:
                if o == "-s":
                    cur_fs_sz = int(a)
                if o == "-f":
                    frag_sz = int(a)
    # check the current partition size
    """
    # gpart show /dev/da0
=>      40  62914480  da0  GPT  (30G)
        40      1024    1  freebsd-boot  (512K)
      1064  58719232    2  freebsd-ufs  (28G)
  58720296   3145728    3  freebsd-swap  (1.5G)
  61866024   1048496       - free -  (512M)
    """
    expect_sz = None
    m = re.search('^(/dev/.+)p([0-9])$', devpth)
    gpart_res = _get_gpart_output(m.group(1))
    for line in gpart_res.splitlines():
        if re.search(r"freebsd-ufs", line):
            fields = line.split()
            expect_sz = int(fields[1])
    # Normalize the gpart sector size,
    # because the size is not exactly the same as fs size.
    normal_expect_sz = (expect_sz - expect_sz % (frag_sz / 512))
    if normal_expect_sz == cur_fs_sz:
        return True
    else:
        return False


# Do not use a dictionary as these commands should be able to be used
# for multiple filesystem types if possible, e.g. one command for
# ext2, ext3 and ext4.
RESIZE_FS_PREFIXES_CMDS = [
    ('btrfs', _resize_btrfs),
    ('ext', _resize_ext),
    ('xfs', _resize_xfs),
    ('ufs', _resize_ufs),
]

RESIZE_FS_PRECHECK_CMDS = {
    'ufs': _can_skip_resize_ufs
}


def rootdev_from_cmdline(cmdline):
    found = None
    for tok in cmdline.split():
        if tok.startswith("root="):
            found = tok[5:]
            break
    if found is None:
        return None

    if found.startswith("/dev/"):
        return found
    if found.startswith("LABEL="):
        return "/dev/disk/by-label/" + found[len("LABEL="):]
    if found.startswith("UUID="):
        return "/dev/disk/by-uuid/" + found[len("UUID="):]

    return "/dev/" + found


def can_skip_resize(fs_type, resize_what, devpth):
    fstype_lc = fs_type.lower()
    for i, func in RESIZE_FS_PRECHECK_CMDS.items():
        if fstype_lc.startswith(i):
            return func(resize_what, devpth)
    return False


def is_device_path_writable_block(devpath, info, log):
    """Return True if devpath is a writable block device.

    @param devpath: Path to the root device we want to resize.
    @param info: String representing information about the requested device.
    @param log: Logger to which logs will be added upon error.

    @returns Boolean True if block device is writable
    """
    container = util.is_container()

    # Ensure the path is a block device.
    if (devpath == "/dev/root" and not os.path.exists(devpath) and
            not container):
        devpath = util.rootdev_from_cmdline(util.get_cmdline())
        if devpath is None:
            log.warn("Unable to find device '/dev/root'")
            return False
        log.debug("Converted /dev/root to '%s' per kernel cmdline", devpath)

    try:
        statret = os.stat(devpath)
    except OSError as exc:
        if container and exc.errno == errno.ENOENT:
            log.debug("Device '%s' did not exist in container. "
                      "cannot resize: %s", devpath, info)
        elif exc.errno == errno.ENOENT:
            log.warn("Device '%s' did not exist. cannot resize: %s",
                     devpath, info)
        else:
            raise exc
        return False

    if not os.access(devpath, os.W_OK):
        if container:
            log.debug("'%s' not writable in container. cannot resize: %s",
                      devpath, info)
        else:
            log.warn("'%s' not writable. cannot resize: %s", devpath, info)
        return

    if not stat.S_ISBLK(statret.st_mode) and not stat.S_ISCHR(statret.st_mode):
        if container:
            log.debug("device '%s' not a block device in container."
                      " cannot resize: %s" % (devpath, info))
        else:
            log.warn("device '%s' not a block device. cannot resize: %s" %
                     (devpath, info))
        return False
    return True


def handle(name, cfg, _cloud, log, args):
    if len(args) != 0:
        resize_root = args[0]
    else:
        resize_root = util.get_cfg_option_str(cfg, "resize_rootfs", True)
    validate_cloudconfig_schema(cfg, schema)
    if not util.translate_bool(resize_root, addons=[NOBLOCK]):
        log.debug("Skipping module named %s, resizing disabled", name)
        return

    # TODO(harlowja): allow what is to be resized to be configurable??
    resize_what = "/"
    result = util.get_mount_info(resize_what, log)
    if not result:
        log.warn("Could not determine filesystem type of %s", resize_what)
        return

    (devpth, fs_type, mount_point) = result

    info = "dev=%s mnt_point=%s path=%s" % (devpth, mount_point, resize_what)
    log.debug("resize_info: %s" % info)

    if not is_device_path_writable_block(devpth, info, log):
        return

    resizer = None
    if can_skip_resize(fs_type, resize_what, devpth):
        log.debug("Skip resize filesystem type %s for %s",
                  fs_type, resize_what)
        return

    fstype_lc = fs_type.lower()
    for (pfix, root_cmd) in RESIZE_FS_PREFIXES_CMDS:
        if fstype_lc.startswith(pfix):
            resizer = root_cmd
            break

    if not resizer:
        log.warn("Not resizing unknown filesystem type %s for %s",
                 fs_type, resize_what)
        return

    resize_cmd = resizer(resize_what, devpth)
    log.debug("Resizing %s (%s) using %s", resize_what, fs_type,
              ' '.join(resize_cmd))

    if resize_root == NOBLOCK:
        # Fork to a child that will run
        # the resize command
        util.fork_cb(
            util.log_time, logfunc=log.debug, msg="backgrounded Resizing",
            func=do_resize, args=(resize_cmd, log))
    else:
        util.log_time(logfunc=log.debug, msg="Resizing",
                      func=do_resize, args=(resize_cmd, log))

    action = 'Resized'
    if resize_root == NOBLOCK:
        action = 'Resizing (via forking)'
    log.debug("%s root filesystem (type=%s, val=%s)", action, fs_type,
              resize_root)


def do_resize(resize_cmd, log):
    try:
        util.subp(resize_cmd)
    except util.ProcessExecutionError:
        util.logexc(log, "Failed to resize filesystem (cmd=%s)", resize_cmd)
        raise
    # TODO(harlowja): Should we add a fsck check after this to make
    # sure we didn't corrupt anything?

# vi: ts=4 expandtab
