#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stddef.h>

enum {
    MAX_WIDE_PATH = 4096,
    MAX_WIDE_CMD = 8192
};

static size_t wlen(const wchar_t *s) {
    size_t n = 0;
    while (s[n] != 0) n++;
    return n;
}

static int wstr_copy(wchar_t *dst, size_t cap, const wchar_t *src) {
    size_t i = 0;
    if (cap == 0) return 0;
    while (src[i] != 0) {
        if (i + 1 >= cap) {
            dst[0] = 0;
            return 0;
        }
        dst[i] = src[i];
        i++;
    }
    dst[i] = 0;
    return 1;
}

static int wstr_append(wchar_t *dst, size_t cap, const wchar_t *src) {
    size_t cur = wlen(dst);
    size_t i = 0;
    if (cur >= cap) return 0;
    while (src[i] != 0) {
        if (cur + i + 1 >= cap) {
            dst[cur] = 0;
            return 0;
        }
        dst[cur + i] = src[i];
        i++;
    }
    dst[cur + i] = 0;
    return 1;
}

static int wappend_ulong(wchar_t *dst, size_t cap, unsigned long val) {
    wchar_t tmp[32];
    size_t i = 0;
    if (val == 0) {
        tmp[i++] = L'0';
    } else {
        while (val > 0 && i < (sizeof(tmp) / sizeof(tmp[0]))) {
            tmp[i++] = (wchar_t)(L'0' + (val % 10UL));
            val /= 10UL;
        }
    }
    while (i > 0) {
        wchar_t ch[2];
        ch[0] = tmp[--i];
        ch[1] = 0;
        if (!wstr_append(dst, cap, ch)) return 0;
    }
    return 1;
}

static int join_path_w(wchar_t *out, size_t cap, const wchar_t *dir, const wchar_t *rel) {
    size_t dlen = wlen(dir);
    if (!wstr_copy(out, cap, dir)) return 0;
    if (dlen > 0 && dir[dlen - 1] != L'\\' && dir[dlen - 1] != L'/') {
        if (!wstr_append(out, cap, L"\\")) return 0;
    }
    return wstr_append(out, cap, rel);
}

static int parent_dir_w(const wchar_t *path, wchar_t *out, size_t cap) {
    if (!wstr_copy(out, cap, path)) return 0;
    size_t len = wlen(out);
    while (len > 0 && (out[len - 1] == L'\\' || out[len - 1] == L'/')) {
        if (len == 3 && out[1] == L':') return 0;
        len--;
    }
    while (len > 0 && out[len - 1] != L'\\' && out[len - 1] != L'/') {
        len--;
    }
    if (len == 0) return 0;
    if (len == 3 && out[1] == L':') {
        out[3] = 0;
    } else {
        out[len - 1] = 0;
    }
    return 1;
}

static int file_exists_w(const wchar_t *path) {
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static const wchar_t *extract_extra_args(const wchar_t *cmdline) {
    const wchar_t *p = cmdline;
    if (!p) return L"";
    while (*p == L' ' || *p == L'\t') p++;
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') p++;
        if (*p == L'"') p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t') p++;
    }
    while (*p == L' ' || *p == L'\t') p++;
    return p;
}

static int try_spawn_w(const wchar_t *exe, const wchar_t *base_args,
                       const wchar_t *extra_args, const wchar_t *cwd,
                       DWORD wait_ms, int *out_exited_nonzero,
                       DWORD *out_exit_code) {
    wchar_t cmd[MAX_WIDE_CMD];
    *out_exited_nonzero = 0;
    *out_exit_code = 0;

    if (!wstr_copy(cmd, MAX_WIDE_CMD, L"\"")) return 0;
    if (!wstr_append(cmd, MAX_WIDE_CMD, exe)) return 0;
    if (!wstr_append(cmd, MAX_WIDE_CMD, L"\" ")) return 0;
    if (!wstr_append(cmd, MAX_WIDE_CMD, base_args)) return 0;
    if (extra_args && *extra_args != 0) {
        if (!wstr_append(cmd, MAX_WIDE_CMD, L" ")) return 0;
        if (!wstr_append(cmd, MAX_WIDE_CMD, extra_args)) return 0;
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW | DETACHED_PROCESS,
                        NULL, cwd, &si, &pi)) {
        return 0;
    }

    DWORD wait_res = WaitForSingleObject(pi.hProcess, wait_ms);
    if (wait_res == WAIT_OBJECT_0) {
        DWORD code = 0;
        if (!GetExitCodeProcess(pi.hProcess, &code)) code = 1;
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        if (code == 0) return 1;
        *out_exited_nonzero = 1;
        *out_exit_code = code;
        return 0;
    }

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
}

