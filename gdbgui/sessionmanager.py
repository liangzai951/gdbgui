import logging
import traceback
from typing import Any, Dict, List, Optional, Set
from pygdbmi.IoManager import IoManager
from collections import defaultdict
from .ptylib import Pty
import os
import datetime
import signal

logger = logging.getLogger(__name__)


class DebugSession:
    def __init__(
        self,
        *,
        pygdbmi_controller: IoManager,
        pty_for_gdbgui: Pty,
        pty_for_gdb: Pty,
        pty_for_debugged_program: Pty,
        command: str,
        mi_version: str,
        pid: int,
    ):
        self.command = command
        self.pygdbmi_controller = pygdbmi_controller
        self.pty_for_gdbgui = pty_for_gdbgui
        self.pty_for_gdb = pty_for_gdb
        self.pty_for_debugged_program = pty_for_debugged_program
        self.mi_version = mi_version
        self.pid = pid
        self.start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.client_ids: Set[str] = set()

    def terminate(self):
        if self.pid:
            os.kill(self.pid, signal.SIGKILL)
        self.pygdbmi_controller = None

    def to_dict(self):
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "command": self.command,
            "c2": "hi",
            "client_ids": list(self.client_ids),
        }

    def add_client(self, client_id: str):
        self.client_ids.add(client_id)

    def remove_client(self, client_id: str):
        self.client_ids.discard(client_id)
        if len(self.client_ids) == 0:
            self.terminate()


class SessionManager(object):
    def __init__(self, app_config: Dict[str, Any]):
        self.debug_session_to_client_ids: Dict[DebugSession, List[str]] = defaultdict(
            list
        )  # key is controller, val is list of client ids

        self.gdb_reader_thread = None
        self.config = app_config

    def connect_client_to_debug_session(
        self, *, desired_gdbpid: int, client_id: str
    ) -> DebugSession:
        debug_session = self.debug_session_from_pid(desired_gdbpid)

        if not debug_session:
            raise ValueError(f"No existing gdb process with pid {desired_gdbpid}")
        debug_session.add_client(client_id)
        self.debug_session_to_client_ids[debug_session].append(client_id)
        return debug_session

    def add_new_debug_session(
        self, *, gdb_command: str, mi_version: str, client_id: str
    ) -> DebugSession:
        pty_for_gdb = Pty(cmd=gdb_command)
        pty_for_debugged_program = Pty()
        pty_for_gdbgui = Pty(echo=False)
        pty_for_gdb.write(f"new-ui {mi_version} {pty_for_gdbgui.name}\n")
        pty_for_gdb.write(f"set inferior-tty {pty_for_debugged_program.name}\n")

        pid = pty_for_gdb.pid
        debug_session = DebugSession(
            pygdbmi_controller=IoManager(
                os.fdopen(pty_for_gdbgui.stdin, mode="wb", buffering=0),
                os.fdopen(pty_for_gdbgui.stdout, mode="rb", buffering=0),
                None,
            ),
            pty_for_gdbgui=pty_for_gdbgui,
            pty_for_gdb=pty_for_gdb,
            pty_for_debugged_program=pty_for_debugged_program,
            command=gdb_command,
            mi_version=mi_version,
            pid=pid,
        )
        debug_session.add_client(client_id)
        self.debug_session_to_client_ids[debug_session] = [client_id]
        return debug_session

    def remove_debug_session_by_pid(self, gdbpid: int) -> List[str]:
        debug_session = self.debug_session_from_pid(gdbpid)
        if debug_session:
            orphaned_client_ids = self.remove_debug_session(debug_session)
        else:
            logger.info(f"could not find debug session with gdb pid {gdbpid}")
            orphaned_client_ids = []
        return orphaned_client_ids

    def remove_debug_session(self, debug_session: DebugSession) -> List[str]:
        logger.info(f"Removing debug session for pid {debug_session.pid}")
        try:
            debug_session.terminate()
        except Exception:
            logger.error(traceback.format_exc())
        orphaned_client_ids = self.debug_session_to_client_ids.pop(debug_session, [])
        return orphaned_client_ids

    def remove_debug_sessions_with_no_clients(self) -> None:
        to_remove = []
        for debug_session, _ in self.debug_session_to_client_ids.items():
            if len(debug_session.client_ids) == 0:
                to_remove.append(debug_session)
        for debug_session in to_remove:
            self.remove_debug_session(debug_session)

    def get_client_ids_from_gdb_pid(self, pid: int) -> List[str]:
        debug_session = self.debug_session_from_pid(pid)
        if debug_session:
            return self.debug_session_to_client_ids.get(debug_session, [])
        return []

    def get_pid_from_debug_session(self, debug_session: DebugSession) -> Optional[int]:
        if debug_session and debug_session.pid:
            return debug_session.pid
        return None

    def debug_session_from_pid(self, pid: int) -> Optional[DebugSession]:
        for debug_session in self.debug_session_to_client_ids:
            this_pid = self.get_pid_from_debug_session(debug_session)
            if this_pid == pid:
                return debug_session
        return None

    def debug_session_from_client_id(self, client_id: str) -> Optional[DebugSession]:
        for debug_session, client_ids in self.debug_session_to_client_ids.items():
            if client_id in client_ids:
                return debug_session
        return None

    def exit_all_gdb_processes(self):
        logger.info("exiting all subprocesses")
        for debug_session in self.debug_session_to_client_ids:
            # TODO kill gdb process and controller
            logger.info(f"Exiting debug session for pid {debug_session.pid}")
            debug_session.terminate()
            self.debug_session_to_client_ids.pop(debug_session)

    def get_dashboard_data(self) -> List[Any]:
        data = []
        for debug_session, client_ids in self.debug_session_to_client_ids.items():
            data.append(debug_session.to_dict())
        return data

    def disconnect_client(self, client_id: str):
        for debug_session, client_ids in self.debug_session_to_client_ids.items():
            if client_id in client_ids:
                client_ids.remove(client_id)
                debug_session.remove_client(client_id)
        self.remove_debug_sessions_with_no_clients()

    def _spawn_new_gdb_controller(self):
        pass

    def _connect_to_existing_gdb_controller(self):
        pass