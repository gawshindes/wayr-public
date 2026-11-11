"""Build the environment a Modal deployment needs from the files we already keep.

A production newsletter is a `newsletters/<name>/` folder plus the region's env
file (API keys, WordPress, Supabase). The Modal secret is the two merged, minus
what must not travel:

- the local state paths (`*_FILE` for the cache, ledger, run log, taxonomy):
  the deploy injects `/data/...` defaults and the user secret would override
  them, so a local `data/<name>/...` path in the secret would silently write
  into the ephemeral container instead of the volume;
- `PIPELINE_MODE`: the deploy injects the mode last, from WAYR_PIPELINE_MODE.

Paths into `newsletters/` are made absolute (`/root/newsletters/...`) so they
resolve regardless of the container's working directory.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

STATE_FILE_KEYS = frozenset(
    {
        "ARTICLE_CACHE_FILE",
        "FEED_LEDGER_FILE",
        "NEWSLETTER_RUNS_FILE",
        "WP_TAXONOMY_CACHE_FILE",
    }
)
INJECTED_KEYS = frozenset({"PIPELINE_MODE"})
REMOTE_ROOT = "/root/"


def parse_env_text(text: str) -> Dict[str, str]:
    """KEY=VALUE lines; `#` comments and blanks ignored; later keys win.
    A trailing `  # comment` after a value is stripped, matching dotenv's habit
    for unquoted values."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if val.startswith("#"):  # `KEY=   # comment` is an empty value
            val = ""
        elif val and not (val.startswith('"') or val.startswith("'")):
            val = val.split("  #", 1)[0].split(" #", 1)[0].strip()
        elif val[:1] in ('"', "'") and val[-1:] == val[:1]:
            val = val[1:-1]
        out[key.strip()] = val
    return out


def assemble_secret(
    base: Dict[str, str], overrides: Dict[str, str]
) -> Tuple[Dict[str, str], List[str]]:
    """Merge base + overrides into the secret's environment.

    Returns (env, notes): the environment to upload, and one line per key that
    was dropped or rewritten so the operator sees exactly what changed."""
    merged = {**base, **overrides}
    env: Dict[str, str] = {}
    notes: List[str] = []
    for key, val in merged.items():
        if key in STATE_FILE_KEYS:
            notes.append(f"dropped {key} (the /data volume default applies)")
            continue
        if key in INJECTED_KEYS:
            notes.append(
                f"dropped {key} (injected by the deploy from WAYR_PIPELINE_MODE)"
            )
            continue
        if val.startswith("newsletters/"):
            val = REMOTE_ROOT + val
            notes.append(f"{key}: made absolute → {val}")
        env[key] = val
    # The settings validator would catch these at run time — on Modal, after the
    # deploy. Say it now, while the operator is looking.
    mailer = env.get("NEWSLETTER_MAILER", "none").lower()
    needs = {
        "resend": ("RESEND_API_KEY", "RESEND_SEGMENT_ID", "NEWSLETTER_FROM"),
        "resend-direct": (
            "RESEND_API_KEY",
            "NEWSLETTER_FROM",
            "NEWSLETTER_UNSUBSCRIBE_URL",
            "SUPABASE_URL",
            "SUPABASE_SECRET_KEY",
        ),
    }
    aliases = {"SUPABASE_SECRET_KEY": ("SUPABASE_SERVICE_ROLE_KEY",)}
    for k in needs.get(mailer, ()):
        if not env.get(k) and not any(env.get(a) for a in aliases.get(k, ())):
            notes.append(f"WARNING: NEWSLETTER_MAILER={mailer} but {k} is empty")
    return env, notes


def redact(env: Dict[str, str], keep: Iterable[str] = ()) -> Dict[str, str]:
    """Secrets shown as <set>; everything else verbatim."""
    keep = set(keep)
    out = {}
    for k, v in env.items():
        secret = any(t in k for t in ("KEY", "SECRET", "TOKEN", "PASSWORD", "URN"))
        out[k] = v if (k in keep or not secret or not v) else "<set>"
    return out
