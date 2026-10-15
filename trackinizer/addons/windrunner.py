"""The Windrunner research deployment: the addons its trackinizer server runs.

Start the server with it::

    python -m trackinizer.server --addons trackinizer.addons.windrunner.windrunner
"""

from __future__ import annotations

from dataclasses import field

from configgle import Makes

from trackinizer.addons.deployment import Deployment
from trackinizer.addons.research import Research
from trackinizer.addons.supervisor import Supervisor


__all__ = ["Windrunner", "windrunner"]


class Windrunner(Makes["Deployment"], Deployment.Config):
    """Windrunner's site: one field per addon."""

    supervisor: Supervisor.Config | None = field(default_factory=Supervisor.Config)
    """Labels failed agent turns and resumes transient ones; ``None`` turns it off."""

    research: Research.Config | None = field(default_factory=Research.Config)
    """Audits predictions, ledger drift and Beliefs at /api/addons/research/audit."""


def windrunner() -> Windrunner:
    """Return the deployment config the Windrunner server runs."""
    return Windrunner()
