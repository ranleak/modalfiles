#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlwapi.h>
#include <stdio.h>
#include <string.h>

static void join_path(char *out, size_t outsz, const char *dir, const char *name) {
    snprintf(out, outsz, "%s\\%s", dir, name);
}

static int file_exists(const char *path) {
    DWORD a = GetFileAttributesA(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static int try_spawn(const char *exe, const char *args, const char *cwd) {
    char cmd[4096];
    snprintf(cmd, sizeof(cmd), "\"%s\" %s", exe, args);
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW | DETACHED_PROCESS, NULL, cwd, &si, &pi)) {
        return 0;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR lpCmdLine, int nShow) {
    (void)hInst; (void)hPrev; (void)lpCmdLine; (void)nShow;
    char exePath[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, exePath, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 1;
    char dir[MAX_PATH];
    strncpy(dir, exePath, MAX_PATH - 1);
    dir[MAX_PATH - 1] = 0;
    PathRemoveFileSpecA(dir);

    char candidate[MAX_PATH];
    const char *args = "-m modalfiles.gui.server";

    /* 1) venv next to launcher */
    join_path(candidate, sizeof(candidate), dir, "venv\\Scripts\\pythonw.exe");
    if (file_exists(candidate) && try_spawn(candidate, args, dir)) return 0;

    /* 2) venv in parent (if launcher in bin/) */
    char parent[MAX_PATH];
    strncpy(parent, dir, MAX_PATH - 1); parent[MAX_PATH - 1] = 0;
    PathRemoveFileSpecA(parent);
    join_path(candidate, sizeof(candidate), parent, "venv\\Scripts\\pythonw.exe");
    if (file_exists(candidate) && try_spawn(candidate, args, parent)) return 0;

    /* 3) py launcher (cwd = install dir so modalfiles package is importable) */
    if (try_spawn("pyw.exe", "-3 -m modalfiles.gui.server", dir)) return 0;

    /* 4) pythonw on PATH */
    if (try_spawn("pythonw.exe", args, dir)) return 0;

    MessageBoxA(NULL,
        "ModalFiles GUI could not find Python.\n\n"
        "Install Python 3.10+ from https://www.python.org/downloads/ and ensure\n"
        "the 'py' launcher or 'pythonw.exe' is available on PATH.\n\n"
        "Then relaunch ModalFiles GUI.",
        "ModalFiles GUI", MB_ICONERROR | MB_OK);
    return 1;
}
