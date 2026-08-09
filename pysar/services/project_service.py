from pathlib import Path

from pysar.core.format.rsar import Brsar

from pysar.services.errors import NoProjectOpenError, SaveFailedError
from pysar.services.models import ProjectSession


class ProjectService:
    def new_session(self) -> ProjectSession:
        return ProjectSession()

    def open_archive(self, path: str | Path) -> ProjectSession:
        archive_path = Path(path)
        archive = Brsar.open(archive_path)
        return ProjectSession(
            archive_path=archive_path,
            archive=archive,
            dirty=archive.is_dirty,
        )

    def save_archive(self, session: ProjectSession, path: str | Path | None = None) -> Path:
        archive = self.require_archive(session)
        target_path = Path(path) if path is not None else session.archive_path
        if target_path is None:
            raise SaveFailedError("no archive path is set for this session")
        archive.save(target_path)
        session.archive_path = target_path
        session.dirty = False
        return target_path

    def close_session(self, session: ProjectSession) -> ProjectSession:
        session.archive = None
        session.archive_path = None
        session.dirty = False
        session.safe_mode = True
        session.destructive_operations_executed = False
        session.undo_snapshot = None
        session.undo_label = None
        session.selected_sound_id = None
        session.selected_bank_index = None
        session.selected_group_index = None
        return session

    def mark_dirty(self, session: ProjectSession) -> None:
        session.dirty = True

    def clear_dirty(self, session: ProjectSession) -> None:
        session.dirty = False

    @staticmethod
    def require_archive(session: ProjectSession) -> Brsar:
        if session.archive is None:
            raise NoProjectOpenError("no archive is open")
        archive: Brsar = session.archive
        return archive
