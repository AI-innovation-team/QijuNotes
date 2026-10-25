#!/usr/bin/env python3
"""Set/reset this project's account locally; passwords never enter argv or output."""
import argparse
import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsh_personal.auth import AuthError, AuthStorageError, AuthStore


def main():
    parser = argparse.ArgumentParser(description='本机设置或重设起居账号；密码只在交互输入中读取。')
    parser.add_argument('command', choices=['setup', 'reset', 'status'])
    parser.add_argument('--data', default=str(Path.home() / '.local/share/dsh-personal'))
    args = parser.parse_args()
    auth = AuthStore(args.data)
    try:
        if args.command == 'status':
            print('账号已设置。' if auth.configured() else '账号尚未设置。')
            return 0
        if not sys.stdin.isatty():
            parser.error('请在本机交互终端运行；不从参数、管道或环境变量读取密码。')
        if args.command == 'setup' and auth.configured():
            raise AuthError('账号已存在；需要重设时使用 reset。')
        username = input('账号（2至64字符）：')
        password = getpass.getpass('密码（至少10字符，输入不显示）：')
        confirmation = getpass.getpass('再输入一次密码：')
        if password != confirmation:
            raise AuthError('两次输入不一致，未修改账号。')
        if args.command == 'setup':
            # setup creates a browser-shaped session for its common API; revoke
            # it immediately because this CLI never delivers a cookie.
            result = auth.setup(username, password)
            auth.logout(result['session_id'])
        else:
            auth.reset_account(username, password)
        print('账号已保存。请使用账号密码登录；已有登录会话已撤销。' if args.command == 'reset'
              else '账号已保存。现在可以使用账号密码登录。')
        return 0
    except (AuthError, AuthStorageError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
