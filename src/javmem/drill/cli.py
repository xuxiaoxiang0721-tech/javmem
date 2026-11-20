"""javmem drill：删除演练（delete）、投毒演练（poison）的命令行（U2 乙）。"""

from __future__ import annotations

import argparse

from javmem.drill.common import Out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="drill_command", required=True)
    from javmem.drill import delete, poison

    delete.add_arguments(commands.add_parser(
        "delete", help="删除演练：拷一份数据目录，撤回（--purge 再彻底删除）一个包，逐层查、逐字节搜（只在拷贝上做）"))
    poison.add_arguments(commands.add_parser(
        "poison", help="投毒演练：编的 8 个毒包在新建的临时数据目录里跑一遍（--fake 用假模型不花钱；真模型要 --budget）"))


def run(args: argparse.Namespace, out: Out = print) -> int:
    if args.drill_command == "delete":
        from javmem.drill.delete import run as run_delete

        return run_delete(args, out)
    from javmem.drill.poison import run as run_poison

    return run_poison(args, out)
