from __future__ import annotations

import sys


def main() -> int:
    from helios import log

    log.setup()
    log.install_excepthook()

    from helios.deps import enforce_runtime_versions

    if not enforce_runtime_versions():
        return 1

    # Before any driver spawns: the Claude driver forwards the Infisical
    # machine identity to interactive sessions, but only what this
    # process has. Launch paths that bypass the systemd user environment left
    # it with nothing to forward. Names only — the value is never logged.
    from helios.backend.process.env_scrub import import_workload_identity

    adopted = import_workload_identity()
    if adopted:
        import logging

        logging.getLogger("helios").info(
            "adopted workload identity from the systemd user environment: %s",
            ", ".join(adopted),
        )

    from helios.app import HeliosApplication

    app = HeliosApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
