"""Read CLAUDE.md and memory files for a project.

A "context" for a project is:
  - The global CLAUDE.md (`~/.claude/CLAUDE.md`)
  - The project's CLAUDE.md (`<cwd>/CLAUDE.md`)
  - The MEMORY.md index for the project (`~/.claude/projects/<encoded>/memory/MEMORY.md`)
  - Each individual memory file in that directory

Phase 1: read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from helios.backend.projects import CLAUDE_HOME, Project


@dataclass(slots=True)
class ContextFile:
    title: str
    path: Path
    kind: str  # "global-claude-md" | "project-claude-md" | "memory-index" | "memory"
    exists: bool

    def read_text(self) -> str:
        if not self.exists:
            return ""
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def context_files_for(project: Project) -> list[ContextFile]:
    files: list[ContextFile] = []

    global_md = CLAUDE_HOME / "CLAUDE.md"
    files.append(
        ContextFile(
            title="Global CLAUDE.md",
            path=global_md,
            kind="global-claude-md",
            exists=global_md.is_file(),
        )
    )

    proj_md = Path(project.cwd) / "CLAUDE.md"
    files.append(
        ContextFile(
            title="Project CLAUDE.md",
            path=proj_md,
            kind="project-claude-md",
            exists=proj_md.is_file(),
        )
    )

    mem_dir = project.path / "memory"
    if mem_dir.is_dir():
        index = mem_dir / "MEMORY.md"
        if index.is_file():
            files.append(
                ContextFile(
                    title="MEMORY.md (index)",
                    path=index,
                    kind="memory-index",
                    exists=True,
                )
            )
        # individual files
        for entry in sorted(mem_dir.glob("*.md")):
            if entry.name == "MEMORY.md":
                continue
            files.append(
                ContextFile(
                    title=entry.stem,
                    path=entry,
                    kind="memory",
                    exists=True,
                )
            )

    return files
