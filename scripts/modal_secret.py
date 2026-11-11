#!/usr/bin/env python3
"""Build (and, with --apply, upload) the Modal secret for a production newsletter.

    poetry run python scripts/modal_secret.py --env-file .env.usa --newsletter wayrtoday            # dry run: keys and notes
    poetry run python scripts/modal_secret.py --env-file .env.usa --newsletter wayrtoday --apply    # modal secret create --force wayr-secrets-prod

The secret is the region env file merged with newsletters/<name>/env.overrides,
minus the local state paths and PIPELINE_MODE (see wayr/utils/deploy_env.py).
Uses the active Modal profile (`modal profile current`); set MODAL_PROFILE to
pick the workspace. The secret name follows modal_deploy.py: wayr-secrets-<env>.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wayr.utils.deploy_env import assemble_secret, parse_env_text, redact  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--env-file",
        required=True,
        help="the region env file with the keys (e.g. .env.usa)",
    )
    ap.add_argument("--newsletter", required=True, help="folder under newsletters/")
    ap.add_argument(
        "--env",
        default="prod",
        help="deployment environment; the secret is wayr-secrets-<env>, what modal_deploy.py reads (default prod)",
    )
    ap.add_argument(
        "--secret-name",
        default=None,
        help="Modal secret to write (default: wayr-secrets-<env>)",
    )
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override one key for this upload (e.g. NEWSLETTER_DELIVERY=draft for a first issue); repeatable",
    )
    ap.add_argument(
        "--apply", action="store_true", help="upload with `modal secret create --force`"
    )
    a = ap.parse_args()
    a.secret_name = a.secret_name or f"wayr-secrets-{a.env}"

    base = parse_env_text(Path(a.env_file).read_text(encoding="utf-8"))
    ov_path = Path("newsletters") / a.newsletter / "env.overrides"
    if not ov_path.is_file():
        print(f"no such newsletter: {ov_path}", file=sys.stderr)
        return 2
    overrides = parse_env_text(ov_path.read_text(encoding="utf-8"))
    for item in a.set:
        if "=" not in item:
            print(f"--set expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        overrides[k.strip()] = v.strip()
    env, notes = assemble_secret(base, overrides)
    for item in a.set:
        notes.append(f"--set {item} (this upload only; the files are unchanged)")

    print(f"Secret '{a.secret_name}' from {a.env_file} + {ov_path}: {len(env)} keys\n")
    for k, v in redact(env).items():
        print(f"  {k}={v}")
    print("\nAdjustments:")
    for n in notes:
        print(f"  - {n}")
    if not a.apply:
        print("\ndry run — re-run with --apply to upload")
        return 0

    cmd = ["modal", "secret", "create", "--force", a.secret_name] + [
        f"{k}={v}" for k, v in env.items()
    ]
    print(f"\nuploading {len(env)} keys to Modal secret '{a.secret_name}' …")
    res = subprocess.run(cmd, text=True, capture_output=True)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
