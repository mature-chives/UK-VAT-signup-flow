from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from .config import load_settings
from .runner import AutomationStopped, VatAutomation


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="英国 VAT 注册流程自动化")
    result.add_argument("--config", type=Path, default=Path("vat-config.json"))
    result.add_argument("--resume", action="store_true", help="从上次 URL 恢复")
    result.add_argument(
        "--non-interactive", action="store_true", help="遇到验证码时直接停止"
    )
    result.add_argument(
        "--fresh-session",
        action="store_true",
        help="使用全新临时浏览器配置，从 GOV.UK 入口重新开始",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.fresh_session and args.resume:
        print("运行失败：--fresh-session 不能与 --resume 同时使用。")
        return 1
    try:
        settings = load_settings(args.config)
        if args.fresh_session:
            with tempfile.TemporaryDirectory(prefix="uk-vat-browser-profile-") as directory:
                settings.profile_dir = Path(directory)
                asyncio.run(
                    VatAutomation(
                        settings, interactive=not args.non_interactive
                    ).run(resume=False)
                )
        else:
            asyncio.run(
                VatAutomation(settings, interactive=not args.non_interactive).run(
                    resume=args.resume
                )
            )
    except AutomationStopped as exc:
        print(f"流程已安全停止：{exc}")
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"运行失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
