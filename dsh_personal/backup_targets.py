"""Read-only backup destination checks; never create or mount a target.

Call immediately before and after publishing each remote artifact. Matching the
declared Linux mount identity catches missing/replaced mounts, but path checks
alone cannot close an unmount/remount race during publication. This is not proof
that another physical machine, a separate key copy, or a restore is available.
Mount sources can contain credentials and are never returned or put in errors.
"""
from __future__ import annotations

import os
from pathlib import Path
import re


def _read_mountinfo():
    return Path("/proc/self/mountinfo").read_text()


def _path(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError("备份目标路径必须明确填写")
    try:
        return Path(value).expanduser().absolute().resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("备份目标路径无法解析") from None


def _overlap(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def _no_symlink_components(value):
    try:
        path = Path(value).expanduser().absolute()
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("异机目标及挂载根不能经符号链接定位")
    except OSError:
        raise ValueError("异机目标路径无法检查") from None


def _unescape(value):
    # mountinfo uses octal escapes for whitespace and backslash. Do not treat
    # arbitrary backslash sequences as Python escapes or evaluate input.
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m.group(1), 8)), value)


def _mounts(content):
    result = []
    for line in content.splitlines():
        before, separator, after = line.partition(" - ")
        fields, fs = before.split(), after.split()
        if not separator or len(fields) < 6 or len(fs) < 3:
            continue
        try:
            device = tuple(int(value) for value in fields[2].split(":"))
            if len(device) != 2 or not fields[0].isdigit():
                continue
            mountpoint = Path(_unescape(fields[4]))
            if not mountpoint.is_absolute():
                continue
            result.append({"mount_id": fields[0], "device": device,
                           "mountpoint": mountpoint, "source": _unescape(fs[1]),
                           "fstype": _unescape(fs[0])})
        except (TypeError, ValueError):
            continue
    return result


def validate_backup_targets(data, key_path, local_backup, local_ledger,
                            offsite_dir=None, expected_mount=None):
    """Validate locations without reading key bytes or creating any paths.

    ``expected_mount`` is an operator-supplied dict containing mountpoint,
    source and fstype. These values identify a currently mounted target; they
    do not attest remote-machine independence. Local output roots may be absent
    (the local writer creates them), while an offsite target must already exist.
    """
    data_root, key = _path(data), _path(key_path)
    backup_root, ledger_root = _path(local_backup), _path(local_ledger)
    offsite = _path(offsite_dir) if offsite_dir is not None else None
    roots = [data_root, backup_root, ledger_root] + ([offsite] if offsite else [])
    if any(key.is_relative_to(root) for root in roots):
        raise ValueError("备份密钥必须保存在数据及所有本地/异机输出目录以外")
    for root in (backup_root, ledger_root):
        if root.exists() and not root.is_dir():
            raise ValueError("本地备份及账本目标必须是目录")
    result = {"local_backup": str(backup_root), "local_ledger": str(ledger_root),
              "offsite_dir": str(offsite) if offsite else None,
              "key_separated": True, "offsite_configured": offsite is not None,
              "mount_matches_expected": False, "offsite_verified": False,
              "checks_read_only": True,
              "limitation": "路径预检不能消除检查与写入之间的卸载竞态；匹配挂载不等于异机或独立密钥恢复验证"}
    if offsite is None:
        return result
    if any(_overlap(offsite, root) for root in (data_root, backup_root, ledger_root)):
        raise ValueError("异机目标不能与数据、本地备份或账本目录相同或互为父子目录")
    _no_symlink_components(offsite_dir)
    if not offsite.is_dir():
        raise ValueError("异机目标必须预先存在且已挂载；不会自动创建目录")
    if not isinstance(expected_mount, dict) or any(
        not isinstance(expected_mount.get(field), str) or not expected_mount[field].strip()
        for field in ("mountpoint", "source", "fstype")
    ):
        raise ValueError("异机目标需要明确的预期挂载根、来源和文件系统类型")
    _no_symlink_components(expected_mount["mountpoint"])
    mountpoint = _path(expected_mount["mountpoint"])
    if mountpoint == Path(mountpoint.anchor):
        raise ValueError("异机目标不能以系统根挂载冒充独立目标挂载")
    if not offsite.is_relative_to(mountpoint):
        raise ValueError("异机目标不在声明的挂载根内")
    try:
        mounts = _mounts(_read_mountinfo())
    except (OSError, ValueError, UnicodeError):
        raise ValueError("无法读取挂载身份；异机复制保持停止") from None
    covering = [m for m in mounts if offsite.is_relative_to(m["mountpoint"])]
    if not covering:
        raise ValueError("异机目标未找到有效挂载身份")
    depth = max(len(m["mountpoint"].parts) for m in covering)
    effective = [m for m in covering if len(m["mountpoint"].parts) == depth]
    if len(effective) != 1:
        raise ValueError("异机目标存在重叠挂载歧义；停止复制以待核对")
    actual = effective[0]
    if (actual["mountpoint"] != mountpoint or
            actual["source"] != expected_mount["source"] or
            actual["fstype"] != expected_mount["fstype"]):
        raise ValueError("异机目标当前挂载与预期身份不匹配")
    try:
        device = offsite.stat().st_dev
    except OSError:
        raise ValueError("异机目标已不可访问；停止复制") from None
    if actual["device"] != (os.major(device), os.minor(device)):
        raise ValueError("异机目标设备与挂载记录不一致；停止复制")
    result.update({"mount_matches_expected": True,
                   "mountpoint": str(mountpoint), "fstype": actual["fstype"],
                   "mount_id": actual["mount_id"]})
    return result
