/* Cross-compiled Windows console setup: maps a persistent WebDAV drive. */
#include <windows.h>
#include <wininet.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include <process.h>

static int line(const char *prompt, char *buf, size_t n) {
    printf("%s", prompt); fflush(stdout);
    if (!fgets(buf, (int)n, stdin)) return 0;
    size_t len = strlen(buf);
    if (!len || (buf[len-1] != '\n' && !feof(stdin))) return 0;
    while (len && (buf[len-1] == '\n' || buf[len-1] == '\r')) buf[--len] = 0;
    return len > 0;
}
int main(void) {
    char url[2048], drive[32], user[256], unc[3072], host[256], path[2048];
    URL_COMPONENTSA parts = {0};
    puts("ModalFiles setup - maps a persistent Windows WebDAV drive.\n"
         "The Windows WebClient service must be running.\n");
    if (!line("Modal HTTPS base URL: ", url, sizeof(url))) goto invalid;
    parts.dwStructSize = sizeof(parts);
    parts.lpszHostName = host; parts.dwHostNameLength = sizeof(host);
    parts.lpszUrlPath = path; parts.dwUrlPathLength = sizeof(path);
    parts.dwUserNameLength = 1; parts.dwPasswordLength = 1; parts.dwExtraInfoLength = 1;
    if (!InternetCrackUrlA(url, 0, 0, &parts) || parts.nScheme != INTERNET_SCHEME_HTTPS ||
        parts.dwUserNameLength || parts.dwPasswordLength || parts.dwExtraInfoLength ||
        !parts.dwHostNameLength || strchr(path, '%') || strchr(path, '\\')) goto invalid;
    for (char *p = host; *p; p++) if (!(isalnum((unsigned char)*p) || *p == '.' || *p == '-')) goto invalid;
    for (char *p = path; *p; p++) {
        if (!(isalnum((unsigned char)*p) || *p == '/' || *p == '-' || *p == '_' || *p == '.')) goto invalid;
        if (*p == '/') *p = '\\';
    }
    if (!line("Drive letter (for example Z): ", drive, sizeof(drive)) ||
        strlen(drive) != 1 || !isalpha((unsigned char)drive[0])) goto invalid;
    drive[0] = (char)toupper((unsigned char)drive[0]); drive[1] = ':'; drive[2] = 0;
    if (!line("WebDAV username: ", user, sizeof(user))) goto invalid;
    char userarg[264]; snprintf(userarg, sizeof(userarg), "/user:%s", user);
    if (parts.nPort == INTERNET_DEFAULT_HTTPS_PORT)
        snprintf(unc, sizeof(unc), "\\\\%s@SSL\\DavWWWRoot%s", host, path);
    else snprintf(unc, sizeof(unc), "\\\\%s@SSL@%u\\DavWWWRoot%s", host, parts.nPort, path);
    size_t len = strlen(unc); while (len && unc[len-1] == '\\') unc[--len] = 0;
    printf("Connecting %s to %s. Enter the WebDAV password when prompted.\n", drive, unc);
    const char *args[] = {"net.exe", "use", drive, unc, "*", userarg, "/persistent:yes", NULL};
    intptr_t result = _spawnvp(_P_WAIT, "net.exe", args);
    if (result != 0) { puts("Mapping failed. Check the URL, credentials and WebClient service."); }
    else puts("Done. Open the drive in File Explorer.");
    puts("Press Enter to close."); getchar();
    return result == 0 ? 0 : 1;
invalid:
    puts("Invalid input. Use an HTTPS URL without credentials, query, fragment or encoded path.");
    puts("Press Enter to close."); getchar(); return 1;
}
