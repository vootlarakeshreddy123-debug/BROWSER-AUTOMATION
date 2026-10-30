from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


SW_RESTORE = 9
SW_MAXIMIZE = 3

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

GW_OWNER = 4


if sys.platform == "win32":

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )


    def _visible_chrome_windows() -> list[int]:
        result: list[int] = []

        @EnumWindowsProc
        def callback(
            hwnd: int,
            _lparam: int,
        ) -> bool:

            try:
                if not user32.IsWindowVisible(hwnd):
                    return True

                if user32.GetWindow(
                    hwnd,
                    GW_OWNER,
                ):
                    return True

                cls = ctypes.create_unicode_buffer(
                    256
                )

                user32.GetClassNameW(
                    hwnd,
                    cls,
                    256,
                )

                if cls.value == "Chrome_WidgetWin_1":
                    result.append(
                        int(hwnd)
                    )

            except Exception:
                pass

            return True

        user32.EnumWindows(
            callback,
            0,
        )

        return result


    class PROCESSENTRY32W(
        ctypes.Structure
    ):
        _fields_ = [
            (
                "dwSize",
                wintypes.DWORD,
            ),
            (
                "cntUsage",
                wintypes.DWORD,
            ),
            (
                "th32ProcessID",
                wintypes.DWORD,
            ),
            (
                "th32DefaultHeapID",
                ctypes.c_void_p,
            ),
            (
                "th32ModuleID",
                wintypes.DWORD,
            ),
            (
                "cntThreads",
                wintypes.DWORD,
            ),
            (
                "th32ParentProcessID",
                wintypes.DWORD,
            ),
            (
                "pcPriClassBase",
                wintypes.LONG,
            ),
            (
                "dwFlags",
                wintypes.DWORD,
            ),
            (
                "szExeFile",
                wintypes.WCHAR * 260,
            ),
        ]


    def _process_tree(
        root_pid: int,
    ) -> set[int]:

        snapshot = (
            kernel32.CreateToolhelp32Snapshot(
                0x00000002,
                0,
            )
        )

        if snapshot == wintypes.HANDLE(
            -1
        ).value:
            return {root_pid}

        try:
            entry = PROCESSENTRY32W()

            entry.dwSize = ctypes.sizeof(
                entry
            )

            parent_map: dict[
                int,
                int,
            ] = {}

            if kernel32.Process32FirstW(
                snapshot,
                ctypes.byref(entry),
            ):

                while True:
                    parent_map[
                        entry.th32ProcessID
                    ] = (
                        entry.th32ParentProcessID
                    )

                    if not kernel32.Process32NextW(
                        snapshot,
                        ctypes.byref(entry),
                    ):
                        break

            tree = {
                root_pid
            }

            changed = True

            while changed:
                changed = False

                for (
                    child,
                    parent,
                ) in parent_map.items():

                    if (
                        parent in tree
                        and child not in tree
                    ):
                        tree.add(child)
                        changed = True

            return tree

        finally:
            kernel32.CloseHandle(
                snapshot
            )


    def windows_for_process(
        pid: int,
    ) -> list[int]:

        result: list[int] = []

        pids = _process_tree(
            pid
        )

        @EnumWindowsProc
        def callback(
            hwnd: int,
            _lparam: int,
        ) -> bool:

            try:
                if not user32.IsWindowVisible(
                    hwnd
                ):
                    return True

                if user32.GetWindow(
                    hwnd,
                    GW_OWNER,
                ):
                    return True

                cls = ctypes.create_unicode_buffer(
                    256
                )

                user32.GetClassNameW(
                    hwnd,
                    cls,
                    256,
                )

                if (
                    cls.value
                    != "Chrome_WidgetWin_1"
                ):
                    return True

                owner_pid = wintypes.DWORD()

                user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(
                        owner_pid
                    ),
                )

                if (
                    owner_pid.value
                    in pids
                ):
                    result.append(
                        int(hwnd)
                    )

            except Exception:
                pass

            return True

        user32.EnumWindows(
            callback,
            0,
        )

        return result


    def _window_title(
        hwnd: int,
    ) -> str:

        length = (
            user32.GetWindowTextLengthW(
                hwnd
            )
        )

        if length <= 0:
            return ""

        text = ctypes.create_unicode_buffer(
            length + 1
        )

        user32.GetWindowTextW(
            hwnd,
            text,
            length + 1,
        )

        return text.value


    def find_chrome_window(
        title_hint: str = "",
        known_hwnds: set[int] | None = None,
    ) -> int | None:

        known = (
            known_hwnds
            or set()
        )

        candidates = (
            _visible_chrome_windows()
        )

        if title_hint:

            needle = (
                title_hint
                .strip()
                .lower()
            )

            exact = [
                hwnd
                for hwnd in candidates
                if (
                    _window_title(hwnd)
                    .strip()
                    .lower()
                    == needle
                )
            ]

            if exact:
                return exact[0]

            partial = [
                hwnd
                for hwnd in candidates
                if (
                    needle
                    and needle
                    in _window_title(
                        hwnd
                    ).lower()
                )
            ]

            if partial:
                return partial[0]

        fresh = [
            hwnd
            for hwnd in candidates
            if hwnd not in known
        ]

        if fresh:
            return fresh[0]

        if candidates:
            return candidates[0]

        return None


    def _force_foreground(
        hwnd: int,
    ) -> None:

        foreground = (
            user32.GetForegroundWindow()
        )

        # IMPORTANT:
        # GetCurrentThreadId belongs to kernel32.
        current_thread = (
            kernel32.GetCurrentThreadId()
        )

        foreground_thread = (
            user32.GetWindowThreadProcessId(
                foreground,
                None,
            )
            if foreground
            else 0
        )

        target_thread = (
            user32.GetWindowThreadProcessId(
                hwnd,
                None,
            )
        )

        attached: list[int] = []

        try:

            for thread_id in {
                foreground_thread,
                target_thread,
            }:

                if (
                    thread_id
                    and thread_id
                    != current_thread
                ):

                    if user32.AttachThreadInput(
                        thread_id,
                        current_thread,
                        True,
                    ):
                        attached.append(
                            thread_id
                        )

            # Windows foreground activation trick.
            user32.keybd_event(
                VK_MENU,
                0,
                0,
                0,
            )

            user32.keybd_event(
                VK_MENU,
                0,
                KEYEVENTF_KEYUP,
                0,
            )

            user32.AllowSetForegroundWindow(
                0xFFFFFFFF
            )

            user32.ShowWindow(
                hwnd,
                SW_RESTORE,
            )

            user32.ShowWindow(
                hwnd,
                SW_MAXIMIZE,
            )

            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_SHOWWINDOW,
            )

            user32.SetWindowPos(
                hwnd,
                HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_SHOWWINDOW,
            )

            user32.BringWindowToTop(
                hwnd
            )

            user32.SetActiveWindow(
                hwnd
            )

            user32.SetForegroundWindow(
                hwnd
            )

            user32.SetFocus(
                hwnd
            )

        finally:

            for thread_id in attached:

                user32.AttachThreadInput(
                    thread_id,
                    current_thread,
                    False,
                )


    def activate_chrome_window(
        hwnd: int,
    ) -> bool:

        if not hwnd:
            return False

        if not user32.IsWindow(
            hwnd
        ):
            return False

        for _ in range(3):

            try:
                _force_foreground(
                    hwnd
                )
            except Exception:
                pass

            time.sleep(
                0.06
            )

            if (
                user32.GetForegroundWindow()
                == hwnd
            ):
                return True

        return False


else:

    def _visible_chrome_windows() -> list[int]:
        return []


    def windows_for_process(
        pid: int,
    ) -> list[int]:
        return []


    def find_chrome_window(
        title_hint: str = "",
        known_hwnds: set[int] | None = None,
    ) -> int | None:
        return None


    def activate_chrome_window(
        hwnd: int,
    ) -> bool:
        return False