static void show_bundled_failure(DWORD exit_code) {
    wchar_t msg[1024];
    wstr_copy(msg, sizeof(msg) / sizeof(msg[0]),
              L"ModalFiles GUI failed to start using the bundled Python runtime (exit code ");
    wappend_ulong(msg, sizeof(msg) / sizeof(msg[0]), (unsigned long)exit_code);
    wstr_append(msg, sizeof(msg) / sizeof(msg[0]),
                L").\n\nCheck %APPDATA%\\ModalFiles\\gui.log for details, or reinstall ModalFiles.");
    MessageBoxW(NULL, msg, L"ModalFiles GUI", MB_ICONERROR | MB_OK);
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR lpCmdLine, int nShow) {
    (void)hInst;
    (void)hPrev;
    (void)lpCmdLine;
    (void)nShow;

    wchar_t exePath[MAX_WIDE_PATH];
    DWORD n = GetModuleFileNameW(NULL, exePath, MAX_WIDE_PATH);
    if (n == 0 || n >= MAX_WIDE_PATH) return 1;

    wchar_t dir[MAX_WIDE_PATH];
    if (!parent_dir_w(exePath, dir, MAX_WIDE_PATH)) return 1;

    wchar_t parent[MAX_WIDE_PATH];
    int has_parent = parent_dir_w(dir, parent, MAX_WIDE_PATH);

    const wchar_t *extra_args = extract_extra_args(GetCommandLineW());
    const wchar_t *args = L"-m modalfiles.gui.server";

    wchar_t candidate[MAX_WIDE_PATH];
    int exited_nonzero = 0;
    DWORD exit_code = 0;

    /* 1) Bundled embeddable Python next to launcher (primary distribution layout) */
    if (join_path_w(candidate, MAX_WIDE_PATH, dir, L"python\\pythonw.exe") &&
        file_exists_w(candidate)) {
        if (try_spawn_w(candidate, args, extra_args, dir, 500,
                        &exited_nonzero, &exit_code)) {
            return 0;
        }
        if (exited_nonzero) {
            show_bundled_failure(exit_code);
            return 1;
        }
    }

    /* 2) Bundled embeddable Python in parent directory (if launcher in bin/) */
    if (has_parent &&
        join_path_w(candidate, MAX_WIDE_PATH, parent, L"python\\pythonw.exe") &&
        file_exists_w(candidate)) {
        if (try_spawn_w(candidate, args, extra_args, parent, 500,
                        &exited_nonzero, &exit_code)) {
            return 0;
        }
        if (exited_nonzero) {
            show_bundled_failure(exit_code);
            return 1;
        }
    }

    /* 3) Local dev virtualenvs next to launcher or in parent directory */
    const wchar_t *venv_rels[2] = {
        L"venv\\Scripts\\pythonw.exe",
        L".venv\\Scripts\\pythonw.exe"
    };
    for (size_t i = 0; i < 2; i++) {
        if (join_path_w(candidate, MAX_WIDE_PATH, dir, venv_rels[i]) &&
            file_exists_w(candidate) &&
            try_spawn_w(candidate, args, extra_args, dir, 350,
                        &exited_nonzero, &exit_code)) {
            return 0;
        }
    }
    if (has_parent) {
        for (size_t i = 0; i < 2; i++) {
            if (join_path_w(candidate, MAX_WIDE_PATH, parent, venv_rels[i]) &&
                file_exists_w(candidate) &&
                try_spawn_w(candidate, args, extra_args, parent, 350,
                            &exited_nonzero, &exit_code)) {
                return 0;
            }
        }
    }

    /* 4) System Python fallbacks for standalone dev builds */
    if (try_spawn_w(L"pyw.exe", L"-3 -m modalfiles.gui.server", extra_args, dir, 350,
                    &exited_nonzero, &exit_code)) {
        return 0;
    }
    if (try_spawn_w(L"pythonw.exe", args, extra_args, dir, 350,
                    &exited_nonzero, &exit_code)) {
        return 0;
    }

    MessageBoxW(NULL,
        L"ModalFiles GUI could not start because the bundled Python runtime\n"
        L"(python\\pythonw.exe) was not found and no compatible Python 3.10+\n"
        L"interpreter is available on this system.\n\n"
        L"Reinstall ModalFiles to restore the bundled runtime.",
        L"ModalFiles GUI", MB_ICONERROR | MB_OK);
    return 1;
}
