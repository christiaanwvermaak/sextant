"""Sextant backend — production entrypoint.

Run via `python app.py`. Routes register through Tina4's auto-discovery of
`src/routes`.
"""
import os
import sys

# Tina4 3.12+ only honours plain, un-prefixed environment variables when legacy
# env is allowed, and without TINA4_OVERRIDE_CLIENT it exits 2 at boot with no
# message that says why. Both gates have bitten several apps; set them before
# anything imports tina4_python.
os.environ.setdefault("TINA4_ALLOW_LEGACY_ENV", "true")
os.environ.setdefault("TINA4_OVERRIDE_CLIENT", "true")
os.environ.setdefault("PORT", "7145")

from src.app import config as config_mod  # noqa: E402

# DO NOT import src.routes.* here.
#
# run() clears the route registry and rebuilds it with _auto_discover("src"),
# which walks the tree calling importlib.import_module. A module already in
# sys.modules does not re-execute, so its @get decorators never fire the second
# time: the routes are registered by the import here, thrown away by the clear,
# and then not restored. The app boots clean, the framework's own routes answer,
# and every application route 404s. Auto-discovery finds src/routes on its own.


def _preflight():
    """Fail at boot on bad configuration, not at someone's first request.

    A console that starts, serves a sign-in page and only then cannot reach a
    database is worse than one that refuses to start and says why. Every check
    here is something a deployment can get wrong silently.
    """
    try:
        config = config_mod.load()
    except config_mod.ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    problems = []

    # The audit log is not optional. Its whole purpose is to exist at the moment
    # something goes wrong, and discovering it is unwritable *then* is too late.
    audit_dir = os.path.dirname(config.audit_path) or "."
    try:
        os.makedirs(audit_dir, exist_ok=True)
        probe = os.path.join(audit_dir, ".sextant-write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(probe)
    except OSError as exc:
        problems.append(
            f"the audit directory {audit_dir} is not writable ({exc}). "
            "Refusing to start: a change that cannot be recorded must not be possible."
        )

    writable = [c.id for c in config.connections.values() if c.writable]
    if problems:
        for problem in problems:
            print(f"FATAL: {problem}", file=sys.stderr, flush=True)
        sys.exit(1)

    print(
        f"sextant: {len(config.connections)} connection(s), "
        f"{len(writable)} writable {writable or ''}, "
        f"{len(config.auth.providers)} identity provider(s) "
        f"{list(config.auth.providers) or ''}, "
        f"local sign-in {'on' if config.auth.local_enabled else 'off'}, "
        f"audit -> {config.audit_path}",
        flush=True,
    )
    # Said out loud on purpose. A writable connection to production is a thing
    # someone should notice in the logs on the day they configure it.
    for c in config.connections.values():
        if c.writable:
            print(f"sextant: '{c.name}' is WRITABLE"
                  f"{' (changes require confirmation)' if c.confirm_writes else ''}",
                  flush=True)


if __name__ == "__main__":
    _preflight()

    # `from tina4_python.core import run`, NOT `from tina4_python import run`.
    # They are different entrypoints, and the package-level one defaults to the
    # dev reloader: it serves the framework's own routes and none of this
    # application's, so every /api/* path 404s while /health answers 200 from a
    # built-in. The pod looks perfectly healthy and does nothing. The tell is the
    # Tina4 dev toolbar rendered into the 404 body.
    from tina4_python.core import run

    run("0.0.0.0", int(os.environ.get("PORT", 7145)), no_browser=True, no_reload=True)
